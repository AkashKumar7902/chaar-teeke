"""The DEMO dataset: 12 weeks of synthetic history written straight into the schema.

Seeded and deterministic: the same seed on the same clock gives the same rows (phone ciphers and password
hashes aside, which are salted; the digest looks at the numbers inside the ciphers and skips the hashes).
The staff passwords are the one thing that is not seeded: every run makes new random ones, returns them in
the report and writes them to var/demo_logins.txt (mode 0600). Every row carries is_demo = 1, every
phone number is in the reserved +91 99999 0xxxx range and is never dialled, and nothing here is a real
person, voice or place a patient said.

What it builds (SPEC s13): 3 clinics and 6 staff logins, about 25 enrolments a day with doses, calls and
answers consistent with the regimen calendar and today's date, bite reports whose notch rates differ by
cluster (two clusters near 55-60%, the rest near 78-82%), sick-dog alerts in every state, about 12 place
captures waiting for the reviewer, worked wards, sight counts and a few learned place names.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .. import audit, clock
from ..api.auth import hash_password
from ..context import AppContext
from ..doses import refresh_states
from ..enrol import DEMO_CLINIC_PHONES, DEMO_VISIT_TIME
from ..privacy import delete_clip_file
from ..regimens import get as get_regimen
from ..regimens import schedule
from ..stats.clusters import ward_table
from ..stats.errors import StatsError

DEFAULT_SEED = 20261019
HISTORY_DAYS = 84
LOGINS_FILE = "demo_logins.txt"
PASSWORD_GROUPS = 3  # groups of four characters from _ALPHABET: 60 bits, easy to read out and type
PATIENT_NUMBERS = 9_990  # +91 99999 09990 and up are the demo clinics' own numbers (enrol.DEMO_CLINIC_PHONES)
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_DAY_FACTOR = (1.15, 1.05, 1.0, 1.0, 0.95, 0.85, 0.6)  # Monday first

TABLES = (
    "clinics",
    "staff",
    "courses",
    "missed_calls",
    "contacts",
    "purge_log",
    "doses",
    "calls",
    "answers",
    "bite_reports",
    "place_captures",
    "alerts",
    "callback_requests",
    "worked_wards",
    "sight_counts",
    "aliases",
)
_WIPE_ORDER = (
    "callback_requests",
    "alerts",
    "place_captures",
    "answers",
    "calls",
    "doses",
    "contacts",
    "bite_reports",
    "missed_calls",
    "purge_log",
    "courses",
    "worked_wards",
    "sight_counts",
    "aliases",
    "staff",
    "clinics",
)

STAFF = (
    ("clinic", "clinic", "GMC clinic desk", "cln_gmc"),
    ("abc", "abc", "Sterilisation team", None),
    ("vet", "vet", "Vet on duty", None),
    ("oversight", "oversight", "Health oversight", None),
    ("reviewer", "reviewer", "Place reviewer", None),
    ("admin", "admin", "Administrator", None),
)
OUTSIDE_TEHSILS = ("Akhnoor", "Bishnah", "Marh", "Samba", "Ranbir Singh Pura")
ALERT_NOTES_ACTED = ("Team visited the place and caught the dog.", "Dog traced and taken by the team.")
ALERT_NOTES_NOT_ACTED = ("The dog could not be found at the place given.", "Place too vague to search.")
PLACE_TEXT_LATIN = ("{n} ke paas", "{n} market ke samne", "mera ghar {n} mein hai", "{n} chowk par")
PLACE_TEXT_HINDI = ("{n} के पास", "{n} मार्केट के सामने", "{n} में")
PLACE_TEXT_VAGUE = (
    "wahan school ke saamne",
    "bus adde ke paas",
    "mandir ke peeche gali mein",
    "hospital ke bahar",
)
STOP_REASONS = {
    "declined": "the patient said no to reminders",
    "withdrawn": "the patient pressed 9 to stop the calls",
    "not_needed": "Clinic decided no more doses were needed",
    "moved": "Patient moved to another city",
    "retention": "Course window over; the phone number was removed",
}


class SeedError(StatsError):
    """code: not_demo, not_empty, clusters_not_loaded, clip_not_deleted."""


class SeedReport(BaseModel):
    seed: int
    now: str
    counts: dict[str, int]
    digest: str
    weak_cluster_ids: list[str]
    logins: dict[str, str]  # username -> the password made for this seed; not part of the digest


def new_password() -> str:
    """A fresh random password, from the system's secure source and not from the seeded stream."""
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(PASSWORD_GROUPS))


def logins_path(ctx: AppContext) -> Path:
    return ctx.settings.var_dir / LOGINS_FILE


