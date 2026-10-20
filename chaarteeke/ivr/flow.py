"""Call flows as pure state machines. No I/O, no clock, no database.

A flow is fed events (`step(event)`) and answers with actions that the runner carries out in order:

    events   Start, Key(digit), NoInput, Recorded(name, ok, talking_s, voiced_s), PlayDone(ok), Hangup
             (Key("9") is also the answer to a Record that was interrupted by that key)
    actions  Play(prompts), Collect(valid_keys, timeout_s), Record(max_s, silence_s), Save(question, value),
             Withdraw(reason), MarkTaken(dose_no), End(reason)

A step ends at the first Collect or Record (the runner then waits for the person) or at End. The runner
reports `PlayDone` only when a playback failed; a played prompt needs no event. Key 9 at any question
asks "stop the calls?" (OPT_CONFIRM) and only a second 9 withdraws. The first call's notice is layered: a short
notice (FC_NOTICE) names every item; key 0 at the consent question plays the full one (FC_NOTICE_FULL) and asks
again, up to policy calls.full_notice_max_plays times, after which 0 is a wrong key. The flow says nothing about
which version was heard; the runner reads that from what was played to the end. Silence and a wrong key each count as
one of the tries a question gets (policy calls.no_input_repeats repeats, then the give-up prompt).
A question that gets no answer is never saved, so the next call asks it again.

What the first call reads out at the end comes from the dose table (the next dose still to come), never from
anything the patient pressed. A reminder or morning-after call says nothing about the clinic, a dose or a ward
until the listener presses 1 to say they are the person who gave the missed call.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..policy import Policy

# ----------------------------------------------------------------------- events


@dataclass(frozen=True)
class Start:
    pass


@dataclass(frozen=True)
class Key:
    digit: str


@dataclass(frozen=True)
class NoInput:
    pass


@dataclass(frozen=True)
class Recorded:
    """A recording finished. `talking_s` is the phone system's own figure (whole seconds, information only);
    `voiced_s` is what the app measured in the audio it fetched (chaarteeke/ivr/voiced.py), and it alone
    decides whether the answer holds speech. It is None only when nothing was recorded (ok is False)."""

    name: str
    ok: bool
    talking_s: float | None
    voiced_s: float | None = None


@dataclass(frozen=True)
class PlayDone:
    ok: bool


@dataclass(frozen=True)
class Hangup:
    pass


Event = Start | Key | NoInput | Recorded | PlayDone | Hangup


# ---------------------------------------------------------------------- actions


@dataclass(frozen=True)
class Say:
    """One prompt with its slot values. `lang` fixes a language (the language menu); else the call's."""

    id: str
    slots: Mapping[str, Any] = field(default_factory=dict)
    lang: str | None = None


@dataclass(frozen=True)
class Play:
    """Play prompts in order. `listen`: a key pressed while they play counts as the answer that follows.
    `keep`: keys (besides 9) that are held when pressed during a prompt that asks nothing, so the question
    that comes next sees them."""

    prompts: tuple[Say, ...]
    listen: bool = False
    keep: str = ""

    @property
    def ids(self) -> list[str]:
        return [s.id for s in self.prompts]


@dataclass(frozen=True)
class Collect:
    valid_keys: str
    timeout_s: float


@dataclass(frozen=True)
class Record:
    max_s: int
    silence_s: int


@dataclass(frozen=True)
class Save:
    question: str
    value: str


@dataclass(frozen=True)
class Withdraw:
    reason: str  # declined | withdrawn | wrong_number


@dataclass(frozen=True)
class MarkTaken:
    """The patient says they took the dose. It is theirs to say; only the clinic marks a dose given."""

    dose_no: int


@dataclass(frozen=True)
class End:
    reason: str  # completed | declined | withdrawn | wrong_number | gave_up | hangup | playback_failed


Action = Play | Collect | Record | Save | Withdraw | MarkTaken | End


@dataclass(frozen=True)
class DoseInfo:
    dose_no: int
    due: date


# --------------------------------------------------------------------- the maps
# The first call's answers and what is stored (bite_reports): animal notched | unnotched | unsure | pet |
# other | animal_unknown, signs yes | no | dont_know. A question nobody answered stays no_answer.
LANG_BY_KEY = {"1": "hi", "2": "doi", "3": "ur"}
FULL_NOTICE_KEY = "0"  # at the consent question: hear the full notice
PLACE_TYPE_BY_KEY = {"1": "home_street", "2": "school", "3": "hospital", "4": "bus_market", "5": "other"}
ANIMAL_BY_KEY = {"2": "pet", "3": "other", "4": "animal_unknown"}  # key 1 (street dog) asks FC_NOTCH next
NOTCH_BY_KEY = {"1": "notched", "2": "unnotched", "3": "unsure"}
SIGNS_BY_KEY = {"1": "yes", "2": "no", "3": "dont_know"}

