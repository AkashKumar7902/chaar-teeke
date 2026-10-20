"""The call runner: places a call over the telephony port, drives a flow, saves what the person says.

`place(call_id)` is what the scheduler hands a due call to. It never substitutes anything for what is
missing: a language without recorded voice, a missing clinic phone number, a refused originate or a
dropped telephone link ends the call as failed (or no answer) with a plain sentence in `calls.error`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import sqlite3
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import audit, clock, doses, enrol
from ..context import AppContext
from ..doses import emit
from ..ids import new_id
from ..telephony.model import (
    CallAnswered,
    CommandRejected,
    Dtmf,
    Event,
    Hangup,
    MissedCall,
    PlaybackFinished,
    RecordingDone,
    TelephonyConnectionError,
    TelephonyError,
)
from ..telephony.sim import SERVER_STOPPED
from . import flow as fl
from .prompts import LANG_NAMES, PromptError, visit_time
from .voiced import UnreadableRecording, voiced_seconds

log = logging.getLogger("chaarteeke.runner")

# A phone number (or a browser phone's session address) must never reach calls.error, an event or a log
# line, and the PBX may echo the endpoint it was asked to dial in its error text.
_NUMBERISH = re.compile(r"\+?\d[\d\s\-()]{6,}\d|sim:[\w-]+")


def scrub(text: str | None) -> str | None:
    return None if text is None else _NUMBERISH.sub("[number removed]", text)


def _log_failure(what: str, error: BaseException) -> None:
    """Log a failure by its class, its scrubbed message and the code path, never the message as raised."""
    log.error(
        "%s: %s: %s\n%s",
        what,
        type(error).__name__,
        scrub(str(error)),
        "".join(traceback.format_tb(error.__traceback__)),
    )


WITHDRAW_REASONS = {
    "declined": "the patient said no to reminders",
    "withdrawn": "the patient pressed 9 to stop the calls",
    "wrong_number": "the patient pressed 8: wrong number",
}
_NOTICE_KINDS = ("duplicate_notice",)
_DONE = ("completed", "declined", "withdrawn", "wrong_number")
STOP_KEY = "9"
PERSON_HUNG_UP = enrol.PERSON_HUNG_UP
STORAGE_FAILED = "The recording could not be saved (the disk is full or not writable)."
_ANSWER_COLUMNS = {"place_type": "place_type", "animal": "animal", "signs": "signs"}
_BITE_QUESTIONS = frozenset({"consent_questions", "place", *_ANSWER_COLUMNS})


class RunnerError(Exception):
    """A call the runner was given that it cannot run at all (unknown, or not waiting to be dialled)."""


class _Refused(Exception):
    """The call cannot be placed; `message` goes to calls.error and `status` to calls.status."""

    def __init__(self, message: str, status: str = "failed") -> None:
        super().__init__(message)
        self.message, self.status = message, status


class _CallFailed(Exception):
    """The call broke while it was running; `message` is plain words."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _HungUp(Exception):
    pass