def _write_logins(ctx: AppContext, logins: dict[str, str], made_at: str) -> None:
    """The staff passwords of this seed, for the person running the demo. Mode 0600, replaced every seed."""
    path = logins_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Chaar Teeke demo staff logins, made {made_at}. Every seed makes new ones.",
        "# Visitors use the visitor entry on the landing page and need none of these.",
        *(f"{name:<10} {password}" for name, password in logins.items()),
    ]
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # before any password is written: open() leaves the mode of a leftover file alone
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    tmp.replace(path)


def _b32(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


class _Gen:
    """Builds every row in memory, in a fixed order, from one seeded random stream."""

    def __init__(self, ctx: AppContext, seed: int) -> None:
        self.ctx = ctx
        self.rng = random.Random(seed)
        self.now = clock.now()
        self.tz = ctx.settings.local_tz
        self.today = clock.local_today(self.tz)
        self.calls_pol = ctx.policy["calls"]
        self.rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.logins: dict[str, str] = {}
        self.numbers = 0
        self.register_no: dict[str, int] = defaultdict(lambda: 10_000)
        self.reports: list[dict[str, Any]] = []
        self.signs_answer: dict[str, dict[str, Any]] = {}
        table = ward_table(ctx.settings.data_dir / "wards.geojson")
        self.wards = table
        self.clusters = ctx.clusters.all()
        by_size = sorted(self.clusters, key=lambda c: (-c.population, c.id))
        self.weak = sorted(c.id for c in (by_size[0], by_size[2])) if len(by_size) >= 3 else [by_size[0].id]
        self.rate: dict[str, float] = {}
        self.unsure: dict[str, float] = {}
        for c in self.clusters:
            self.rate[c.id] = (
                self.rng.uniform(0.36, 0.44) if c.id in self.weak else self.rng.uniform(0.78, 0.82)
            )
            self.unsure[c.id] = self.rng.uniform(0.12, 0.24)
        pops = sorted(w.population for w in table.values() if w.population)
        fallback = pops[len(pops) // 2] * 0.3
        self.ward_list = [w for c in self.clusters for w in c.ward_nos]
        self.ward_weight = [
            (table[w].population or fallback) * self.rng.uniform(0.6, 1.5) if w in table else fallback
            for w in self.ward_list
        ]
        self.cluster_of = {w: c.id for c in self.clusters for w in c.ward_nos}
        self.hindi = self._hindi_names()

    # ------------------------------------------------------------------ helpers
    def _hindi_names(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for e in json.loads((self.ctx.settings.data_dir / "gazetteer.json").read_text())["entries"]:
            if e.get("type") == "ward" and e.get("name_hi") and len(e.get("ward_nos", [])) == 1:
                out[e["ward_nos"][0]] = e["name_hi"]
        return out

    def id(self, prefix: str, when: datetime) -> str:
        return f"{prefix}_{_b32(int(when.timestamp() * 1000), 10)}{_b32(self.rng.getrandbits(40), 8)}"

    def at(self, day: date, hhmm: str) -> datetime:
        return clock.at_local(day, hhmm, self.tz)

    def local_day(self, when: datetime) -> date:
        return when.astimezone(clock.local_tz(self.tz)).date()

    def iso(self, when: datetime) -> str:
        return clock.iso(when)

    def capped(self, when: datetime) -> datetime:
        return min(when, self.now - timedelta(seconds=30))

    def number(self) -> str:
        n = self.numbers
        self.numbers += 1
        if n >= PATIENT_NUMBERS:
            raise SeedError("too_many_numbers", "The reserved demo number range is used up")
        return f"+9199999{n:05d}"

    def add(self, table: str, **row: Any) -> None:
        self.rows[table].append(row)

    # ------------------------------------------------------------------- build
    def build(self) -> None:
        self._clinics_and_staff()
        start = self.today - timedelta(days=HISTORY_DAYS - 1)
        for i in range(HISTORY_DAYS):
            day = start + timedelta(days=i)
            n = max(3, round(25 * _DAY_FACTOR[day.weekday()] * self.rng.uniform(0.85, 1.15)))
            moments = [self._enrol_moment(day) for _ in range(n)]
            for when in sorted(m for m in moments if m is not None):
                self._course(day, when)
        self._awaiting_and_unlinked()
        self._alerts_and_reviews()
        self._field_work()

    def _clinics_and_staff(self) -> None:
        defaults = dict(vaccine_out=0, vaccine_out_since=None, is_demo=1)
        self.add(
            "clinics",
            id="cln_gmc",
            name="GMC Jammu anti-rabies clinic",
            short_name="GMC Jammu ARC",
            ward_no=27,
            default_regimen="id_trc",
            default_lang="hi",
            arv_status="confirmed",
            visit_time=DEMO_VISIT_TIME,
            phone=None,  # the real number is not ours to publish; a vaccine-out reminder for this clinic fails loudly
            **defaults,
        )
        self.add(
            "clinics",
            id="cln_demo2",
            name="Demo clinic (unverified)",
            short_name="Demo clinic",
            ward_no=None,
            default_regimen="im_essen",
            default_lang="hi",
            arv_status="unverified",
            visit_time=DEMO_VISIT_TIME,
            phone=DEMO_CLINIC_PHONES["cln_demo2"],
            **defaults,
        )
        self.add(
            "clinics",
            id="cln_demo_visitor",
            name="Demo visitor desk",
            short_name="Visitor desk",
            ward_no=None,
            default_regimen="id_trc",
            default_lang="hi",
            arv_status="unverified",
            visit_time=DEMO_VISIT_TIME,
            phone=DEMO_CLINIC_PHONES["cln_demo_visitor"],
            **defaults,
        )
        for username, role, display, clinic in STAFF:
            password = new_password()
            self.logins[username] = password
            self.add(
                "staff",
                id=self.id("stf", self.now),
                username=username,
                display_name=display,
                role=role,
                clinic_id=clinic,
                password_hash=hash_password(password),
                active=1,
                is_demo=1,
            )

    def _enrol_moment(self, day: date) -> datetime | None:
        minutes = self.rng.randint(9 * 60, 16 * 60 + 30)
        when = self.at(day, f"{minutes // 60:02d}:{minutes % 60:02d}") + timedelta(
            seconds=self.rng.randint(0, 59)
        )
        return when if when <= self.now - timedelta(minutes=6) else None

    # --------------------------------------------------------------- calls
    def _attempts(
        self,
        course_id: str,
        kind: str,
        key: str,
        dose_no: int | None,
        first: datetime,
        p_answer: float,
        listen: tuple[int, int],
        stop_at: datetime | None = None,
    ) -> tuple[str | None, datetime | None]:
        """Rows for each attempt of one call job. Returns (id of the answered call, its answer time)."""
        pol = self.calls_pol
        window_end = self.at(self.local_day(first), pol["window_end_local"])
        t = first
        for attempt in range(1, pol["max_attempts"] + 1):
            why = None
            if t > self.now - timedelta(seconds=30):
                why = "Demo history ends here"
            elif stop_at is not None and t >= stop_at:
                why = "The course was stopped before this try"
            elif attempt > 1 and t >= window_end:
                why = "Outside the calling window"
            job_key = key if attempt == 1 else f"{key}:a{attempt}"
            if why:
                if attempt > 1:  # a retry nobody made: stored as cancelled so the scheduler never plans it
                    self.add(
                        "calls",
                        id=self.id("call", t),
                        course_id=course_id,
                        kind=kind,
                        job_key=job_key,
                        dose_no=dose_no,
                        scheduled_for=self.iso(t),
                        attempt=attempt,
                        transport="ari",
                        started_at=None,
                        is_demo=1,
                        status="cancelled",
                        answered_at=None,
                        ended_at=None,
                        hangup_cause=None,
                        seconds_listened=None,
                        error=why,
                    )
                break
            call_id = self.id("call", t)
            row = dict(
                id=call_id,
                course_id=course_id,
                kind=kind,
                job_key=job_key,
                dose_no=dose_no,
                scheduled_for=self.iso(t),
                attempt=attempt,
                transport="ari",
                started_at=self.iso(t + timedelta(seconds=1)),
                is_demo=1,
            )
            roll = self.rng.random()
            if roll < p_answer:
                answered = t + timedelta(seconds=self.rng.randint(8, 25))
                ended = max(answered, self.capped(answered + timedelta(seconds=self.rng.randint(*listen))))
                self.add(
                    "calls",
                    **row,
                    status="completed",
                    answered_at=self.iso(answered),
                    ended_at=self.iso(ended),
                    hangup_cause="normal_clearing",
                    seconds_listened=float((ended - answered).total_seconds()),
                    error=None,
                )
                return call_id, answered
            failed = roll > 0.99
            ended = t + timedelta(seconds=pol["ring_timeout_s"])
            self.add(
                "calls",
                **row,
                status="failed" if failed else "no_answer",
                answered_at=None,
                ended_at=self.iso(ended),
                hangup_cause=None if failed else "no_answer",
                seconds_listened=None,
                error="The phone network did not connect the call" if failed else None,
            )
            if attempt <= len(pol["retry_gaps_min"]):
                t = ended + timedelta(minutes=pol["retry_gaps_min"][attempt - 1])
        return None, None

    # -------------------------------------------------------------- one course
    def _course(self, day: date, enrol_at: datetime) -> None:
        r, pol = self.rng, self.calls_pol
        clinic = "cln_gmc" if r.random() < 0.85 else "cln_demo2"
        regimen = get_regimen(r.choices(["id_trc", "im_essen", "reexposure"], [55, 40, 5])[0])
        lang = r.choices(["hi", "doi", "ur"], [50, 30, 20])[0]
        eligibility = r.choices(["adult", "guardian_consented", "unconfirmed"], [89, 8, 3])[0]
        sched = schedule(regimen, day)
        course_id = self.id("crs", enrol_at)
        self.register_no[clinic] += 1
        mc_at = enrol_at + timedelta(seconds=r.randint(30, 180))
        link_at = mc_at + timedelta(seconds=r.randint(5, 40))
        last_due = sched[-1].due_date
        purge_after = self.at(
            last_due + timedelta(days=self.ctx.policy["retention"]["phone_purge_days_after_last_dose"]),
            self.ctx.policy["retention"]["purge_local_time"],
        )
        n_doses = len(sched)

        stop_kind, stop_dose, stop_at = None, 0, None
        x = r.random()
        if n_doses > 1 and x < 0.055:
            stop_kind = "withdrawn" if x < 0.01 else "not_needed" if x < 0.04 else "moved"
            stop_dose = r.randint(1, n_doses - 1)
            stop_at = self.at(sched[stop_dose - 1].due_date + timedelta(days=1), "10:00")
            if stop_at > self.now:
                stop_kind, stop_at = None, None
        dropout = r.randint(1, n_doses - 1) if n_doses > 1 and r.random() > 0.9 else n_doses

        number = self.number()
        # first call
        first_t = link_at + timedelta(seconds=pol["first_call_delay_s"])
        first_id, answered_at = self._attempts(
            course_id, "first", f"first:{course_id}", None, first_t, 0.70, (90, 200)
        )
        consent_reminders = consent_questions = None
        consent_at = None
        report = None
        if first_id:
            consent_reminders = r.random() < 0.93
            consent_at = answered_at
            ask = consent_reminders and eligibility != "unconfirmed"
            consent_questions = (r.random() < 0.88) if ask else None
            report = self._first_call_answers(
                first_id, answered_at, lang, consent_reminders, consent_questions, clinic, course_id, day
            )
        reminded = bool(consent_reminders)
        last_answered = (first_id, answered_at) if first_id else None
        if first_id and not consent_reminders:
            stop_kind, stop_dose, stop_at = "declined", 1, answered_at
        elif stop_kind == "withdrawn" and not reminded:
            stop_kind, stop_at = None, None  # nobody presses 9 on a call they never took

        given: dict[int, tuple[date, str] | None] = {}
        for spec in sched:
            n, due = spec.dose_no, spec.due_date
            if n == 1:
                given[n] = (due, "clinic")
                continue
            past_stop = stop_kind is not None and n > stop_dose
            if past_stop:
                given[n] = None
                continue
            remind_t = self.at(due - timedelta(days=1), pol["reminder_local_time"])
            if reminded and remind_t >= link_at:
                rid, rat = self._attempts(
                    course_id,
                    "reminder",
                    f"reminder:{course_id}:{n}:{(due - timedelta(days=1)).isoformat()}",
                    n,
                    remind_t,
                    0.82,
                    (20, 60),
                    stop_at,
                )
                last_answered = (rid, rat) if rid else last_answered
            if due > self.today:
                given[n] = None
                continue
            attends = n <= dropout
            on_time = attends and r.random() < (0.88 if reminded else 0.72)
            if on_time:
                given[n] = (due, "clinic")
                continue
            outcome: tuple[date, str] | None = None
            morning_t = self.at(due + timedelta(days=1), pol["morning_after_local_time"])
            says_taken = False
            if reminded:
                call_id, ans_at = self._attempts(
                    course_id,
                    "missed_dose",
                    f"missed_dose:{course_id}:{n}:{(due + timedelta(days=1)).isoformat()}",
                    n,
                    morning_t,
                    0.75,
                    (15, 40),
                    stop_at,
                )
                if call_id and attends:
                    says_taken = r.random() < 0.3
                if call_id:
                    last_answered = (call_id, ans_at)
                    self.add(
                        "answers",
                        call_id=call_id,
                        question="dose_taken",
                        value="yes" if says_taken else "no",
                        at=self.iso(ans_at + timedelta(seconds=10)),
                    )
            if says_taken:
                outcome = None  # the patient's word is not the clinic's mark: the dose stays open on the clinic's list
            elif attends and r.random() < 0.75:
                outcome = (due + timedelta(days=r.choice([1, 1, 2, 2, 3, 4, 5, 7, 9])), "clinic")
            given[n] = outcome if outcome and outcome[0] <= self.today else None

        if stop_kind == "withdrawn" and last_answered:
            self.add(
                "answers",
                call_id=last_answered[0],
                question="withdraw",
                value="withdrawn",
                at=self.iso(last_answered[1] + timedelta(seconds=20)),
            )
        self._dose_rows(course_id, sched, given, stop_kind)
        all_given = all(given.get(s.dose_no) for s in sched)
        status, completed_at, stopped_reason = "active", None, None
        if all_given:
            status = "completed"
            finish = max(g[0] for g in given.values() if g)
            completed_at = self.iso(self.capped(self.at(finish, "13:00")))
        elif stop_kind:
            status, stopped_reason = "stopped", STOP_REASONS[stop_kind]
        elif purge_after <= self.now:
            status, stopped_reason = "stopped", STOP_REASONS["retention"]
        self.add(
            "courses",
            id=course_id,
            clinic_id=clinic,
            register_no=str(self.register_no[clinic]),
            patient_type="new" if r.random() < 0.9 else "old",
            regimen_id=regimen.id,
            day0=day.isoformat(),
            lang=lang,
            status=status,
            eligibility=eligibility,
            consent_reminders_at=self.iso(consent_at) if consent_reminders else None,
            consent_questions=None if consent_questions is None else ("yes" if consent_questions else "no"),
            notice_version=self.ctx.policy.version if consent_at else None,
            stopped_reason=stopped_reason,
            source="demo",
            created_at=self.iso(enrol_at),
            completed_at=completed_at,
            is_demo=1,
        )
        self.add(
            "missed_calls",
            id=self.id("mc", mc_at),
            at=self.iso(mc_at),
            phone_cipher=None,
            phone_last4=number[-4:],
            transport="ari",
            status="linked",
            course_id=course_id,
            clinic_hint=clinic,
            is_demo=1,
        )
        self._contact(course_id, number, link_at, purge_after, stop_kind, stop_at)
        if report is not None:
            self.reports.append(report)

    def _contact(self, course_id, number, link_at, purge_after, stop_kind, stop_at) -> None:
        # the same reasons enrol._withdraw and privacy.run_purges write
        if stop_kind in ("withdrawn", "declined"):
            gone = ("withdrawn", stop_at)
        elif purge_after <= self.now:
            gone = ("retention", purge_after)
        else:
            gone = None
        if gone:
            self.add(
                "purge_log", course_id=course_id, what="phone", reason=gone[0], purged_at=self.iso(gone[1])
            )
            return
        self.add(
            "contacts",
            course_id=course_id,
            phone_cipher=self.ctx.vault.encrypt(number),
            phone_last4=number[-4:],
            created_at=self.iso(link_at),
            purge_after=self.iso(purge_after),
        )

    def _dose_rows(self, course_id, sched, given, stop_kind) -> None:
        pol = self.ctx.policy["doses"]
        for spec in sched:
            g = given.get(spec.dose_no)
            if g:
                status, given_on, by = "given", g[0].isoformat(), g[1]
            elif stop_kind == "not_needed" and spec.dose_no > 1:
                status, given_on, by = "not_needed", None, None
            else:
                late = (self.today - spec.due_date).days
                status = (
                    "late_review"
                    if late >= pol["late_review_from_days"]
                    else "late_minor"
                    if late >= pol["late_minor_days"][0]
                    else "due"
                )
                given_on, by = None, None
            self.add(
                "doses",
                course_id=course_id,
                dose_no=spec.dose_no,
                offset_days=spec.offset_days,
                due_date=spec.due_date.isoformat(),
                status=status,
                given_on=given_on,
                marked_by=by,
            )

    # ------------------------------------------------------ first call answers
    def _first_call_answers(self, call_id, answered_at, lang, reminders, questions, clinic, course_id, day):
        r = self.rng
        t = answered_at

        def say(question: str, value: str) -> None:
            nonlocal t
            t += timedelta(seconds=r.randint(6, 18))
            self.add("answers", call_id=call_id, question=question, value=value, at=self.iso(self.capped(t)))

        # answers use the words the call runner stores (the clinic set the language, so no "lang" answer)
        if not reminders:
            say("withdraw", "declined")
            return None
        say("consent_reminders", "yes")
        if questions is None:
            return None
        say("consent_questions", "yes" if questions else "no")
        if not questions:
            return None
        say("dose_today", "1")
        ward = r.choices(self.ward_list, self.ward_weight)[0]
        cluster = self.cluster_of[ward]
        status = r.choices(
            ["matched", "decided", "outside_jmc", "cant_tell", "no_place_heard"], [86, 6, 3, 2, 3]
        )[0]
        say("place", "none" if status == "no_place_heard" else "recorded")
        place_type = r.choices(
            ["home_street", "school", "hospital", "bus_market", "other", "no_answer"], [58, 10, 6, 14, 9, 3]
        )[0]
        if place_type != "no_answer":
            say("place_type", place_type)
        kind = r.choices(["street", "pet", "other", "animal_unknown", "no_answer"], [70, 17, 9, 3, 1])[0]
        if kind == "street":
            q = min(0.95, max(0.3, r.gauss(self.rate[cluster], 0.03)))
            if r.random() < self.unsure[cluster]:
                animal = "unsure"
            else:
                animal = "notched" if r.random() < q else "unnotched"
        else:
            animal = {"pet": "pet", "other": "other", "animal_unknown": "animal_unknown"}.get(
                kind, "no_answer"
            )
        if animal != "no_answer":
            say("animal", animal)
        signs = r.choices(["no", "dont_know", "no_answer"], [92.5, 4, 3.5])[0]
        signs_row = None
        if signs != "no_answer":
            say("signs", signs)
            signs_row = self.rows["answers"][-1]
        report_id = self.id("rep", t)
        if signs_row is not None:
            self.signs_answer[report_id] = signs_row
        row = dict(
            id=report_id,
            course_id=course_id,
            clinic_id=clinic,
            bite_week=clock.week_start(day - timedelta(days=r.randint(0, 2))).isoformat(),
            place_type=place_type,
            animal=animal,
            signs=signs,
            ward_no=ward if status in ("matched", "decided") else None,
            outside_tehsil=r.choice(OUTSIDE_TEHSILS) if status == "outside_jmc" else None,
            location_status=status,
            cluster_id=cluster if status in ("matched", "decided") else None,
            source="demo",
            created_at=self.iso(self.capped(t + timedelta(seconds=5))),
            is_demo=1,
        )
        self.rows["bite_reports"].append(row)
        return row

    # ------------------------------------------------- today's loose ends
    def _awaiting_and_unlinked(self) -> None:
        r = self.rng
        for i in range(2):
            made = self.now - timedelta(minutes=r.randint(4, 40) + 10 * i)
            clinic = "cln_gmc"
            sched = schedule(get_regimen("id_trc"), self.today)
            course_id = self.id("crs", made)
            self.register_no[clinic] += 1
            self.add(
                "courses",
                id=course_id,
                clinic_id=clinic,
                register_no=str(self.register_no[clinic]),
                patient_type="new",
                regimen_id="id_trc",
                day0=self.today.isoformat(),
                lang="hi",
                status="awaiting_call",
                eligibility="adult",
                consent_reminders_at=None,
                consent_questions=None,
                notice_version=None,
                stopped_reason=None,
                source="demo",
                created_at=self.iso(made),
                completed_at=None,
                is_demo=1,
            )
            self._dose_rows(course_id, sched, {1: (self.today, "clinic")}, None)
        for minutes in (25, 130, 300):
            at = self.now - timedelta(minutes=minutes + r.randint(0, 9))
            # Simulator calls, as a clinic's own browser phone gives them: Today shows them in the link list
            # with the SIMULATOR pill. The numbers are in the reserved range, never dialled. Reset makes them again.
            number = self.number()
            self.add(
                "missed_calls",
                id=self.id("mc", at),
                at=self.iso(at),
                phone_cipher=self.ctx.vault.encrypt(number),
                phone_last4=number[-4:],
                transport="sim",
                status="unlinked",
                course_id=None,
                clinic_hint="cln_gmc",
                is_demo=1,
            )

    # ------------------------------------------------ alerts and the review queue
    def _alerts_and_reviews(self) -> None:
        placed = sorted(
            (x for x in self.reports if x["ward_no"] is not None), key=lambda x: (x["created_at"], x["id"])
        )
        plan = [
            ("new", 0),
            ("new", 1),
            ("claimed", 5),
            ("claimed", 14),
            ("acted", 24),
            ("acted", 55),
            ("acted", 100),
            ("not_acted", 38),
            ("not_acted", 130),
        ]
        answered_signs = [
            x for x in placed if x["id"] in self.signs_answer
        ]  # an alert needs the answer to change
        used: set[str] = set()
        for n, (status, back) in enumerate(plan):
            if back >= len(answered_signs):
                continue
            rep = answered_signs[-1 - back]
            used.add(rep["id"])
            rep["signs"] = "yes"
            self.signs_answer[rep["id"]]["value"] = "yes"
            self._alert(rep, status, n)
        cutoff = self.iso(self.now - timedelta(days=5))
        pool = [x for x in placed if x["created_at"] >= cutoff and x["id"] not in used]
        step = max(1, len(pool) // 12)
        for k, rep in enumerate(pool[::step][:12], start=1):
            self._review(rep, k)

    def _alert(self, rep: dict[str, Any], status: str, n: int) -> None:
        r = self.rng
        made = clock.parse_iso(rep["created_at"])
        alert_id = self.id("alr", made)
        seen = self.capped(made + timedelta(minutes=r.randint(2, 9)))
        claimed_by = claimed_at = closed_at = note = None
        if status != "new":
            claimed_by, claimed_at = "staff:vet", self.capped(made + timedelta(minutes=r.randint(6, 25)))
        if status in ("acted", "not_acted"):
            closed_at = self.capped(made + timedelta(minutes=r.randint(90, 1200)))
            note = r.choice(ALERT_NOTES_ACTED if status == "acted" else ALERT_NOTES_NOT_ACTED)
        self.add(
            "alerts",
            id=alert_id,
            report_id=rep["id"],
            ward_no=rep["ward_no"],
            cluster_id=rep["cluster_id"],
            place_type=rep["place_type"],
            local_day=self.local_day(made).isoformat(),
            status=status,
            claimed_by=claimed_by,
            claimed_at=self.iso(claimed_at) if claimed_at else None,
            closed_at=self.iso(closed_at) if closed_at else None,
            outcome_note=note,
            first_seen_by_vet_at=self.iso(seen),
            created_at=rep["created_at"],
            is_demo=1,
        )
        if status == "claimed" and n == 2:
            self._callback(alert_id, rep, claimed_at, "open")
        if status == "acted" and n == 4:
            self._callback(alert_id, rep, claimed_at, "done")

    def _callback(self, alert_id: str, rep: dict[str, Any], claimed_at: datetime, status: str) -> None:
        asked = self.capped(claimed_at + timedelta(minutes=2))
        done = self.capped(asked + timedelta(minutes=40)) if status == "done" else None
        self.add(
            "callback_requests",
            id=self.id("cb", asked),
            alert_id=alert_id,
            course_id=rep["course_id"],
            requested_by="staff:vet",
            requested_at=self.iso(asked),
            status=status,
            done_at=self.iso(done) if done else None,
            note="Spoke to the patient" if done else None,
        )

    def _review(self, rep: dict[str, Any], k: int) -> None:
        r = self.rng
        true_ward = rep["ward_no"]
        rep["location_status"], rep["ward_no"], rep["cluster_id"] = "needs_review", None, None
        none = k % 3 == 0
        info = self.wards[true_ward]
        short = info.name.split("/")[0].split("(")[0].strip()
        if none:
            text, confidence, cands = r.choice(PLACE_TEXT_VAGUE), "none", []
        else:
            hindi = self.hindi.get(true_ward)
            if hindi and k % 2 == 0:
                text = r.choice(PLACE_TEXT_HINDI).format(n=hindi)
            else:
                text = r.choice(PLACE_TEXT_LATIN).format(n=short)
            other = r.choice([w for w in self.ward_list if w != true_ward])
            cands = [
                dict(ward_no=true_ward, ward_name=info.name, label="possible", evidence=short),
                dict(
                    ward_no=other,
                    ward_name=self.wards[other].name,
                    label="possible",
                    evidence=self.wards[other].name.split("/")[0].split("(")[0].strip(),
                ),
            ]
            confidence = "possible"
        clip = f"data/demo/clips/{k}.wav"
        has_clip = (self.ctx.settings.data_dir / "demo" / "clips" / f"{k}.wav").exists()
        made = clock.parse_iso(rep["created_at"])
        self.add(
            "place_captures",
            report_id=rep["id"],
            recording_name=f"demo/place-{k}",
            audio_path=clip if has_clip else None,
            transcript=text,
            asr_model="synthetic transcript (demo data)",
            asr_status="done",
            asr_error=None,
            candidates_json=json.dumps(cands, ensure_ascii=False),
            confidence=confidence,
            decided_by=None,
            decided_at=None,
            clip_deleted_at=None,
            expires_at=self.iso(made + timedelta(days=self.ctx.policy["retention"]["clip_max_age_days"])),
        )

    # ----------------------------------------- worked wards, sight counts, names
    def _field_work(self) -> None:
        r = self.rng
        by_cluster = {c.id: c for c in self.clusters}
        picks: list[tuple[int, int]] = []  # (ward_no, days ago)
        for cid in self.weak:
            wards = by_cluster[cid].ward_nos
            picks += [(wards[0], 36), (wards[1], 24)]
        for c in self.clusters:
            if c.id not in self.weak and len(picks) < 9:
                picks.append((c.ward_nos[0], r.randint(10, 60)))
        picks.append((self.clusters[0].ward_nos[-1], 5))
        for ward, ago in picks:
            self.add(
                "worked_wards",
                ward_no=ward,
                worked_on=(self.today - timedelta(days=ago)).isoformat(),
                recorded_by="staff:abc",
                note="Sterilisation round",
                is_demo=1,
            )
        for _ in range(28):
            ward = r.choice(self.ward_list)
            cid = self.cluster_of[ward]
            total = r.randint(12, 30)
            unsure = round(total * self.unsure[cid] * r.uniform(0.7, 1.3))
            notched = round((total - unsure) * min(0.95, max(0.3, r.gauss(self.rate[cid], 0.05))))
            self.add(
                "sight_counts",
                ward_no=ward,
                counted_on=(self.today - timedelta(days=r.randint(1, 70))).isoformat(),
                notched=notched,
                unnotched=total - unsure - notched,
                unsure=unsure,
                recorded_by="staff:abc",
                is_demo=1,
            )
        for ward in (
            self.clusters[1].ward_nos[0],
            self.clusters[3].ward_nos[0],
            self.clusters[5].ward_nos[0],
            self.clusters[7].ward_nos[0],
        ):
            short = self.wards[ward].name.split("/")[0].split("(")[0].strip().lower()
            self.add(
                "aliases",
                text_norm=f"{short} market",
                ward_no=ward,
                added_by="staff:reviewer",
                added_at=self.iso(self.now - timedelta(days=r.randint(2, 40))),
            )


# ---------------------------------------------------------------------- entry points
def _delete_clips(ctx: AppContext) -> None:
    """Voice clips recorded on the demo phone are personal data: they go with the rows that name them."""
    for r in ctx.db.query("SELECT audio_path FROM place_captures WHERE audio_path IS NOT NULL"):
        try:
            delete_clip_file(ctx, r["audio_path"])
        except (OSError, ValueError) as e:
            raise SeedError(
                "clip_not_deleted", "A voice clip could not be deleted, so the demo was not reset."
            ) from e


def _wipe(conn) -> None:
    # Staff ids are made again, so staff sessions go; a visitor's session names no staff row and stays,
    # so pressing Reset does not sign the presenter and the visitors out.
    conn.execute("DELETE FROM sessions WHERE staff_id IS NOT NULL")
    for table in _WIPE_ORDER:
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM kv WHERE key IN ('demo_seed', 'last_purge_run')")
    # row numbers start again, so a rebuilt demo is identical to the first one
    marks = ", ".join("?" * len(_WIPE_ORDER))
    conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({marks})", _WIPE_ORDER)


def _insert(conn, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    if any(list(r) != cols for r in rows):
        raise SeedError("bad_rows", f"Seed rows for {table} do not share columns")
    conn.executemany(
        f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})", rows
    )


def digest(ctx: AppContext) -> str:
    """SHA-256 over every seeded table. Phone ciphers are replaced by the number inside, because the
    ciphertext differs on every run; password hashes are left out for the same reason."""
    h = hashlib.sha256()
    for table in TABLES:
        for row in ctx.db.query(f"SELECT * FROM {table} ORDER BY rowid"):
            d = dict(row)
            d.pop("password_hash", None)  # salted and new on every seed, like the cipher below
            if d.get("phone_cipher"):
                d["phone_cipher"] = ctx.vault.decrypt(d["phone_cipher"])
            h.update(json.dumps([table, d], sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()


def seed(ctx: AppContext, seed: int = DEFAULT_SEED, reset: bool = False) -> SeedReport:
    if not ctx.settings.is_demo:
        raise SeedError("not_demo", "Demo data is only made in demo mode")
    if ctx.clusters is None:
        raise SeedError("clusters_not_loaded", "The analysis areas are not loaded")
    held = any(
        ctx.db.one(f"SELECT 1 FROM {t} LIMIT 1") for t in ("clinics", "staff", "courses", "bite_reports")
    )
    if held and not reset:
        raise SeedError("not_empty", "The database already has data. Reset the demo to replace it")
    gen = _Gen(ctx, seed)
    gen.build()
    if held:
        _delete_clips(ctx)  # first: a clip that cannot be deleted stops the reset before any row is lost
    with ctx.db.transaction() as conn:
        if held:
            _wipe(conn)
        for table in TABLES:
            _insert(conn, table, gen.rows[table])
        audit.record(
            conn,
            "system:demo",
            "demo.seed",
            "demo",
            None,
            {"seed": seed, "courses": len(gen.rows["courses"]), "reset": reset},
        )
    refresh_states(ctx)
    counts = {t: ctx.db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in TABLES}
    report = SeedReport(
        seed=seed,
        now=clock.iso(gen.now),
        counts=counts,
        digest=digest(ctx),
        weak_cluster_ids=list(gen.weak),
        logins=gen.logins,
    )
    _write_logins(ctx, gen.logins, report.now)
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES ('demo_seed', ?)",
            (json.dumps({"seed": seed, "digest": report.digest, "at": report.now}),),
        )
    return report


def clip_dir(ctx: AppContext) -> Path:
    return ctx.settings.data_dir / "demo" / "clips"
