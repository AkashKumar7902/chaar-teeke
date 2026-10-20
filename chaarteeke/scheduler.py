"""The scheduler: the one writer that plans, dispatches and retries calls.

One process holds the `locks` row 'scheduler' and refreshes its heartbeat every tick; a second instance
waits. Calls are planned under a unique job key (`kind:<course>:<dose>:<local_date>`), so a restart, a
second tick or a second instance can never create the same call twice. A call is only handed to the
runner inside the calling window (policy.calls.window_*), and a retry is a new row with the suffix
`:a2`, `:a3`. A reminder says "Tomorrow", so it is only placed on the day before its dose and is cancelled
on any later day; the morning-after call says "Yesterday", so it is only placed on the day after. Calls over
the browser phone can overlap; the real line carries at most policy calls.max_parallel_ari calls at once,
and the rest wait their turn as `scheduled` rows. Courses made by the demo seed (source 'demo') are history:
they are never planned, dialled or retried.

The calling window binds the real line only. In demo mode, calls over the browser phone ignore it when policy
calls.window_applies_to_simulator is false, so a visitor can try the phone at any hour; pilot mode and every
real-line call keep the window.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sqlite3
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from . import audit, clock
from .context import AppContext
from .doses import PENDING, emit, refresh_states
from .enrol import PERSON_HUNG_UP, SCHEDULER_ACTOR, call_event, drop_unconsented, new_call
from .privacy import BackupError, PurgeReport, backup_database, run_purges
from .telephony.model import BROWSER_PHONE_GONE

log = logging.getLogger(__name__)

LOCK_NAME = "scheduler"
_RETRY_SUFFIX = re.compile(r":a\d+$")
_IN_FLIGHT = ("dialing", "ringing", "answered")
_LINKED_TRANSPORT = (
    "(SELECT transport FROM missed_calls m WHERE m.course_id = c.id AND m.status='linked' LIMIT 1)"
)
PARALLEL_TRANSPORT = (
    "sim"  # browser phones; every other line (or an unknown one) is capped by calls.max_parallel_ari
)


class TickReport(BaseModel):
    ran: bool
    holder: str
    refreshed: int = 0
    planned: int = 0
    retries: int = 0
    dispatched: int = 0
    cancelled: int = 0
    failed: int = 0
    purge: PurgeReport | None = None
    errors: list[str] = []


@dataclass(frozen=True)
class NextCall:
    at: datetime
    kind: str
    course_id: str
    dose_no: int | None


@dataclass
class _Work:
    events: list = field(default_factory=list)
    planned: int = 0
    retries: int = 0
    cancelled: int = 0


# ------------------------------------------------------------- calling window
def _window(ctx: AppContext, day: date) -> tuple[datetime, datetime]:
    pol = ctx.policy["calls"]
    tz = ctx.settings.local_tz
    return clock.at_local(day, pol["window_start_local"], tz), clock.at_local(
        day, pol["window_end_local"], tz
    )


def in_window(ctx: AppContext, when: datetime) -> bool:
    start, end = _window(ctx, when.astimezone(clock.local_tz(ctx.settings.local_tz)).date())
    return start <= when < end


def window_exempt(ctx: AppContext, transport: str | None) -> bool:
    """True for a browser-phone call in demo mode when the policy lets the simulator ignore the window."""
    return (
        ctx.settings.is_demo
        and transport == PARALLEL_TRANSPORT
        and not ctx.policy["calls"]["window_applies_to_simulator"]
    )


def next_window_time(ctx: AppContext, when: datetime) -> datetime:
    """`when` itself if calls are allowed then, else the next moment the window opens."""
    day = when.astimezone(clock.local_tz(ctx.settings.local_tz)).date()
    start, end = _window(ctx, day)
    if when < start:
        return start
    if when >= end:
        return _window(ctx, day + timedelta(days=1))[0]
    return when


def _planned_at(ctx: AppContext, day: date, key: str) -> datetime:
    return clock.at_local(day, ctx.policy["calls"][key], ctx.settings.local_tz)


# ------------------------------------------------------------------ next call
def next_call(ctx: AppContext) -> NextCall | None:
    """The next call the system will place, counting the window, for the demo clock's 'jump'."""
    now = clock.now()
    best: NextCall | None = None

    def consider(at: datetime, kind: str, course_id: str, dose_no: int | None, transport: str | None) -> None:
        nonlocal best
        at = max(at, now)
        if not window_exempt(ctx, transport):
            at = next_window_time(ctx, at)
        if best is None or at < best.at:
            best = NextCall(at, kind, course_id, dose_no)

    for c in ctx.db.query(
        "SELECT course_id, kind, dose_no, scheduled_for, transport FROM calls WHERE status='scheduled'"
        " AND NOT EXISTS (SELECT 1 FROM courses c WHERE c.id = calls.course_id AND c.source='demo')"
    ):
        consider(clock.parse_iso(c["scheduled_for"]), c["kind"], c["course_id"], c["dose_no"], c["transport"])
    for d in ctx.db.query(
        "SELECT d.course_id, d.dose_no, d.due_date, " + _LINKED_TRANSPORT + " AS transport"
        " FROM doses d JOIN courses c ON c.id = d.course_id"
        " JOIN contacts ct ON ct.course_id = c.id"
        " WHERE c.status='active' AND c.source != 'demo' AND c.consent_reminders_at IS NOT NULL"
        " AND d.dose_no > 1 AND d.status IN ('due','late_minor','late_review')"
    ):
        due = date.fromisoformat(d["due_date"])
        for kind, day, key in (
            ("reminder", due - timedelta(days=1), "reminder_local_time"),
            ("missed_dose", due + timedelta(days=1), "morning_after_local_time"),
        ):
            at = _planned_at(ctx, day, key)
            if at > now:
                consider(at, kind, d["course_id"], d["dose_no"], d["transport"])
    return best