class _Ended(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Plan:
    call_id: str
    course_id: str
    clinic_id: str
    kind: str
    transport: str
    number: str
    flow: fl.Flow
    source: str
    is_demo: int
    day0: date


@dataclass(frozen=True)
class _Clip:
    """A recording the app fetched from the phone system, and the voiced seconds it measured in it."""

    audio: bytes
    voiced_s: float


@dataclass
class _Live:
    """One call in progress: its events, the keys pressed ahead, and what was heard."""

    call_id: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    port_call_id: str | None = None
    keys: deque[str] = field(default_factory=deque)
    played: set[str] = field(default_factory=set)
    clips: list[str] = field(default_factory=list)  # recordings that came back ok, not yet saved
    heard: dict[str, _Clip] = field(default_factory=dict)  # the fetched audio of those, by recording name
    hangup: Hangup | None = None
    answered_at: float | None = None
    plays: int = 0
    recordings: int = 0


# Other lanes' modules are imported where used: they are written in parallel and may not exist yet.
def _process_capture(ctx: AppContext, report_id: str) -> None:
    from ..places import review

    review.process_capture(ctx, report_id)


def _alert_from_report(ctx: AppContext, conn: sqlite3.Connection, report_id: str) -> str | None:
    from .. import alerts

    return alerts.from_report(ctx, conn, report_id)


def _cause_sentence(hangup: Hangup) -> str:
    return {
        17: "The line was busy.",
        19: "The phone rang but nobody answered.",
        21: "The call was declined.",
    }.get(hangup.cause_code or 0, hangup.cause_text or "The call did not connect.")


class CallRunner:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._live: dict[str, _Live] = {}
        self._by_port: dict[str, _Live] = {}
        self._pump: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        # work in worker threads, which cancelling a task cannot stop
        self._threads: set[asyncio.Future] = set()
        self._placing: dict[str, asyncio.Task] = {}  # call id -> the task running it
        self._loop: asyncio.AbstractEventLoop | None = None
        self._paused = False
        self._down: TelephonyError | None = None

    # ------------------------------------------------------------ event pump
    async def start(self) -> None:
        """Begin consuming the telephony port's events. The composition root starts the port itself."""
        self._loop = asyncio.get_running_loop()
        if self._pump is None:
            self._pump = asyncio.create_task(self._consume(), name="call-runner")

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            await asyncio.gather(self._pump, return_exceptions=True)
            self._pump = None
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        # A worker thread (the place review) cannot be cancelled and uses the database: wait until it is done, so
        # nothing writes after the caller closes the database.
        await asyncio.gather(*self._threads, return_exceptions=True)
        self._fail_live(TelephonyError("The system is stopping."))

    def cancel_calls(self, timeout_s: float = 15.0) -> int:
        """End every call in progress and refuse new ones until `resume()`. For the demo reset, which is about
        to delete the courses these calls belong to: a call still running would write its answers to rows that
        no longer exist. Blocks, so call it from a worker thread (a request handler), not from the event loop.
        Returns how many calls were ended."""
        loop = self._loop
        self._paused = True
        if loop is None or not loop.is_running():
            return 0
        with contextlib.suppress(RuntimeError):
            if asyncio.get_running_loop() is loop:
                raise RuntimeError("cancel_calls waits for the calls to end: call it from a worker thread")
        return asyncio.run_coroutine_threadsafe(self._cancel_calls(), loop).result(timeout_s)

    async def _cancel_calls(self) -> int:
        tasks = list(self._placing.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def resume(self) -> None:
        self._paused = False

    async def wait_background(self) -> None:
        """Wait for place-review work started by finished calls (the tests, and a clean shutdown)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    async def _consume(self) -> None:
        try:
            async for ev in self.ctx.telephony.events():
                try:
                    self._dispatch(ev)
                except Exception as e:
                    _log_failure("could not handle a telephony event", e)
                    self.ctx.bus.publish("system", {"check": "telephony_events", "ok": False})
        except TelephonyError as e:
            self._fail_live(e)
            self.ctx.bus.publish("system", {"check": "telephony", "ok": False})

    def _fail_live(self, error: TelephonyError) -> None:
        self._down = error
        for live in self._live.values():
            live.queue.put_nowait(error)

    def _dispatch(self, ev: Event) -> None:
        if isinstance(ev, MissedCall):
            self._missed_call(ev)
            return
        live: _Live | None
        if isinstance(ev, CallAnswered):
            live = self._live.get(ev.correlation_id)
            if live is not None:
                live.port_call_id = ev.call_id
                self._by_port[ev.call_id] = live
        elif isinstance(ev, Hangup):
            live = self._live.get(ev.correlation_id) or self._by_port.get(ev.call_id)
        else:
            live = self._by_port.get(ev.call_id)
        if live is None:
            log.debug("telephony event for a call this runner does not own")
            return
        live.queue.put_nowait(ev)

    def _missed_call(self, ev: MissedCall) -> None:
        number = ev.caller_number
        transport = "sim" if number and number.startswith("sim:") else "ari"
        clinic = ev.clinic_id or self.ctx.settings.clinic_lines.get(ev.line or "")
        try:
            enrol.record_missed_call(self.ctx, number, transport, ev.at, clinic_hint=clinic)
        except Exception as e:
            _log_failure("could not record a missed call", e)
            self.ctx.bus.publish("system", {"check": "missed_call", "ok": False})

    # ---------------------------------------------------------------- place
    async def place(self, call_id: str) -> str:
        """Dial one call and run it to the end. Returns the final call status."""
        row = self.ctx.db.one("SELECT * FROM calls WHERE id=?", (call_id,))
        if row is None:
            raise RunnerError("This call was not found.")
        if row["status"] not in ("scheduled", "dialing"):
            raise RunnerError(f"This call is already {row['status']}.")
        try:
            if self._paused:
                raise _Refused("The demo is being reset, so this call was not placed.", "cancelled")
            plan = self._plan(row)
        except _Refused as e:
            self._finish(call_id, row, e.status, e.message)
            return e.status
        live = _Live(call_id)
        self._live[call_id] = live
        self._placing[call_id] = asyncio.current_task()
        try:
            status, error = await self._run(plan, live)
        except asyncio.CancelledError:
            self._finish(call_id, row, "failed", "The call was interrupted.", live)
            raise
        except Exception as e:
            _log_failure(f"call {call_id} broke", e)
            self._finish(
                call_id, row, "failed", f"The call stopped on an internal error ({type(e).__name__}).", live
            )
            raise
        finally:
            self._placing.pop(call_id, None)
            await self._clean_up(plan, live)
        self._finish(call_id, row, status, error, live, plan)
        return status

    # --------------------------------------------------------------- planning
    def _plan(self, call: sqlite3.Row) -> _Plan:
        ctx = self.ctx
        if ctx.telephony is None or ctx.prompts is None:
            raise _Refused("No telephone link or prompt catalogue is attached.")
        if self._down is not None:
            raise _Refused(f"The telephone link is down: {self._down}")
        course = ctx.db.one("SELECT * FROM courses WHERE id=?", (call["course_id"],))
        if course is None:
            raise _Refused("The patient's course no longer exists.", "cancelled")
        if course["source"] == "demo":
            raise _Refused("This is demo history, not a real patient, so it is never dialled.")
        clinic = ctx.db.one("SELECT * FROM clinics WHERE id=?", (course["clinic_id"],))
        if clinic is None:
            raise _Refused("The clinic is not in the system.")
        try:
            hours = None if call["kind"] in _NOTICE_KINDS else visit_time(clinic)  # notices name no time
            number = enrol.dial_number(ctx, call["id"])
            prior = self._prior_answers(course["id"])
            plan_flow, team_ward = self._make_flow(call, course, clinic, hours, prior)
            self._check_audio(plan_flow, clinic["id"], hours, team_ward)
        except (PromptError, enrol.EnrolError) as e:
            raise _Refused(e.message) from e
        return _Plan(
            call["id"], course["id"], clinic["id"], call["kind"], "sim" if number.startswith("sim:") else "ari",
            number, plan_flow, course["source"], course["is_demo"], date.fromisoformat(course["day0"]),
        )  # fmt: skip

    def _prior_answers(self, course_id: str) -> dict[str, str]:
        rows = self.ctx.db.query(
            "SELECT a.question, a.value FROM answers a JOIN calls c ON c.id = a.call_id"
            " WHERE c.course_id=? ORDER BY a.id",
            (course_id,),
        )
        return {r["question"]: r["value"] for r in rows}

    def _dose_rows(self, course_id: str) -> list[sqlite3.Row]:
        return self.ctx.db.query("SELECT * FROM doses WHERE course_id=? ORDER BY dose_no", (course_id,))

    def _make_flow(
        self,
        call: sqlite3.Row,
        course: sqlite3.Row,
        clinic: sqlite3.Row,
        hours: str | None,
        prior: dict[str, str],
    ) -> tuple[fl.Flow, int | None]:
        policy, lang, kind = self.ctx.policy, course["lang"], call["kind"]
        digits = re.sub(r"\D", "", course["register_no"] or "")
        register = digits if digits and enrol.same_phone_courses(self.ctx, course["id"]) > 1 else None
        schedule = [
            fl.DoseInfo(d["dose_no"], date.fromisoformat(d["due_date"]))
            for d in self._dose_rows(course["id"])
        ]
        if kind in _NOTICE_KINDS:
            return fl.NoticeFlow(policy=policy, lang=lang), None
        if kind == "first":
            asks_language = course["source"] == "simulator" and "lang" not in prior
            today = clock.local_today(self.ctx.settings.local_tz)
            upcoming = doses.next_due(self.ctx, course["id"])
            next_dose = (
                fl.DoseInfo(upcoming.dose_no, date.fromisoformat(upcoming.due_date)) if upcoming else None
            )
            if next_dose is not None and next_dose.due < today and not clinic["phone"]:
                raise _Refused(
                    "The next injection date has already passed and the clinic has no phone number set up, "
                    "so the call cannot tell the patient whom to call."
                )
            return (
                fl.FirstCallFlow(
                    policy=policy,
                    lang=lang,
                    clinic_id=clinic["id"],
                    next_dose=next_dose,
                    total=len(schedule),
                    visit_time=hours,
                    today=today,
                    ask_questions=course["eligibility"] in ("adult", "guardian_consented"),
                    clinic_phone=clinic["phone"],
                    guardian=course["eligibility"] == "guardian_consented",
                    ask_language=asks_language,
                    prior=prior,
                ),
                None,
            )
        dose = next((d for d in schedule if d.dose_no == call["dose_no"]), None)
        if dose is None:
            raise _Refused("The dose for this call was not found.")
        if kind == "missed_dose":
            after = next((d for d in schedule if d.dose_no == dose.dose_no + 1), None)
            return (
                fl.MorningAfterFlow(
                    policy=policy, lang=lang, clinic_id=clinic["id"], dose_no=dose.dose_no, total=len(schedule),
                    due=dose.due, next_dose=after, visit_time=hours, register=register,
                ),
                None,
            )  # fmt: skip
        out = bool(clinic["vaccine_out"])
        phone = clinic["phone"]
        if out and not phone:
            raise _Refused(
                "The clinic is marked out of vaccine but has no phone number set up, so the reminder cannot say whom to call."
            )
        ward = self._team_ward(course)
        return (
            fl.ReminderFlow(
                policy=policy, lang=lang, clinic_id=clinic["id"], dose_no=dose.dose_no, total=len(schedule),
                due=dose.due, visit_time=hours, vaccine_out=out, clinic_phone=phone, team_ward=ward, register=register,
            ),
            ward,
        )  # fmt: skip

    def _team_ward(self, course: sqlite3.Row) -> int | None:
        """The ward to name in the 'team worked your area' line: matched, worked since day 0, not yet said."""
        db = self.ctx.db
        if db.one("SELECT 1 FROM kv WHERE key=?", (f"team_line:{course['id']}",)):
            return None
        rep = db.one("SELECT ward_no, location_status FROM bite_reports WHERE course_id=?", (course["id"],))
        if rep is None or rep["ward_no"] is None or rep["location_status"] not in ("matched", "decided"):
            return None
        worked = db.one(
            "SELECT 1 FROM worked_wards WHERE ward_no=? AND worked_on > ?", (rep["ward_no"], course["day0"])
        )
        return rep["ward_no"] if worked else None

    def _check_audio(self, flow: fl.Flow, clinic_id: str, hours: str | None, team_ward: int | None) -> None:
        """A language with any recorded voice missing is not called at all."""
        for lang in flow.langs:
            absent = self.ctx.prompts.missing(
                lang,
                clinic_ids=[clinic_id],
                times=[hours] if hours else [],
                ward_nos=[team_ward] if team_ward else [],
            )
            if absent:
                raise _Refused(
                    f"No recorded voice for {LANG_NAMES[lang]} yet: {len(absent)} files are missing, "
                    f"for example {absent[0]}."
                )

    # ---------------------------------------------------------------- running
    async def _run(self, plan: _Plan, live: _Live) -> tuple[str, str | None]:
        ctx, port = self.ctx, self.ctx.telephony
        self._set_status(plan.call_id, "dialing", transport=plan.transport)
        try:
            await port.originate(
                plan.number, plan.call_id, ring_timeout_s=ctx.policy["calls"]["ring_timeout_s"]
            )
        except TelephonyError as e:
            if isinstance(e, TelephonyConnectionError):
                ctx.bus.publish("system", {"check": "telephony", "ok": False})
            return "failed", f"The phone system could not place the call: {e}"
        self._set_status(plan.call_id, "ringing")
        try:
            pol = ctx.policy["calls"]
            first = await self._next(live, pol["ring_timeout_s"] + pol["step_timeout_s"])
            if first is None:
                return "failed", "The phone system did not say whether the call was answered."
            if isinstance(first, Hangup):
                return "no_answer", _cause_sentence(first)
            self._answered(plan, live)
            reason = await self._drive(plan, live)
        except TelephonyError as e:
            return "failed", f"The telephone link failed during the call: {e}"
        except _CallFailed as e:
            return "failed", e.message
        except _HungUp:
            reason = "hangup"
        return self._outcome(plan, live, reason)

    def _answered(self, plan: _Plan, live: _Live) -> None:
        live.answered_at = asyncio.get_running_loop().time()
        with self.ctx.db.transaction() as conn:
            conn.execute("UPDATE calls SET answered_at=? WHERE id=?", (clock.iso(clock.now()), plan.call_id))
        self._set_status(plan.call_id, "answered")

    @staticmethod
    def _outcome(plan: _Plan, live: _Live, reason: str) -> tuple[str, str | None]:
        if reason in _DONE:
            return "completed", None
        if reason == "gave_up":
            return "no_answer", "The person answered but gave no usable key."
        if reason == "playback_failed":
            return "failed", "A recorded message could not be played."
        if live.played & plan.flow.essential:
            return "completed", None
        if live.hangup is not None and live.hangup.cause_text == SERVER_STOPPED:
            return "failed", "The call was cut off because the server stopped."
        return "failed", PERSON_HUNG_UP

    async def _drive(self, plan: _Plan, live: _Live) -> str:
        """Feed events to the flow and carry out its actions until it ends. Returns the end reason."""
        flow = plan.flow
        event: fl.Event = fl.Start()
        while True:
            try:
                actions = flow.step(event)
                event = await self._execute(plan, live, actions)
            except _HungUp:
                flow.step(fl.Hangup())
                return "hangup"
            except _Ended as end:
                return end.reason

    @staticmethod
    def _answers_to(actions: list[fl.Action], after: int) -> str | None:
        """The keys the question that follows a prompt accepts; a recording question accepts only 9."""
        for nxt in actions[after + 1 :]:
            if isinstance(nxt, fl.Collect):
                return nxt.valid_keys
            if isinstance(nxt, fl.Record):
                return STOP_KEY
            if isinstance(nxt, fl.End):
                return None
        return None

    async def _execute(self, plan: _Plan, live: _Live, actions: list[fl.Action]) -> fl.Event:
        for i, action in enumerate(actions):
            match action:
                case fl.Play():
                    if not await self._play(plan, live, action, self._answers_to(actions, i)):
                        return fl.PlayDone(False)
                case fl.Collect():
                    return await self._collect(live, action)
                case fl.Record():
                    return await self._record(plan, live, action)
                case fl.Save():
                    await self._save(plan, live, action)
                case fl.Withdraw():
                    self._withdraw(plan, action.reason)
                case fl.MarkTaken():
                    self._note_taken(plan, action.dose_no)
                case fl.End():
                    raise _Ended(action.reason)
        raise ValueError("a flow step ended without a question, a recording or an end")

    async def _next(self, live: _Live, timeout: float | None) -> Event | None:
        """The next port event for this call; None on timeout. A hang-up or a dead link raises."""
        try:
            item = await asyncio.wait_for(live.queue.get(), timeout)
        except TimeoutError:
            return None
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, Hangup):
            live.hangup = item
            if live.answered_at is None:
                return item
            raise _HungUp
        return item

    def _step_timer(self):
        """A function giving the seconds left of this step (policy calls.step_timeout_s)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + self.ctx.policy["calls"]["step_timeout_s"]
        return lambda: max(0.0, end - loop.time())

    def _step_stalled(self, waiting_for: str) -> _CallFailed:
        seconds = self.ctx.policy["calls"]["step_timeout_s"]
        return _CallFailed(
            f"The phone did not report {waiting_for} within {seconds:g} seconds, so the call was ended."
        )

    async def _play(self, plan: _Plan, live: _Live, play: fl.Play, accepts: str | None = None) -> bool:
        """Play prompts. A question (`listen`) is cut short by the first key it accepts: the playback is
        stopped, that key is kept for the question, and the call moves on at once, so a person who knows the
        answer does not wait for the voice. A key pressed before the question began counts the same way, and
        the question is then not played at all. Any other key is kept as a wrong key, as before. A 9 pressed
        during a prompt that asks nothing (the greeting, the notice) is kept, so the person who pressed it
        reaches the stop-the-calls question next instead of being ignored. So is a key the prompt names in
        `keep` (key 0 over the short notice, which asks for the full one)."""
        flow, cat = plan.flow, self.ctx.prompts
        try:
            media = [m for say in play.prompts for m in cat.render(say.lang or flow.lang, say.id, say.slots)]
        except PromptError as e:
            raise _CallFailed(e.message) from e
        listening = play.listen and bool(accepts)
        early = next((k for k in live.keys if listening and k in accepts), None)
        if early is not None:
            live.keys = deque([early])
            return True
        live.plays += 1
        playback_id = f"{plan.call_id}-p{live.plays}"
        await self.ctx.telephony.play(live.port_call_id, media, playback_id)
        step = self._step_timer()
        cut_short = False
        held = STOP_KEY + play.keep
        pressed = [
            k for k in live.keys if k in held
        ]  # a 9 pressed over the greeting is still waiting when the notice ends
        while True:
            ev = await self._next(live, step())
            if ev is None:
                raise self._step_stalled("a message to finish playing")
            if isinstance(ev, Dtmf):
                live.keys.append(ev.digit)
                if ev.digit in held:
                    pressed.append(ev.digit)
                if listening and ev.digit in accepts and await self._stop_playback(live, playback_id):
                    live.keys = deque(
                        [ev.digit]
                    )  # the first key that answers; earlier wrong keys are dropped
                    cut_short = True
                    break
            elif isinstance(ev, PlaybackFinished) and ev.playback_id == playback_id:
                break
        if cut_short:
            return True
        if not play.listen:
            live.keys.clear()
            if pressed:  # a 9 wins over any other held key
                live.keys.append(STOP_KEY if STOP_KEY in pressed else pressed[0])
        if ev.ok:
            live.played.update(say.id for say in play.prompts)
        return ev.ok

    async def _stop_playback(self, live: _Live, playback_id: str) -> bool:
        """Stop the playing prompt. False when it had just ended by itself (nothing left to stop)."""
        try:
            await self.ctx.telephony.stop_playback(live.port_call_id, playback_id)
        except CommandRejected as e:
            if e.status == 404:
                return False
            raise
        return True

    async def _collect(self, live: _Live, collect: fl.Collect) -> fl.Event:
        try:
            if live.keys:
                return fl.Key(live.keys.popleft())
            deadline = asyncio.get_running_loop().time() + collect.timeout_s
            while True:
                ev = await self._next(live, max(0.0, deadline - asyncio.get_running_loop().time()))
                if ev is None:
                    return fl.NoInput()
                if isinstance(ev, Dtmf):
                    return fl.Key(ev.digit)
        finally:
            live.keys.clear()

    async def _record(self, plan: _Plan, live: _Live, record: fl.Record) -> fl.Event:
        """Record the answer. A 9 pressed while the question played, or while the person spoke, is not part
        of the answer: it comes back as Key("9") (the clip, if any, is dropped with the call's other clips)."""
        stop_pressed = STOP_KEY in live.keys
        live.keys.clear()  # nothing else pressed while the question played is part of a spoken answer
        if stop_pressed:
            return fl.Key(STOP_KEY)
        live.recordings += 1
        name = f"{plan.call_id}-r{live.recordings}"
        await self.ctx.telephony.record(
            live.port_call_id, name, max_seconds=record.max_s, max_silence_s=record.silence_s
        )
        pressed_nine = False
        step = self._step_timer()
        while True:
            ev = await self._next(live, step())
            if ev is None:
                raise self._step_stalled("the recording to finish")
            if isinstance(ev, Dtmf) and ev.digit == "9":
                pressed_nine = True
            elif isinstance(ev, RecordingDone) and ev.recording_name == name:
                if ev.ok:
                    live.clips.append(name)
                if pressed_nine:
                    return fl.Key("9")
                voiced = await self._measure(live, name) if ev.ok else None
                return fl.Recorded(name, ev.ok, ev.talking_s, voiced)

    async def _measure(self, live: _Live, name: str) -> float:
        """Fetch the recording and measure the voiced seconds in it, the same way for every transport. The
        phone system's own talking time (whole seconds) is only information: it reads 0 for a short word."""
        audio = await self.ctx.telephony.fetch_recording(name)
        calls = self.ctx.policy["calls"]
        try:
            voiced = await asyncio.to_thread(voiced_seconds, audio, calls["speech_frame_rms_min"])
        except UnreadableRecording as e:
            raise _CallFailed(f"The place recording could not be read: {e}.") from e
        live.heard[name] = _Clip(audio, voiced)
        return voiced

    # ----------------------------------------------------------- side effects
    def _withdraw(self, plan: _Plan, reason: str) -> None:
        enrol.withdraw(
            self.ctx, plan.course_id, f"patient:{plan.course_id}", WITHDRAW_REASONS[reason], kind=reason
        )
        with self.ctx.db.transaction() as conn:
            self._insert_answer(conn, plan, "withdraw", reason)

    def _note_taken(self, plan: _Plan, dose_no: int) -> None:
        """'Yes, I took it' is what the patient says, not what the clinic has seen. The dose stays open and the
        clinic's list shows it as 'says taken, confirm' until staff mark it. The answer itself is already saved
        (dose_taken); this adds the audit row and tells the clinic's page."""
        dose = self.ctx.db.one(
            "SELECT status FROM doses WHERE course_id=? AND dose_no=?", (plan.course_id, dose_no)
        )
        if dose is None:
            raise _CallFailed("The dose for this call was not found.")
        with self.ctx.db.transaction() as conn:
            audit.record(
                conn, f"patient:{plan.course_id}", "dose.patient_reported", "course", plan.course_id,
                {"dose_no": dose_no},
            )  # fmt: skip
        emit(
            self.ctx, [("dose", {"course_id": plan.course_id, "dose_no": dose_no, "status": dose["status"]})]
        )

    @staticmethod
    def _insert_answer(conn: sqlite3.Connection, plan: _Plan, question: str, value: str) -> None:
        conn.execute(
            "INSERT INTO answers(call_id, question, value, at) VALUES (?,?,?,?)",
            (plan.call_id, question, value, clock.iso(clock.now())),
        )

    def _notice_heard(self, live: _Live) -> str:
        """The notice version a consent is recorded against: the prompts version and what was played to the end,
        the short notice, or the short notice and the full one (a notice cut off by a key does not count)."""
        full = "+full" if "FC_NOTICE_FULL" in live.played else ""
        return f"{self.ctx.prompts.version}:short{full}"

    async def _save(self, plan: _Plan, live: _Live, save: fl.Save) -> None:
        clip: _Clip | None = None
        name = ""
        if save.question == "place" and save.value == "recorded":
            if not live.clips or live.clips[-1] not in live.heard:
                raise _CallFailed("The place recording was lost before it could be saved.")
            name = live.clips[-1]
            clip = live.heard[name]
        events: list[tuple[str, dict[str, Any]]] = []
        staged: list[tuple[Path, Path]] = []  # (temporary file, final file) of a clip being saved
        report_id: str | None = None
        try:
            with self.ctx.db.transaction() as conn:
                self._insert_answer(conn, plan, save.question, save.value)
                report_id = self._apply(
                    conn, plan, save, clip, name, events, staged, self._notice_heard(live)
                )
            self._publish_clips(staged)
        except OSError as e:
            self._discard_clips(staged)
            _log_failure(f"call {plan.call_id}: a recording could not be saved", e)
            self.ctx.bus.publish("system", {"check": "storage", "ok": False})
            raise _CallFailed(STORAGE_FAILED) from e
        except BaseException:
            self._discard_clips(staged)
            raise
        emit(self.ctx, events)
        if clip is not None:
            await self._delete_phone_copy(plan, live, name)
            if report_id is not None:
                self._spawn(self._capture(report_id))

    @staticmethod
    def _discard_clips(staged: list[tuple[Path, Path]]) -> None:
        for tmp, _final in staged:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _publish_clips(self, staged: list[tuple[Path, Path]]) -> None:
        """Give each saved clip its real name, after the rows that point to it are committed. If that fails
        the rows are taken back, so no row names a file that is not there."""
        for tmp, final in staged:
            try:
                os.replace(tmp, final)
            except OSError:
                with self.ctx.db.transaction() as conn:
                    rid = final.stem
                    conn.execute("DELETE FROM place_captures WHERE report_id=?", (rid,))
                    conn.execute(
                        "UPDATE bite_reports SET location_status='no_place_heard' WHERE id=?", (rid,)
                    )
                raise

    async def _delete_phone_copy(self, plan: _Plan, live: _Live, name: str) -> None:
        """Our copy under var/clips is the one that is kept; the phone system's is deleted. A failure to
        delete it is raised as a system fault, and the call-end clean-up tries again."""
        try:
            await self.ctx.telephony.delete_recording(name)
        except TelephonyError:
            log.warning("call %s: a saved recording could not be deleted from the phone system", plan.call_id)
            self.ctx.bus.publish("system", {"check": "recording_cleanup", "ok": False})
            return
        live.clips.remove(name)
        live.heard.pop(name, None)

    def _apply(
        self,
        conn: sqlite3.Connection,
        plan: _Plan,
        save: fl.Save,
        clip: _Clip | None,
        name: str,
        events: list[tuple[str, dict[str, Any]]],
        staged: list[tuple[Path, Path]],
        notice: str,
    ) -> str | None:
        """Carry one answer into the course and the bite report. Returns the report id when one changed.
        `notice` is the version of the notice the listener heard, stored with a consent to reminders."""
        q, v, actor = save.question, save.value, f"patient:{plan.course_id}"
        self._check_may_ask(conn, plan, q)
        if q == "lang":
            conn.execute("UPDATE courses SET lang=? WHERE id=?", (v, plan.course_id))
            audit.record(conn, actor, "course.lang", "course", plan.course_id, {"lang": v})
            return None
        if q == "consent_reminders":
            conn.execute(
                "UPDATE courses SET consent_reminders_at=?, notice_version=? WHERE id=?",
                (clock.iso(clock.now()), notice, plan.course_id),
            )
            audit.record(conn, actor, "consent.reminders", "course", plan.course_id)
            return None
        if q == "consent_questions":
            conn.execute("UPDATE courses SET consent_questions=? WHERE id=?", (v, plan.course_id))
            audit.record(conn, actor, "consent.questions", "course", plan.course_id, {"answer": v})
            rid = self._report(conn, plan, "pending" if v == "yes" else "skipped")
        elif q == "place":
            rid = self._report(conn, plan)
            if clip is not None:
                self._store_clip(conn, plan, rid, name, clip, staged)
            else:
                conn.execute("UPDATE bite_reports SET location_status='no_place_heard' WHERE id=?", (rid,))
        elif q in _ANSWER_COLUMNS:
            rid = self._report(conn, plan)
            conn.execute(f"UPDATE bite_reports SET {_ANSWER_COLUMNS[q]}=? WHERE id=?", (v, rid))
            if q == "signs" and v == "yes":
                alert_id = _alert_from_report(self.ctx, conn, rid)
                if alert_id:
                    a = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
                    events.append(
                        ("alert", {"alert_id": a["id"], "status": a["status"], "cluster_id": a["cluster_id"], "ward_no": a["ward_no"]})
                    )  # fmt: skip
        else:
            return None  # dose_today, dose_taken: kept as answers only
        r = conn.execute("SELECT * FROM bite_reports WHERE id=?", (rid,)).fetchone()
        events.append(
            (
                "report",
                {
                    "report_id": rid,
                    "cluster_id": r["cluster_id"],
                    "animal": r["animal"],
                    "location_status": r["location_status"],
                    "simulated": plan.source == "simulator",
                },
            )
        )
        return rid

    @staticmethod
    def _check_may_ask(conn: sqlite3.Connection, plan: _Plan, question: str) -> None:
        """The server's own rule, whatever the flow did: nothing about the bite is kept unless the clinic
        marked the patient an adult (or guardian-consented) and the patient agreed to the questions."""
        if question not in _BITE_QUESTIONS:
            return
        course = conn.execute(
            "SELECT eligibility, consent_questions FROM courses WHERE id=?", (plan.course_id,)
        ).fetchone()
        if course is None or course["eligibility"] not in ("adult", "guardian_consented"):
            raise _CallFailed(
                "An answer about the bite was not kept: the clinic has not marked this patient as an adult "
                "or as having a guardian's consent."
            )
        if question != "consent_questions" and course["consent_questions"] != "yes":
            raise _CallFailed(
                "An answer about the bite was not kept: the patient has not agreed to the questions."
            )

    def _report(self, conn: sqlite3.Connection, plan: _Plan, status: str = "pending") -> str:
        """The course's one bite report, made when first needed."""
        row = conn.execute("SELECT id FROM bite_reports WHERE course_id=?", (plan.course_id,)).fetchone()
        if row:
            return row["id"]
        rid = new_id("rep")
        conn.execute(
            "INSERT INTO bite_reports(id, course_id, clinic_id, bite_week, location_status, source, created_at, is_demo)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rid, plan.course_id, plan.clinic_id, clock.week_start(plan.day0).isoformat(), status, plan.source,
             clock.iso(clock.now()), plan.is_demo),
        )  # fmt: skip
        audit.record(conn, f"patient:{plan.course_id}", "report.create", "report", rid)
        return rid

    def _store_clip(
        self,
        conn: sqlite3.Connection,
        plan: _Plan,
        report_id: str,
        name: str,
        clip: _Clip,
        staged: list[tuple[Path, Path]],
    ) -> None:
        """Write the clip under a temporary name; `_save` gives it its real name once the rows are committed.
        A clip from the browser phone is a visitor's own voice, so it is kept only for policy
        retention.sim_clip_hours."""
        clips = self.ctx.settings.var_dir / "clips"
        clips.mkdir(mode=0o700, parents=True, exist_ok=True)
        final = clips / f"{report_id}.wav"
        tmp = clips / f".{report_id}.{secrets.token_hex(4)}.tmp"
        # a voice clip is personal data: only this system's user may read it
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        staged.append((tmp, final))
        with os.fdopen(fd, "wb") as f:
            f.write(clip.audio)
            f.flush()
            os.fsync(f.fileno())
        keep = self.ctx.policy["retention"]
        age = (
            timedelta(hours=keep["sim_clip_hours"])
            if plan.transport == "sim"
            else timedelta(days=keep["clip_max_age_days"])
        )
        expires = clock.now() + age
        conn.execute(
            "INSERT INTO place_captures(report_id, recording_name, audio_path, voiced_s, asr_status, expires_at)"
            " VALUES (?,?,?,?, 'pending', ?) ON CONFLICT(report_id) DO UPDATE SET recording_name=excluded.recording_name,"
            " audio_path=excluded.audio_path, voiced_s=excluded.voiced_s, asr_status='pending', asr_error=NULL,"
            " transcript=NULL, candidates_json=NULL, confidence=NULL, clip_deleted_at=NULL,"
            " expires_at=excluded.expires_at",
            (report_id, name, str(final), clip.voiced_s, clock.iso(expires)),
        )

    # ------------------------------------------------------------- background
    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _capture(self, report_id: str) -> None:
        """Speech to text and the ward match, off the call and off the event loop."""
        work = asyncio.ensure_future(asyncio.to_thread(_process_capture, self.ctx, report_id))
        self._threads.add(work)
        work.add_done_callback(self._threads.discard)
        try:
            await asyncio.shield(work)  # a cancelled wait leaves the thread running; stop() waits for it
        except Exception as e:
            _log_failure(f"place review failed for {report_id}", e)
            with self.ctx.db.transaction() as conn:
                conn.execute(
                    "UPDATE place_captures SET asr_status='failed', asr_error=? WHERE report_id=? AND asr_status='pending'",
                    (scrub(f"{type(e).__name__}: {e}"), report_id),
                )
            self.ctx.bus.publish("system", {"check": "place_review", "ok": False})

    # ----------------------------------------------------------------- ending
    async def _clean_up(self, plan: _Plan, live: _Live) -> None:
        """Hang up if the person has not, drop recordings nobody kept, and forget the call."""
        port = self.ctx.telephony
        try:
            self._drain_hangup(live)
            if live.port_call_id and live.hangup is None and self._down is None:
                try:
                    await port.hangup(live.port_call_id)
                    await self._await_hangup(live)
                except (TelephonyError, TimeoutError):
                    log.warning("call %s: the hang-up was not confirmed", plan.call_id)
            for name in live.clips:
                try:
                    await port.delete_recording(name)
                except TelephonyError:
                    log.warning("call %s: a recording could not be deleted", plan.call_id)
        finally:
            self._live.pop(plan.call_id, None)
            if live.port_call_id:
                self._by_port.pop(live.port_call_id, None)

    @staticmethod
    def _drain_hangup(live: _Live) -> None:
        while not live.queue.empty():
            item = live.queue.get_nowait()
            if isinstance(item, Hangup):
                live.hangup = item

    async def _await_hangup(self, live: _Live) -> None:
        timeout = self.ctx.policy["calls"]["ring_timeout_s"]
        while live.hangup is None:
            item = await asyncio.wait_for(live.queue.get(), timeout)
            if isinstance(item, Hangup):
                live.hangup = item

    def _set_status(self, call_id: str, status: str, *, transport: str | None = None) -> None:
        with self.ctx.db.transaction() as conn:
            if transport:
                conn.execute("UPDATE calls SET transport=? WHERE id=?", (transport, call_id))
            conn.execute(
                "UPDATE calls SET status=?, started_at=COALESCE(started_at, ?) WHERE id=?",
                (status, clock.iso(clock.now()), call_id),
            )
            c = conn.execute("SELECT course_id, kind, transport FROM calls WHERE id=?", (call_id,)).fetchone()
        emit(self.ctx, [enrol.call_event(call_id, c["course_id"], c["kind"], status, c["transport"])])

    def _finish(
        self,
        call_id: str,
        row: sqlite3.Row,
        status: str,
        error: str | None,
        live: _Live | None = None,
        plan: _Plan | None = None,
    ) -> None:
        h = live.hangup if live else None
        listened = None
        if live and live.answered_at is not None:
            listened = h.duration_s if h else asyncio.get_running_loop().time() - live.answered_at
        cause = scrub(" ".join(str(x) for x in (h.cause_code, h.cause_text) if x)) if h else None
        error = scrub(error)
        with self.ctx.db.transaction() as conn:
            conn.execute(
                "UPDATE calls SET status=?, error=?, ended_at=?, hangup_cause=?, seconds_listened=?,"
                " started_at=COALESCE(started_at, ?) WHERE id=?",
                (
                    status,
                    error,
                    clock.iso(clock.now()),
                    cause or None,
                    listened,
                    clock.iso(clock.now()),
                    call_id,
                ),
            )
            if plan is not None and "FIN_TEAM" in (live.played if live else ()):
                conn.execute(
                    "INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)",
                    (f"team_line:{plan.course_id}", clock.iso(clock.now())),
                )
            transport = plan.transport if plan else row["transport"]
        emit(self.ctx, [enrol.call_event(call_id, row["course_id"], row["kind"], status, transport)])