# Every prompt id a flow can play. Checked against prompts.json, and a test pins it to the flows below.
REQUIRED_PROMPTS = frozenset(
    {
        "SYS_LANG", "SYS_NOINPUT", "SYS_WRONGKEY", "SYS_GIVEUP_FIRST", "SYS_GIVEUP_MISS", "SYS_BYE",
        "FC_HELLO", "FC_NOTICE", "FC_NOTICE_FULL", "FC_CONSENT_Q", "FC_CONSENT_Q_G", "FC_GUARD_OK", "FC_CONSENT_NO",
        "FC_QCONSENT", "FC_PLACE", "FC_PLACE_AGAIN", "FC_PLACETYPE", "FC_ANIMAL", "FC_NOTCH", "FC_SICK",
        "FC_SICK_YES", "FC_CLOSE", "FC_CLOSE_COME", "FC_CLOSE_LAST",
        "RM_GATE", "RM_HELLO", "RM_WHO", "RM_EVE_BODY", "RM_EVE_BODY_OUT", "RM_EVE_END", "FIN_TEAM",
        "RM_MISS_Q", "RM_MISS_TAKEN", "RM_MISS_TAKEN_LAST", "RM_MISS_NOT",
        "WN_CONFIRM", "WN_DONE", "OPT_CONFIRM", "OPT_DONE", "ENR_DUP",
    }
)  # fmt: skip