def next_call_at(ctx: AppContext) -> datetime | None:
    nxt = next_call(ctx)
    return nxt.at if nxt else None


# ------------------------------------------------------------------ the class
class Scheduler:
    def __init__(
        self,
        ctx: AppContext,
        holder: str | None = None,
        *,
        loop_seconds: float | None = None,
        lock_stale_seconds: float | None = None,
        purge_every_seconds: float | None = None,
    ) -> None:
        """The loop, lock and purge timings come from policy.scheduler; the arguments override them (tests)."""
        calls, timing = ctx.policy["calls"], ctx.policy["scheduler"]
        by_kind = calls.get("retry_gaps_min_by_kind", {})
        for name, gaps in (("retry_gaps_min", calls["retry_gaps_min"]), *by_kind.items()):
            if len(gaps) < calls["max_attempts"] - 1:
                raise ValueError(f"policy calls.{name} needs one gap for every retry (max_attempts - 1)")
        self.ctx = ctx
        self.holder = holder or f"sched-{uuid.uuid4().hex[:8]}"
        self.loop_seconds = timing["loop_s"] if loop_seconds is None else loop_seconds
        self.lock_stale = timedelta(
            seconds=timing["lock_stale_s"] if lock_stale_seconds is None else lock_stale_seconds
        )
        self.purge_every = timedelta(
            seconds=timing["purge_every_s"] if purge_every_seconds is None else purge_every_seconds
        )
        self.max_parallel_ari = calls["max_parallel_ari"]
        self._tasks: set[asyncio.Task] = set()
        self._busy_lines: Counter[str] = Counter()
        self._holding = False
        self._stopping = False

    # -- lock
    def _acquire(self) -> bool:
        now = clock.now()
        with self.ctx.db.transaction() as conn:
            row = conn.execute("SELECT * FROM locks WHERE name=?", (LOCK_NAME,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO locks(name, holder, heartbeat_at) VALUES (?,?,?)",
                    (LOCK_NAME, self.holder, clock.iso(now)),
                )
                took_over = True
            elif row["holder"] == self.holder:
                conn.execute("UPDATE locks SET heartbeat_at=? WHERE name=?", (clock.iso(now), LOCK_NAME))
                took_over = not self._holding
            elif abs(now - clock.parse_iso(row["heartbeat_at"])) > self.lock_stale:
                # A heartbeat far in the future is as dead as one far in the past: the clock that wrote it
                # (a demo jump, a wrong date) is gone, and waiting for the real time to catch up could
                # stop every call for days.
                log.warning(
                    "scheduler %s takes over the lock from %s: its heartbeat is %s from now",
                    self.holder,
                    row["holder"],
                    "ahead" if clock.parse_iso(row["heartbeat_at"]) > now else "behind",
                )
                conn.execute(
                    "UPDATE locks SET holder=?, heartbeat_at=? WHERE name=?",
                    (self.holder, clock.iso(now), LOCK_NAME),
                )
                audit.record(
                    conn, SCHEDULER_ACTOR, "scheduler.takeover", "lock", LOCK_NAME, {"from": row["holder"]}
                )
                took_over = True
            else:
                self._holding = False
                return False
            self._holding = True
            events = self._fail_interrupted(conn) if took_over else []
        emit(self.ctx, events)
        return True

    def _fail_interrupted(self, conn: sqlite3.Connection) -> list:
        """Calls that were mid-flight when the last writer died cannot be finished: close them as failed."""
        live = {self._tasks_call_id(t) for t in self._tasks}
        events = []
        for c in conn.execute(
            f"SELECT id, course_id, kind, transport FROM calls WHERE status IN ({','.join('?' * len(_IN_FLIGHT))})",
            _IN_FLIGHT,
        ).fetchall():
            if c["id"] in live:
                continue
            conn.execute(
                "UPDATE calls SET status='failed', ended_at=?, error=? WHERE id=?",
                (clock.iso(clock.now()), "The system restarted during this call", c["id"]),
            )
            audit.record(conn, SCHEDULER_ACTOR, "call.interrupted", "call", c["id"])
            events.append(call_event(c["id"], c["course_id"], c["kind"], "failed", c["transport"]))
        return events

    @staticmethod
    def _tasks_call_id(task: asyncio.Task) -> str | None:
        return task.get_name().removeprefix("call:") if task.get_name().startswith("call:") else None

    def release(self) -> None:
        with self.ctx.db.transaction() as conn:
            conn.execute("DELETE FROM locks WHERE name=? AND holder=?", (LOCK_NAME, self.holder))
        self._holding = False

    # -- the tick
    async def tick(self, *, wait: bool = True) -> TickReport:
        """One pass. With wait=False the placed calls keep running in the background (run_forever)."""
        report = TickReport(ran=False, holder=self.holder)
        ctx = self.ctx
        try:
            if not self._acquire():
                return report
            report.ran = True
            report.refreshed = refresh_states(ctx)
            work = _Work()
            with ctx.db.transaction() as conn:
                self._plan(conn, work)
                self._plan_retries(conn, work)
                self._expire_unconsented(conn, work)
            emit(ctx, work.events)
            report.planned, report.retries, report.cancelled = work.planned, work.retries, work.cancelled
            await self._dispatch(report, wait)
            if self._purge_due():
                report.purge = run_purges(ctx)
                report.errors.extend(report.purge.errors)
            self._backup(report)
        except Exception as e:  # one bad pass (even a locked database) must not stop the next one
            log.exception("scheduler tick failed")
            report.errors.append(f"{type(e).__name__}: {e}")
        return report

    def _purge_due(self) -> bool:
        row = self.ctx.db.one("SELECT value FROM kv WHERE key='last_purge_run'")
        if row is None:
            return True
        last, now = clock.parse_iso(row["value"]), clock.now()
        # a run stamped in the future came from a clock that has since moved back: it proves nothing
        return last > now or last + self.purge_every <= now

    def _backup(self, report: TickReport) -> None:
        """Once a local day, after the purge time, when CT_BACKUP_DIR is set: an encrypted copy of the database."""
        ctx = self.ctx
        if ctx.settings.backup_dir is None:
            return
        today = clock.local_today(ctx.settings.local_tz)
        if clock.now() < clock.at_local(
            today, ctx.policy["retention"]["purge_local_time"], ctx.settings.local_tz
        ):
            return
        done = ctx.db.one("SELECT value FROM kv WHERE key='last_backup_day'")
        if done is not None and done["value"] == today.isoformat():
            return
        try:
            backup_database(ctx)
        except BackupError as e:
            report.errors.append(str(e))
            ctx.bus.publish("system", {"check": "backup", "ok": False})
            return
        with ctx.db.transaction() as conn:
            conn.execute(
                "INSERT INTO kv(key, value) VALUES ('last_backup_day', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (today.isoformat(),),
            )
        ctx.bus.publish("system", {"check": "backup", "ok": True})

    # -- planning
    def _plan(self, conn: sqlite3.Connection, work: _Work) -> None:
        ctx = self.ctx
        today = clock.local_today(ctx.settings.local_tz)
        rows = conn.execute(
            "SELECT d.course_id, d.dose_no, d.due_date, c.is_demo, " + _LINKED_TRANSPORT + " AS transport"
            " FROM doses d JOIN courses c ON c.id = d.course_id JOIN contacts ct ON ct.course_id = c.id"
            " WHERE c.status='active' AND c.source != 'demo' AND c.consent_reminders_at IS NOT NULL"
            " AND d.dose_no > 1 AND d.status IN ('due','late_minor','late_review')"
            " AND (ct.purge_after IS NULL OR ct.purge_after > ?)",
            (clock.iso(clock.now()),),
        ).fetchall()
        for r in rows:
            due = date.fromisoformat(r["due_date"])
            plans = []
            if today == due - timedelta(days=1):  # the message says "Tomorrow": it is only true on that day
                plans.append(("reminder", due - timedelta(days=1), "reminder_local_time"))
            if today == due + timedelta(days=1):
                plans.append(("missed_dose", today, "morning_after_local_time"))
            for kind, day, key in plans:
                call_id = new_call(
                    conn,
                    course_id=r["course_id"],
                    kind=kind,
                    job_key=f"{kind}:{r['course_id']}:{r['dose_no']}:{day.isoformat()}",
                    scheduled_for=_planned_at(ctx, day, key),
                    dose_no=r["dose_no"],
                    transport=r["transport"],
                    is_demo=r["is_demo"],
                )
                if call_id:
                    work.planned += 1
                    work.events.append(call_event(call_id, r["course_id"], kind, "scheduled", r["transport"]))

    def _plan_retries(self, conn: sqlite3.Connection, work: _Work) -> None:
        pol = self.ctx.policy["calls"]
        failed = conn.execute(
            "SELECT * FROM calls WHERE status IN ('no_answer','failed') AND attempt < ?"
            " AND NOT EXISTS (SELECT 1 FROM courses c WHERE c.id = calls.course_id AND c.source='demo')",
            (pol["max_attempts"],),
        ).fetchall()
        for c in failed:
            if c["transport"] == "sim" and c["hangup_cause"] == BROWSER_PHONE_GONE:
                continue  # the page is closed: another ring would reach nobody
            if c["kind"] != "duplicate_notice" and not self._course_active(conn, c["course_id"]):
                continue
            base = _RETRY_SUFFIX.sub("", c["job_key"])
            gaps = pol.get("retry_gaps_min_by_kind", {}).get(c["kind"], pol["retry_gaps_min"])
            gap = timedelta(minutes=gaps[c["attempt"] - 1])
            last = clock.parse_iso(c["ended_at"] or c["scheduled_for"])
            call_id = new_call(
                conn,
                course_id=c["course_id"],
                kind=c["kind"],
                job_key=f"{base}:a{c['attempt'] + 1}",
                scheduled_for=last + gap,
                dose_no=c["dose_no"],
                attempt=c["attempt"] + 1,
                transport=c["transport"],
                is_demo=c["is_demo"],
            )
            if call_id:
                work.retries += 1
                work.events.append(
                    call_event(call_id, c["course_id"], c["kind"], "scheduled", c["transport"])
                )

    def _expire_unconsented(self, conn: sqlite3.Connection, work: _Work) -> None:
        """A patient who never agreed to reminders does not keep a number on file. When every attempt at the
        first call is spent, and it was the person (or the line to a closed browser phone) that stopped it,
        the number is removed and the clinic is told on its list. A fault of ours (a missing voice file, a
        broken line) never costs a patient their number."""
        max_attempts = self.ctx.policy["calls"]["max_attempts"]
        waiting = conn.execute(
            "SELECT c.id FROM courses c JOIN contacts ct ON ct.course_id = c.id"
            " WHERE c.status='active' AND c.source != 'demo' AND c.consent_reminders_at IS NULL"
            " AND EXISTS (SELECT 1 FROM calls k WHERE k.course_id = c.id AND k.kind='first')"
            " AND NOT EXISTS (SELECT 1 FROM calls k WHERE k.course_id = c.id AND k.kind='first'"
            "   AND k.status IN ('scheduled','dialing','ringing','answered','completed'))"
        ).fetchall()
        for w in waiting:
            last = conn.execute(
                "SELECT * FROM calls WHERE course_id=? AND kind='first' ORDER BY scheduled_for DESC, attempt DESC",
                (w["id"],),
            ).fetchone()
            by_person = last["status"] == "no_answer" or (
                last["status"] == "failed" and last["error"] == PERSON_HUNG_UP
            )
            gone = last["transport"] == "sim" and last["hangup_cause"] == BROWSER_PHONE_GONE
            if by_person and (last["attempt"] >= max_attempts or gone):
                if drop_unconsented(self.ctx, conn, w["id"], work.events):
                    work.cancelled += 1

    @staticmethod
    def _course_active(conn: sqlite3.Connection, course_id: str | None) -> bool:
        row = conn.execute("SELECT status FROM courses WHERE id=?", (course_id,)).fetchone()
        return row is not None and row["status"] == "active"

    # -- dispatch
    async def _dispatch(self, report: TickReport, wait: bool) -> None:
        ctx = self.ctx
        started: list[asyncio.Task] = []
        while True:
            now = clock.now()
            open_now = in_window(ctx, now)
            due = [
                row
                for row in ctx.db.query(
                    "SELECT id, transport FROM calls WHERE status='scheduled' AND scheduled_for <= ?"
                    " AND NOT EXISTS (SELECT 1 FROM courses c WHERE c.id = calls.course_id AND c.source='demo')"
                    " ORDER BY scheduled_for, id",
                    (clock.iso(now),),
                )
                if open_now or window_exempt(ctx, row["transport"])
            ]
            if due and ctx.runner is None:
                report.errors.append("No call runner is attached, so scheduled calls were not placed.")
                return
            began: list[asyncio.Task] = []
            for row in due:
                line = row["transport"] or "?"
                if line != PARALLEL_TRANSPORT and self._busy_lines[line] >= self.max_parallel_ari:
                    continue  # the line is full: this call waits as 'scheduled' for its turn
                verdict = self._claim(row["id"], now)
                if verdict == "claimed":
                    began.append(self._start(row["id"], line))
                    report.dispatched += 1
                elif verdict == "cancelled":
                    report.cancelled += 1
            started += began
            if not (wait and began):
                break
            await self._await_calls(began)
        report.failed = sum(1 for t in started if t.done() and not t.cancelled() and t.result() is False)

    def _start(self, call_id: str, line: str) -> asyncio.Task:
        task = asyncio.ensure_future(self._place(call_id))
        task.set_name(f"call:{call_id}")
        self._tasks.add(task)
        if line != PARALLEL_TRANSPORT:
            self._busy_lines[line] += 1

        def finished(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if line != PARALLEL_TRANSPORT:
                self._busy_lines[line] -= 1

        task.add_done_callback(finished)
        return task

    async def _await_calls(self, tasks: list[asyncio.Task]) -> None:
        """Wait for placed calls, keeping the lock's heartbeat fresh however long they last."""
        pending = set(tasks)
        while pending:
            _, pending = await asyncio.wait(pending, timeout=self.loop_seconds)
            if pending:
                self._acquire()

    def _claim(self, call_id: str, now: datetime) -> str:
        """Move one call from scheduled to dialing, or cancel it when it no longer makes sense."""
        ctx = self.ctx
        events: list = []
        with ctx.db.transaction() as conn:
            c = conn.execute("SELECT * FROM calls WHERE id=? AND status='scheduled'", (call_id,)).fetchone()
            if c is None:
                return "gone"
            why = self._stale_reason(conn, c, now)
            if why:
                conn.execute("UPDATE calls SET status='cancelled', error=? WHERE id=?", (why, call_id))
                audit.record(conn, SCHEDULER_ACTOR, "call.cancel", "call", call_id, {"why": why})
                events.append(call_event(call_id, c["course_id"], c["kind"], "cancelled", c["transport"]))
                verdict = "cancelled"
            else:
                conn.execute(
                    "UPDATE calls SET status='dialing', started_at=? WHERE id=? AND status='scheduled'",
                    (clock.iso(now), call_id),
                )
                events.append(call_event(call_id, c["course_id"], c["kind"], "dialing", c["transport"]))
                verdict = "claimed"
        emit(ctx, events)
        return verdict

    def _stale_reason(self, conn: sqlite3.Connection, call: sqlite3.Row, now: datetime) -> str | None:
        """Plain words for why this call must not be placed now, or None when it is fine."""
        today = clock.local_today(self.ctx.settings.local_tz)
        course = conn.execute("SELECT status FROM courses WHERE id=?", (call["course_id"],)).fetchone()
        kind = call["kind"]
        contact = conn.execute(
            "SELECT purge_after FROM contacts WHERE course_id=?", (call["course_id"],)
        ).fetchone()
        if course is None or contact is None:
            return "the phone number was removed"
        if contact["purge_after"] and clock.parse_iso(contact["purge_after"]) <= now:
            return "the phone number is past its keep time"  # the purge job may not have run yet
        if kind == "duplicate_notice":
            return None if course["status"] == "active" else "the course is no longer active"
        if course["status"] != "active":
            return "the course is no longer active"
        if kind in ("reminder", "missed_dose"):
            dose = conn.execute(
                "SELECT status, due_date FROM doses WHERE course_id=? AND dose_no=?",
                (call["course_id"], call["dose_no"]),
            ).fetchone()
            if dose is None or dose["status"] not in PENDING:
                return "the dose was already marked as given"
            due = date.fromisoformat(dose["due_date"])
            # the reminder says "Tomorrow, <date>" and the morning-after call says "Yesterday, <date>"
            last_day = due - timedelta(days=1) if kind == "reminder" else due + timedelta(days=1)
            if today > last_day:
                return "the day for this call has passed"
        return None

    async def _place(self, call_id: str) -> bool:
        """Hand one claimed call to the runner. Returns False when it failed; never raises."""
        try:
            result = self.ctx.runner.place(call_id)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as e:  # one bad call must not stop the others
            # not log.exception: the message can echo the number that was dialled, and no log line may carry one
            log.error(
                "placing call %s failed (%s)\n%s",
                call_id,
                type(e).__name__,
                "".join(traceback.format_tb(e.__traceback__)),
            )
            # the exception text may carry a number or a path, so it stays in the log and not on the clinic's screen
            self._mark_failed(call_id, f"The call stopped on an internal error ({type(e).__name__}).")
            return False

    def _mark_failed(self, call_id: str, error: str) -> None:
        events: list = []
        with self.ctx.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE calls SET status='failed', ended_at=?, error=? WHERE id=? AND status IN"
                " ('scheduled','dialing','ringing','answered')",
                (clock.iso(clock.now()), error, call_id),
            ).rowcount
            if changed:
                c = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
                audit.record(conn, SCHEDULER_ACTOR, "call.failed", "call", call_id, {"error": error})
                events.append(call_event(call_id, c["course_id"], c["kind"], "failed", c["transport"]))
        emit(self.ctx, events)

    # -- running
    async def wait_idle(self) -> None:
        """Wait for calls handed over by tick(wait=False) to finish."""
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def run_forever(self) -> None:
        try:
            while not self._stopping:
                await self.tick(wait=False)
                await asyncio.sleep(self.loop_seconds)
        finally:
            await self.wait_idle()
            self.release()

    def stop(self) -> None:
        self._stopping = True