class _Stop(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


Script = Generator[list[Action], Event | None, str]


def _say(*ids: str, **slots: Any) -> Play:
    return Play(tuple(Say(i, slots) for i in ids))


def _who(register: str | None) -> list[Say]:
    """On a phone several patients share, say whose register entry this call is about."""
    return [Say("RM_WHO", {"register": register})] if register else []


class Flow:
    """Base class: runs a generator script, one group of actions per yield."""

    essential: frozenset[str] = frozenset()  # hearing any of these means the message was delivered

    def __init__(self, policy: Policy, lang: str) -> None:
        calls = policy["calls"]
        self.lang = lang
        self.done = False
        self._wait = calls["no_input_wait_s"]
        self._repeats = calls["no_input_repeats"]
        self._gen: Script | None = None

    @property
    def langs(self) -> tuple[str, ...]:
        """The languages this call may speak, so the runner can check their recorded voice first."""
        return (self.lang,)

    def _script(self) -> Script:
        raise NotImplementedError

    def step(self, event: Event) -> list[Action]:
        if self.done:
            return []
        if isinstance(event, Hangup):
            return self._finish([], "hangup")
        if isinstance(event, PlayDone):
            return [] if event.ok else self._finish([], "playback_failed")
        out: list[Action] = []
        try:
            if self._gen is None:
                if not isinstance(event, Start):
                    raise ValueError("a flow begins with Start")
                self._gen = self._script()
                group = next(self._gen)
            else:
                group = self._gen.send(event)
            while True:
                out.extend(group)
                if group and isinstance(group[-1], Collect | Record):
                    return out
                group = self._gen.send(None)
        except _Stop as stop:
            return self._finish(out, stop.reason)
        except StopIteration as end:
            return self._finish(out, end.value or "completed")

    def _finish(self, out: list[Action], reason: str) -> list[Action]:
        self.done = True
        return [*out, End(reason)]

    def _gate(self) -> Generator[list[Action], Event | None, None]:
        """Say nothing about the clinic, a dose or a ward until the listener presses 1 (they are the person who
        gave the missed call). Key 8 says the call is not for them; silence or other keys end it politely.
        Key 9 does not stop the calls here: nobody has said who they are yet."""
        yield from self._ask(
            [Say("RM_GATE")], [Say("RM_GATE")], "1", giveup="SYS_BYE", withdraw=False, wrong_number=True
        )

    # -- asking
    def _ask(
        self,
        first: Sequence[Say],
        again: Sequence[Say],
        keys: str,
        *,
        giveup: str | None,
        giveup_reason: str = "gave_up",
        withdraw: bool = True,
        wrong_number: bool = False,
    ) -> Generator[list[Action], Event | None, str]:
        """Ask until a key in `keys` is pressed. Returns that key. Silence and wrong keys use up tries."""
        extra = ("9" if withdraw else "") + ("8" if wrong_number else "")
        prompts, lead, failures = list(first), [], 0
        while True:
            event = yield [Play(tuple(lead + prompts), listen=True), Collect(keys + extra, self._wait)]
            prompts, lead = list(again), []
            if isinstance(event, Key):
                if event.digit in keys:
                    return event.digit
                if withdraw and event.digit == "9":
                    yield from self._confirm(
                        "OPT_CONFIRM", "9", "OPT_DONE", "withdrawn", "withdrawn", giveup, giveup_reason
                    )
                    continue
                if wrong_number and event.digit == "8":
                    yield from self._confirm(
                        "WN_CONFIRM", "8", "WN_DONE", "wrong_number", "wrong_number", giveup, giveup_reason
                    )
                    continue
                lead = [Say("SYS_WRONGKEY")]
            elif isinstance(event, NoInput):
                lead = [Say("SYS_NOINPUT")]
            else:
                raise ValueError(f"expected a key or silence, got {event!r}")
            failures += 1
            if failures > self._repeats:
                if giveup:
                    yield [Play((Say(giveup),))]
                raise _Stop(giveup_reason)

    def _confirm(
        self,
        ask: str,
        confirm_key: str,
        done: str,
        reason: str,
        end: str,
        giveup: str | None,
        giveup_reason: str,
    ) -> Generator[list[Action], Event | None, None]:
        """A stop needs a second press: only `confirm_key` stops the calls, 1 keeps them."""
        key = yield from self._ask(
            [Say(ask)],
            [Say(ask)],
            confirm_key + "1",
            giveup=giveup,
            giveup_reason=giveup_reason,
            withdraw=False,
        )
        if key == confirm_key:
            yield [Withdraw(reason), Play((Say(done),))]
            raise _Stop(end)


# --------------------------------------------------------------------- first call


class FirstCallFlow(Flow):
    """Notice and consent, then (when the clinic allows) the questions, then the next dose date.

    `next_dose` is the first dose of the course that is not yet given, read from the dose table by the runner
    (None: none left). `guardian`: the bitten person is under 18 and the clinic has marked a guardian's
    agreement, so the consent question is put to the parent or guardian."""

    essential = frozenset({"FC_CLOSE", "FC_CLOSE_COME", "FC_CLOSE_LAST"})

    def __init__(
        self,
        *,
        policy: Policy,
        lang: str,
        clinic_id: str,
        next_dose: DoseInfo | None,
        total: int,
        visit_time: str,
        today: date,
        ask_questions: bool,
        clinic_phone: str | None = None,
        guardian: bool = False,
        ask_language: bool = False,
        prior: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(policy, lang)
        calls = policy["calls"]
        if next_dose is not None and next_dose.due < today and not clinic_phone:
            raise ValueError("a first call whose next dose date has passed needs the clinic's phone number")
        self._record = (calls["place_record_max_s"], calls["place_record_silence_s"])
        self._speech_min_voiced_s = calls["speech_min_voiced_s"]
        self._full_notice_plays = calls["full_notice_max_plays"]
        self._clinic = clinic_id
        self._next = next_dose
        self._total = total
        self._visit_time = visit_time
        self._today = today
        self._ask_questions = ask_questions
        self._clinic_phone = clinic_phone
        self._guardian = guardian
        self._ask_language = ask_language
        self._prior = dict(prior or {})

    @property
    def langs(self) -> tuple[str, ...]:
        return (
            tuple(LANG_BY_KEY.values()) if self._ask_language and "lang" not in self._prior else (self.lang,)
        )

    def _again(self, prompt: str) -> tuple[list[Say], list[Say]]:
        return [Say(prompt)], [Say(prompt)]

    def _q(self, prompt: str, keys: str) -> Generator[list[Action], Event | None, str]:
        return self._ask(*self._again(prompt), keys, giveup="SYS_GIVEUP_FIRST")

    def _consent(self) -> Generator[list[Action], Event | None, str]:
        """Ask for consent. Key 0 plays the full notice and then asks again, as often as the policy allows; after
        that 0 is a wrong key like any other. Returns 1 or 2."""
        prompt = "FC_CONSENT_Q_G" if self._guardian else "FC_CONSENT_Q"
        heard = 0
        while True:
            more = FULL_NOTICE_KEY if heard < self._full_notice_plays else ""
            key = yield from self._ask([Say(prompt)], [Say(prompt)], "12" + more, giveup="SYS_GIVEUP_FIRST")
            if key != FULL_NOTICE_KEY:
                return key
            heard += 1
            yield [_say("FC_NOTICE_FULL")]

    def _script(self) -> Script:
        p = self._prior
        if self._ask_language and "lang" not in p:
            menu = [Say("SYS_LANG", lang=code) for code in LANG_BY_KEY.values()]
            key = yield from self._ask(menu, menu, "".join(LANG_BY_KEY), giveup="SYS_GIVEUP_FIRST")
            self.lang = LANG_BY_KEY[key]
            yield [Save("lang", self.lang)]
        yield [_say("FC_HELLO", clinic=self._clinic)]
        if p.get("consent_reminders") != "yes":
            yield [Play((Say("FC_NOTICE"),), keep=FULL_NOTICE_KEY)]
            if (yield from self._consent()) == "2":
                yield [Withdraw("declined"), _say("FC_CONSENT_NO")]
                raise _Stop("declined")
            yield [Save("consent_reminders", "yes")]
        consent = p.get("consent_questions")
        if self._ask_questions and consent is None:
            if self._guardian:
                yield [_say("FC_GUARD_OK")]
            consent = "yes" if (yield from self._q("FC_QCONSENT", "12")) == "1" else "no"
            yield [Save("consent_questions", consent)]
        if self._ask_questions and consent == "yes":
            yield from self._questions()
        yield [self._close()]
        return "completed"

    def _close(self) -> Play:
        """The next dose is whatever the dose table says is still to come. Nothing the patient pressed decides
        it, and a date that has already passed is never read out as the next one."""
        nxt = self._next
        if nxt is None:
            return _say("FC_CLOSE_LAST")
        if nxt.due < self._today:
            return _say("FC_CLOSE_COME", clinic=self._clinic, clinic_phone=self._clinic_phone)
        return _say(
            "FC_CLOSE",
            n=nxt.dose_no,
            total=self._total,
            date=nxt.due,
            time=self._visit_time,
            clinic=self._clinic,
        )

    def _questions(self) -> Script:
        p = self._prior
        if "place" not in p:
            yield from self._place()
        if "place_type" not in p:
            key = yield from self._q("FC_PLACETYPE", "".join(PLACE_TYPE_BY_KEY))
            yield [Save("place_type", PLACE_TYPE_BY_KEY[key])]
        if "animal" not in p:
            key = yield from self._q("FC_ANIMAL", "1234")
            if key == "1":
                key = yield from self._q("FC_NOTCH", "123")
                animal = NOTCH_BY_KEY[key]
            else:
                animal = ANIMAL_BY_KEY[key]
            yield [Save("animal", animal)]
        if "signs" not in p:
            key = yield from self._q("FC_SICK", "123")
            yield [Save("signs", SIGNS_BY_KEY[key])]
            if key == "1":
                yield [_say("FC_SICK_YES")]
        return "completed"

    def _place(self) -> Script:
        """Record the place, twice at most. Key 9 while the question plays or the answer is recorded asks
        to stop the calls like anywhere else; keeping them asks the same question again. A recording is
        speech when its voiced seconds, measured by the runner, reach policy calls.speech_min_voiced_s."""
        prompts, attempt = ("FC_PLACE", "FC_PLACE_AGAIN"), 0
        while attempt < len(prompts):
            event = yield [Play((Say(prompts[attempt]),), listen=True), Record(*self._record)]
            if isinstance(event, Key):
                if event.digit != "9":
                    raise ValueError(f"only key 9 interrupts a recording, got {event!r}")
                yield from self._confirm(
                    "OPT_CONFIRM", "9", "OPT_DONE", "withdrawn", "withdrawn", "SYS_GIVEUP_FIRST", "gave_up"
                )
                continue
            if not isinstance(event, Recorded):
                raise ValueError(f"expected a recording result, got {event!r}")
            if event.ok and event.voiced_s is None:
                raise ValueError(f"a recording that finished ok was not measured: {event!r}")
            if event.ok and event.voiced_s >= self._speech_min_voiced_s:
                yield [Save("place", "recorded")]
                return "completed"
            attempt += 1
        yield [Save("place", "none")]  # nothing heard twice: say so, guess nothing
        return "completed"


# ------------------------------------------------------------------- reminders


class ReminderFlow(Flow):
    """The evening-before call. After the listener presses 1: clinic, dose number, date, and the keys 9 and star."""

    essential = frozenset({"RM_EVE_BODY", "RM_EVE_BODY_OUT"})

    def __init__(
        self,
        *,
        policy: Policy,
        lang: str,
        clinic_id: str,
        dose_no: int,
        total: int,
        due: date,
        visit_time: str,
        vaccine_out: bool = False,
        clinic_phone: str | None = None,
        team_ward: int | None = None,
        register: str | None = None,
    ) -> None:
        super().__init__(policy, lang)
        self._register = register
        if vaccine_out and not clinic_phone:
            raise ValueError("the vaccine-out reminder needs the clinic's phone number")
        self._clinic = clinic_id
        slots: dict[str, Any] = {
            "n": dose_no,
            "total": total,
            "date": due,
            "time": visit_time,
            "clinic": clinic_id,
        }
        if vaccine_out:
            slots["clinic_phone"] = clinic_phone
        body = [Say("RM_EVE_BODY_OUT" if vaccine_out else "RM_EVE_BODY", slots)]
        if team_ward is not None:
            body.append(Say("FIN_TEAM", {"ward": team_ward}))
        self._body = [*body, Say("RM_EVE_END")]

    def _script(self) -> Script:
        yield from self._gate()
        first = [Say("RM_HELLO", {"clinic": self._clinic}), *_who(self._register), *self._body]
        heard, replays = first, 0
        while True:
            event = yield [Play(tuple(heard), listen=True), Collect("9*", self._wait)]
            heard = self._body
            if not isinstance(event, Key | NoInput):
                raise ValueError(f"expected a key or silence, got {event!r}")
            if isinstance(event, Key) and event.digit == "*" and replays < self._repeats:
                replays += 1
                continue
            if isinstance(event, Key) and event.digit == "9":
                yield from self._confirm(
                    "OPT_CONFIRM", "9", "OPT_DONE", "withdrawn", "withdrawn", None, "completed"
                )
            return "completed"


class MorningAfterFlow(Flow):
    """The morning after a dose that was not marked: did you take it? 1 yes, 2 no. It is asked only after the
    listener has pressed 1 to say they are the person who gave the missed call. A yes is what the patient
    says, not what the clinic has seen: the runner stores it as patient-reported and the dose stays open."""

    essential = frozenset({"RM_MISS_TAKEN", "RM_MISS_TAKEN_LAST", "RM_MISS_NOT"})

    def __init__(
        self,
        *,
        policy: Policy,
        lang: str,
        clinic_id: str,
        dose_no: int,
        total: int,
        due: date,
        next_dose: DoseInfo | None,
        visit_time: str,
        register: str | None = None,
    ) -> None:
        super().__init__(policy, lang)
        self._clinic = clinic_id
        self._register = register
        self._q = Say("RM_MISS_Q", {"n": dose_no, "total": total, "date": due})
        self._dose_no = dose_no
        self._total = total
        self._next = next_dose
        self._visit_time = visit_time

    def _script(self) -> Script:
        yield from self._gate()
        hello = Say("RM_HELLO", {"clinic": self._clinic})
        key = yield from self._ask(
            [hello, *_who(self._register), self._q], [self._q], "12", giveup="SYS_GIVEUP_MISS"
        )
        if key == "1":
            yield [Save("dose_taken", "yes"), MarkTaken(self._dose_no)]
            if self._next is None:
                yield [_say("RM_MISS_TAKEN_LAST")]
            else:
                yield [
                    _say(
                        "RM_MISS_TAKEN",
                        n=self._next.dose_no,
                        total=self._total,
                        date=self._next.due,
                        time=self._visit_time,
                    )
                ]
        else:
            yield [Save("dose_taken", "no"), _say("RM_MISS_NOT", clinic=self._clinic)]
        return "completed"


class NoticeFlow(Flow):
    """The repeat-missed-call notice: "you are already signed up", and a chance to stop the calls with 9. A
    missed call never stops anything by itself; only a 9 here, confirmed by a second 9, does."""

    essential = frozenset({"ENR_DUP"})

    def _script(self) -> Script:
        event = yield [Play((Say("ENR_DUP"),), listen=True), Collect("9", self._wait)]
        if isinstance(event, Key) and event.digit == "9":
            yield from self._confirm(
                "OPT_CONFIRM", "9", "OPT_DONE", "withdrawn", "withdrawn", None, "completed"
            )
        return "completed"
