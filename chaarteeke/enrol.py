"""Enrolment: a course, the missed call that links a phone to it, withdrawal and the clinic's Today list.

A phone number lives only in `contacts` (encrypted) and, for at most `unlinked_missed_call_hours`, in
`missed_calls` (encrypted). Every state change writes an audit row in its own transaction and publishes
its event after the commit. No event, log line or return value here carries a number except
`reveal_phone` and `dial_number`, which exist to hand it to a person or to the dialler.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel

from . import audit, clock, regimens
from .context import AppContext
from .doses import PENDING, emit, refresh_states
from .ids import new_id
from .models import (
    CallbackRequestOut,
    CallOut,
    ClinicRow,
    ClinicToday,
    CourseOut,
    DoseOut,
    EnrolIn,
    UnlinkedMissedCall,
)
from .privacy import delete_clip_file
from .telephony.numbers import to_e164_in

PERSON_HUNG_UP = "The person hung up before the call finished."
DEMO_CLINIC_ID = "cln_demo_visitor"
DEMO_ACTOR = "system:demo"
# The demo clinics' contact details are made up: +91 99999 099xx is in the reserved range that is never dialled.
DEMO_VISIT_TIME = "09:00"
DEMO_CLINIC_PHONES = {"cln_demo_visitor": "+919999909991", "cln_demo2": "+919999909992"}
SCHEDULER_ACTOR = "system:scheduler"
_Events = list[tuple[str, dict[str, Any]]]


class EnrolError(Exception):
    """A change the server refuses. `code` is machine readable, `message` is plain words for the screen.

    Codes: unknown_clinic, regimen_disabled, bad_date, not_awaiting, already_linked, course_closed,
    not_found, plus unknown_regimen, missed_call_closed, wrong_clinic, phone_purged, bad_register_no,
    duplicate_register (`course_id` is the register entry that already has the number), edit_window_closed
    and nothing_to_change.
    """

    def __init__(self, code: str, message: str, course_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.course_id = course_id


class MissedCallResult(BaseModel):
    missed_call_id: str
    status: Literal["unlinked", "linked", "duplicate", "withdrawn", "withheld"]
    course_id: str | None = None


# ------------------------------------------------------------------ helpers
def _tz(ctx: AppContext) -> str:
    return ctx.settings.local_tz


def _today(ctx: AppContext) -> date:
    return clock.local_today(_tz(ctx))


def normalise_number(number: str | None, transport: str) -> str | None:
    """The one form a number is stored and compared in; None when there is no number to call back."""
    if not number:
        return None
    if number.startswith("sim:"):
        return number if transport == "sim" else None
    return to_e164_in(number)


def call_event(call_id: str, course_id: str | None, kind: str, status: str, transport: str | None) -> tuple:
    return (
        "call",
        {
            "call_id": call_id,
            "course_id": course_id,
            "kind": kind,
            "status": status,
            "simulated": transport == "sim",
        },
    )


def new_call(
    conn: sqlite3.Connection,
    *,
    course_id: str | None,
    kind: str,
    job_key: str,
    scheduled_for: datetime,
    dose_no: int | None = None,
    attempt: int = 1,
    transport: str | None = None,
    is_demo: int = 0,
) -> str | None:
    """Insert a scheduled call. Returns its id, or None when the job key already exists (idempotent)."""
    call_id = new_id("call")
    cur = conn.execute(
        "INSERT OR IGNORE INTO calls(id, course_id, kind, job_key, dose_no, scheduled_for, attempt, status,"
        " transport, is_demo) VALUES (?,?,?,?,?,?,?, 'scheduled', ?, ?)",
        (call_id, course_id, kind, job_key, dose_no, clock.iso(scheduled_for), attempt, transport, is_demo),
    )
    return call_id if cur.rowcount else None


def _cancel_scheduled(conn: sqlite3.Connection, course_id: str, why: str, events: _Events) -> None:
    sql = "SELECT id, kind, transport FROM calls WHERE course_id=? AND status='scheduled'"
    for c in conn.execute(sql, (course_id,)).fetchall():
        conn.execute("UPDATE calls SET status='cancelled', error=? WHERE id=?", (why, c["id"]))
        events.append(call_event(c["id"], course_id, c["kind"], "cancelled", c["transport"]))


def _clinic(conn: sqlite3.Connection, clinic_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM clinics WHERE id=?", (clinic_id,)).fetchone()
    if row is None:
        raise EnrolError("unknown_clinic", "This clinic is not in the system.")
    return row


def _course(conn: sqlite3.Connection, course_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
    if row is None:
        raise EnrolError("not_found", "This patient's course was not found.")
    return row


def _parse_day0(ctx: AppContext, text: str) -> date:
    try:
        day0 = date.fromisoformat(text)
    except ValueError as e:
        raise EnrolError("bad_date", "The first dose date is not a valid date.") from e
    if (
        day0.isoformat() != text
    ):  # fromisoformat also reads 20261019 and week dates; the column holds YYYY-MM-DD
        raise EnrolError("bad_date", "The first dose date is not a valid date.")
    if day0 > _today(ctx):
        raise EnrolError("bad_date", "The first dose date cannot be a day that has not come.")
    return day0


# ------------------------------------------------------------------- create
_REGISTER_NO = re.compile(r"[A-Za-z0-9/._-]{1,20}")
_LONG_DIGITS = re.compile(r"\d{10,}")
# characters that reorder how text is shown, so a register number could read differently than it is stored
_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def clean_register_no(raw: str) -> str:
    """The register number as it is stored: letters, digits and / . _ - only, at most 20 characters. Invisible
    format characters (zero-width spaces and the like) are dropped; control characters, text-direction
    overrides and anything shaped like a phone number (ten digits in a row) are refused."""
    if any(ch in _BIDI or unicodedata.category(ch) == "Cc" for ch in raw):
        raise EnrolError("bad_register_no", "The register number has a hidden control character in it.")
    text = "".join(ch for ch in raw.strip() if unicodedata.category(ch) != "Cf")
    if not _REGISTER_NO.fullmatch(text):
        raise EnrolError(
            "bad_register_no",
            "The register number can only have letters, digits and / . _ - (at most 20 characters).",
        )
    if _LONG_DIGITS.search(text):
        raise EnrolError("bad_register_no", "That looks like a phone number. Enter the register number.")
    return text


def _check_register_free(
    conn: sqlite3.Connection, clinic_id: str, register_no: str, own_course_id: str | None = None
) -> None:
    other = conn.execute(
        "SELECT id FROM courses WHERE clinic_id=? AND register_no=? AND status IN ('awaiting_call','active')"
        " AND id IS NOT ?",
        (clinic_id, register_no, own_course_id),
    ).fetchone()
    if other is not None:
        raise EnrolError(
            "duplicate_register",
            "This register number already has a patient who is being called. Open that entry instead.",
            course_id=other["id"],
        )


def _insert_course(
    ctx: AppContext,
    conn: sqlite3.Connection,
    data: EnrolIn,
    actor: str,
    source: str,
    events: _Events,
) -> str:
    clinic = _clinic(conn, data.clinic_id)
    reg = regimens.regimens().get(data.regimen_id)
    if reg is None:
        raise EnrolError("unknown_regimen", "This vaccine schedule is not known.")
    if not reg.enabled:
        raise EnrolError("regimen_disabled", "This vaccine schedule is not switched on.")
    day0 = _parse_day0(ctx, data.day0)
    register_no = clean_register_no(data.register_no)
    _check_register_free(conn, data.clinic_id, register_no)
    course_id = new_id("crs")
    now = clock.now()
    is_demo = 1 if (clinic["is_demo"] or source != "clinic") else 0
    conn.execute(
        "INSERT INTO courses(id, clinic_id, register_no, patient_type, regimen_id, day0, lang, status,"
        " eligibility, source, created_at, is_demo) VALUES (?,?,?,?,?,?,?, 'awaiting_call', ?,?,?,?)",
        (
            course_id,
            data.clinic_id,
            register_no,
            data.patient_type,
            data.regimen_id,
            data.day0,
            data.lang,
            data.eligibility,
            source,
            clock.iso(now),
            is_demo,
        ),
    )
    for spec in regimens.schedule(reg, day0):
        # Dose 1 is the visit that starts the course (day 0), so it is recorded as given by the clinic.
        first = spec.dose_no == 1
        conn.execute(
            "INSERT INTO doses(course_id, dose_no, offset_days, due_date, status, given_on, marked_by)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                course_id,
                spec.dose_no,
                spec.offset_days,
                spec.due_date.isoformat(),
                "given" if first else "due",
                spec.due_date.isoformat() if first else None,
                "clinic" if first else None,
            ),
        )
    audit.record(
        conn,
        actor,
        "course.create",
        "course",
        course_id,
        {"clinic_id": data.clinic_id, "regimen_id": data.regimen_id, "source": source},
    )
    events.append(("course", {"course_id": course_id, "status": "awaiting_call"}))
    return course_id


def create_course(
    ctx: AppContext, data: EnrolIn, actor: str, source: Literal["clinic", "simulator", "demo"] = "clinic"
) -> CourseOut:
    events: _Events = []
    with ctx.db.transaction() as conn:
        course_id = _insert_course(ctx, conn, data, actor, source, events)
    emit(ctx, events)
    return get_course(ctx, course_id)


def update_course(
    ctx: AppContext,
    course_id: str,
    actor: str,
    *,
    register_no: str | None = None,
    eligibility: Literal["adult", "guardian_consented"] | None = None,
) -> CourseOut:
    """Correct a slip made at the desk: the register number, or whether a guardian agreed. Allowed only for
    policy clinic.edit_window_minutes after the entry was made, and only until the patient has been called."""
    if register_no is None and eligibility is None:
        raise EnrolError("nothing_to_change", "Nothing was changed.")
    events: _Events = []
    with ctx.db.transaction() as conn:
        course = _course(conn, course_id)
        if course["status"] not in ("awaiting_call", "active"):
            raise EnrolError("course_closed", "This course has ended, so it cannot be corrected.")
        minutes = ctx.policy["clinic"]["edit_window_minutes"]
        if clock.parse_iso(course["created_at"]) + timedelta(minutes=minutes) <= clock.now():
            raise EnrolError(
                "edit_window_closed", f"An entry can only be corrected in the first {minutes} minutes."
            )
        changes: dict[str, Any] = {}
        if register_no is not None:
            clean = clean_register_no(register_no)
            if clean != course["register_no"]:
                _check_register_free(conn, course["clinic_id"], clean, course_id)
                changes["register_no"] = [course["register_no"], clean]
        if eligibility is not None and eligibility != course["eligibility"]:
            called = conn.execute(
                "SELECT 1 FROM calls WHERE course_id=? AND status != 'scheduled'", (course_id,)
            ).fetchone()
            if called or course["consent_reminders_at"]:
                raise EnrolError(
                    "edit_window_closed",
                    "The patient has already been called, so the guardian's agreement cannot be changed.",
                )
            changes["eligibility"] = [course["eligibility"], eligibility]
        if not changes:
            raise EnrolError("nothing_to_change", "Nothing was changed.")
        for column, (_old, new) in changes.items():
            conn.execute(f"UPDATE courses SET {column}=? WHERE id=?", (new, course_id))
        audit.record(conn, actor, "course.update", "course", course_id, changes)
        events.append(("course", {"course_id": course_id, "status": course["status"]}))
    emit(ctx, events)
    return get_course(ctx, course_id)


# -------------------------------------------------------------- missed calls
def _same_number(ctx: AppContext, cipher: str | None, number: str) -> bool:
    return cipher is not None and ctx.vault.decrypt(cipher) == number


def _insert_missed(
    ctx: AppContext,
    conn: sqlite3.Connection,
    *,
    at: datetime,
    transport: str,
    status: str,
    number: str | None,
    keep_cipher: bool,
    course_id: str | None = None,
    clinic_hint: str | None = None,
) -> str:
    mc_id = new_id("mc")
    last4 = ctx.vault.last4(number) if number else None
    conn.execute(
        "INSERT INTO missed_calls(id, at, phone_cipher, phone_last4, transport, status, course_id,"
        " clinic_hint, is_demo) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            mc_id,
            clock.iso(at),
            ctx.vault.encrypt(number) if (number and keep_cipher) else None,
            last4 or None,
            transport,
            status,
            course_id,
            clinic_hint,
            1 if ctx.settings.is_demo else 0,
        ),
    )
    return mc_id


def _missed_event(ctx: AppContext, conn: sqlite3.Connection, mc_id: str) -> tuple:
    row = conn.execute("SELECT status, phone_last4 FROM missed_calls WHERE id=?", (mc_id,)).fetchone()
    return ("missed_call", {"id": mc_id, "status": row["status"], "phone_last4": row["phone_last4"]})


def _ensure_demo_clinic(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "INSERT OR IGNORE INTO clinics(id, name, short_name, ward_no, default_regimen, default_lang,"
        " arv_status, vaccine_out, is_demo, visit_time, phone) VALUES (?,?,?,NULL,'id_trc','hi','unverified',0,1,?,?)",
        (
            DEMO_CLINIC_ID,
            "Demo visitor desk",
            "Demo desk",
            DEMO_VISIT_TIME,
            DEMO_CLINIC_PHONES[DEMO_CLINIC_ID],
        ),
    )
    if cur.rowcount:
        audit.record(conn, DEMO_ACTOR, "clinic.create", "clinic", DEMO_CLINIC_ID, {"demo": True})


def record_missed_call(
    ctx: AppContext,
    number: str | None,
    transport: Literal["sim", "ari"],
    at: datetime,
    clinic_hint: str | None = None,
) -> MissedCallResult:
    norm = normalise_number(number, transport)
    events: _Events = []
    with ctx.db.transaction() as conn:
        if clinic_hint is not None:
            _clinic(conn, clinic_hint)
        if norm is None:
            mc_id = _insert_missed(
                ctx, conn, at=at, transport=transport, status="withheld", number=None, keep_cipher=False,
                clinic_hint=clinic_hint,
            )  # fmt: skip
            audit.record(conn, "system:telephony", "missed_call.withheld", "missed_call", mc_id)
            events.append(_missed_event(ctx, conn, mc_id))
            result = MissedCallResult(missed_call_id=mc_id, status="withheld")
        else:
            result = _classify(ctx, conn, norm, transport, at, clinic_hint, events)
    emit(ctx, events)
    return result


def _classify(
    ctx: AppContext,
    conn: sqlite3.Connection,
    number: str,
    transport: str,
    at: datetime,
    clinic_hint: str | None,
    events: _Events,
) -> MissedCallResult:
    last4 = ctx.vault.last4(number) or None
    course_id = _enrolled_course(ctx, conn, number, last4)
    if course_id is not None:
        return _second_call(ctx, conn, number, transport, at, course_id, clinic_hint, events)
    # Only the visitor path links at once: a phone opened by an anonymous visitor, with no clinic behind it. A
    # phone opened from a clinic's own view is that clinic's desk, and its missed call waits to be linked.
    if ctx.settings.is_demo and number.startswith("sim:") and clinic_hint is None:
        return _demo_autolink(ctx, conn, number, transport, at, events)
    if _unlinked_pending(ctx, conn, number, last4):
        mc_id = _insert_missed(
            ctx, conn, at=at, transport=transport, status="duplicate", number=number, keep_cipher=False,
            clinic_hint=clinic_hint,
        )  # fmt: skip
        audit.record(conn, "system:telephony", "missed_call.duplicate", "missed_call", mc_id)
        events.append(_missed_event(ctx, conn, mc_id))
        return MissedCallResult(missed_call_id=mc_id, status="duplicate")
    mc_id = _insert_missed(
        ctx, conn, at=at, transport=transport, status="unlinked", number=number, keep_cipher=True,
        clinic_hint=clinic_hint,
    )  # fmt: skip
    audit.record(conn, "system:telephony", "missed_call.received", "missed_call", mc_id)
    events.append(_missed_event(ctx, conn, mc_id))
    return MissedCallResult(missed_call_id=mc_id, status="unlinked")


def _enrolled_courses(ctx: AppContext, conn: sqlite3.Connection, number: str, last4: str | None) -> list[str]:
    """Every active course whose number is this one (a household can share a phone), newest first."""
    rows = conn.execute(
        "SELECT c.id, ct.phone_cipher FROM contacts ct JOIN courses c ON c.id = ct.course_id"
        " WHERE c.status='active' AND ct.phone_last4 = ? ORDER BY c.created_at DESC, c.id",
        (last4 or "",),
    ).fetchall()
    return [r["id"] for r in rows if _same_number(ctx, r["phone_cipher"], number)]


def _enrolled_course(ctx: AppContext, conn: sqlite3.Connection, number: str, last4: str | None) -> str | None:
    found = _enrolled_courses(ctx, conn, number, last4)
    return found[0] if found else None


def _unlinked_pending(ctx: AppContext, conn: sqlite3.Connection, number: str, last4: str | None) -> bool:
    rows = conn.execute(
        "SELECT phone_cipher FROM missed_calls WHERE status='unlinked' AND phone_cipher IS NOT NULL"
        " AND phone_last4 IS ?",
        (last4,),
    ).fetchall()
    return any(_same_number(ctx, r["phone_cipher"], number) for r in rows)


def same_phone_courses(ctx: AppContext, course_id: str) -> int:
    """How many active courses share this course's phone number. More than one means the household uses one
    phone, so a call names the register entry it is about."""
    row = ctx.db.one("SELECT phone_cipher, phone_last4 FROM contacts WHERE course_id=?", (course_id,))
    if row is None:
        return 0
    return len(
        _enrolled_courses(ctx, ctx.db.connect(), ctx.vault.decrypt(row["phone_cipher"]), row["phone_last4"])
    )


def _second_call(
    ctx: AppContext,
    conn: sqlite3.Connection,
    number: str,
    transport: str,
    at: datetime,
    course_id: str,
    clinic_hint: str | None,
    events: _Events,
) -> MissedCallResult:
    """A missed call from a number that already has a course.

    If the patient has not yet agreed to reminders (the first call was missed or never answered), the call is a
    patient asking for the first call again: it is placed again now, with no notice and no change to the
    course. Otherwise the person hears a short notice that they are signed up, and can press 9 on it to stop
    the calls. A missed call never stops anything by itself. The number is kept with the row for the same 48
    hours as any missed call, so staff can link it to a different register entry when the phone is shared."""
    delay = timedelta(seconds=ctx.policy["calls"]["first_call_delay_s"])
    is_demo = 1 if ctx.settings.is_demo else 0
    course = _course(conn, course_id)
    mc_id = _insert_missed(
        ctx, conn, at=at, transport=transport, status="duplicate", number=number, keep_cipher=True,
        course_id=course_id, clinic_hint=clinic_hint,
    )  # fmt: skip
    if course["consent_reminders_at"] is None and not _first_call_in_flight(conn, course_id):
        _rearm_first_call(conn, course_id, mc_id, clock.now() + delay, transport, is_demo, events)
    elif course["consent_reminders_at"] is not None:
        open_notice = conn.execute(
            "SELECT 1 FROM calls WHERE course_id=? AND kind='duplicate_notice'"
            " AND status IN ('scheduled','dialing','ringing','answered')",
            (course_id,),
        ).fetchone()
        if not open_notice:
            call_id = new_call(
                conn,
                course_id=course_id,
                kind="duplicate_notice",
                job_key=f"duplicate:{course_id}:{mc_id}",
                scheduled_for=clock.now() + delay,
                transport=transport,
                is_demo=is_demo,
            )
            if call_id:
                events.append(call_event(call_id, course_id, "duplicate_notice", "scheduled", transport))
    audit.record(
        conn, "system:telephony", "missed_call.duplicate", "missed_call", mc_id, {"course_id": course_id}
    )
    events.append(_missed_event(ctx, conn, mc_id))
    return MissedCallResult(missed_call_id=mc_id, status="duplicate", course_id=course_id)


_IN_FLIGHT = ("dialing", "ringing", "answered")


def _first_call_in_flight(conn: sqlite3.Connection, course_id: str) -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM calls WHERE course_id=? AND kind='first' AND status IN ({','.join('?' * len(_IN_FLIGHT))})",
            (course_id, *_IN_FLIGHT),
        ).fetchone()
        is not None
    )


def _rearm_first_call(
    conn: sqlite3.Connection,
    course_id: str,
    missed_call_id: str,
    when: datetime,
    transport: str,
    is_demo: int,
    events: _Events,
) -> None:
    """Make the first call due at `when`: bring a waiting one forward, or start a fresh set of attempts when
    the earlier ones were used up."""
    waiting = conn.execute(
        "SELECT id FROM calls WHERE course_id=? AND kind='first' AND status='scheduled'"
        " ORDER BY scheduled_for LIMIT 1",
        (course_id,),
    ).fetchone()
    if waiting is not None:
        conn.execute(
            "UPDATE calls SET scheduled_for=? WHERE id=? AND scheduled_for > ?",
            (clock.iso(when), waiting["id"], clock.iso(when)),
        )
        return
    call_id = new_call(
        conn,
        course_id=course_id,
        kind="first",
        job_key=f"first:{course_id}:{missed_call_id}",
        scheduled_for=when,
        transport=transport,
        is_demo=is_demo,
    )
    if call_id:
        events.append(call_event(call_id, course_id, "first", "scheduled", transport))


def _demo_autolink(
    ctx: AppContext, conn: sqlite3.Connection, number: str, transport: str, at: datetime, events: _Events
) -> MissedCallResult:
    _ensure_demo_clinic(conn)
    n = conn.execute("SELECT COUNT(*) FROM courses WHERE clinic_id=?", (DEMO_CLINIC_ID,)).fetchone()[0]
    data = EnrolIn(
        clinic_id=DEMO_CLINIC_ID,
        register_no=f"DEMO-{n + 1}",
        patient_type="new",
        regimen_id="id_trc",
        lang="hi",
        day0=_today(ctx).isoformat(),
        eligibility="adult",
    )
    course_id = _insert_course(ctx, conn, data, DEMO_ACTOR, "simulator", events)
    mc_id = _insert_missed(
        ctx, conn, at=at, transport=transport, status="unlinked", number=number, keep_cipher=True,
        clinic_hint=DEMO_CLINIC_ID,
    )  # fmt: skip
    _link(ctx, conn, mc_id, course_id, DEMO_ACTOR, events)
    return MissedCallResult(missed_call_id=mc_id, status="linked", course_id=course_id)


# --------------------------------------------------------------------- link
def _link(
    ctx: AppContext,
    conn: sqlite3.Connection,
    missed_call_id: str,
    course_id: str,
    actor: str,
    events: _Events,
) -> None:
    mc = conn.execute("SELECT * FROM missed_calls WHERE id=?", (missed_call_id,)).fetchone()
    if mc is None:
        raise EnrolError("not_found", "This missed call was not found.")
    if mc["status"] == "linked":
        raise EnrolError("already_linked", "This missed call is already linked to a patient.")
    # A repeat call from a number that already has a patient may still be linked to another register entry:
    # one phone can serve a household.
    if mc["status"] not in ("unlinked", "duplicate") or mc["phone_cipher"] is None:
        raise EnrolError("missed_call_closed", "This missed call can no longer be linked.")
    keep_hours = ctx.policy["retention"]["unlinked_missed_call_hours"]
    if clock.parse_iso(mc["at"]) <= clock.now() - timedelta(hours=keep_hours):
        raise EnrolError("missed_call_closed", f"This missed call is older than {keep_hours} hours.")
    course = _course(conn, course_id)
    if mc["clinic_hint"] is not None and mc["clinic_hint"] != course["clinic_id"]:
        raise EnrolError("wrong_clinic", "This missed call was made to a different clinic's number.")
    if mc["course_id"] == course_id:
        raise EnrolError("already_linked", "This missed call is from the number this patient already has.")
    if mc["status"] == "duplicate":
        holder = conn.execute("SELECT clinic_id FROM courses WHERE id=?", (mc["course_id"],)).fetchone()
        if holder is None or holder["clinic_id"] != course["clinic_id"]:
            raise EnrolError("wrong_clinic", "This number belongs to a patient of a different clinic.")
    if course["status"] in ("completed", "stopped"):
        raise EnrolError("course_closed", "This course has ended.")
    if course["status"] != "awaiting_call":
        raise EnrolError("not_awaiting", "This patient already has a phone number linked.")
    last_due = date.fromisoformat(
        conn.execute("SELECT MAX(due_date) FROM doses WHERE course_id=?", (course_id,)).fetchone()[0]
    )
    purge_days = ctx.policy["retention"]["phone_purge_days_after_last_dose"]
    purge_after = clock.at_local(
        last_due + timedelta(days=purge_days), ctx.policy["retention"]["purge_local_time"], _tz(ctx)
    )
    now = clock.now()
    conn.execute(
        "INSERT INTO contacts(course_id, phone_cipher, phone_last4, created_at, purge_after) VALUES (?,?,?,?,?)",
        (course_id, mc["phone_cipher"], mc["phone_last4"] or "", clock.iso(now), clock.iso(purge_after)),
    )
    conn.execute(
        "UPDATE missed_calls SET status='linked', course_id=?, phone_cipher=NULL WHERE id=?",
        (course_id, missed_call_id),
    )
    conn.execute("UPDATE courses SET status='active', stopped_reason=NULL WHERE id=?", (course_id,))
    conn.execute(
        "DELETE FROM kv WHERE key=?", (_ended_key(course_id),)
    )  # a new number answers "needs a new number"
    first_before = conn.execute(
        "SELECT 1 FROM calls WHERE course_id=? AND kind='first'", (course_id,)
    ).fetchone()
    call_id = new_call(
        conn,
        course_id=course_id,
        kind="first",
        job_key=f"first:{course_id}:{missed_call_id}" if first_before else f"first:{course_id}",
        scheduled_for=now + timedelta(seconds=ctx.policy["calls"]["first_call_delay_s"]),
        transport=mc["transport"],
        is_demo=course["is_demo"],
    )
    audit.record(conn, actor, "missed_call.link", "course", course_id, {"missed_call_id": missed_call_id})
    events.append(_missed_event(ctx, conn, missed_call_id))
    events.append(("course", {"course_id": course_id, "status": "active"}))
    if call_id:
        events.append(call_event(call_id, course_id, "first", "scheduled", mc["transport"]))


def link(ctx: AppContext, missed_call_id: str, course_id: str, actor: str) -> CourseOut:
    events: _Events = []
    with ctx.db.transaction() as conn:
        _link(ctx, conn, missed_call_id, course_id, actor, events)
    emit(ctx, events)
    return get_course(ctx, course_id)


# ------------------------------------------------------ withdraw, stop, vaccine
def _delete_place_clip(ctx: AppContext, conn: sqlite3.Connection, course_id: str, actor: str) -> bool:
    """A withdrawal takes the place recording and its transcript with it. A place nobody had decided yet
    becomes 'could not tell', so it leaves the review queue. If the file cannot be deleted the withdrawal
    fails as a whole (and can be repeated), because a clip that stays would break the promise made."""
    cap = conn.execute(
        "SELECT r.id AS report_id, r.location_status, pc.audio_path, pc.transcript, pc.decided_at,"
        " pc.clip_deleted_at, pc.report_id AS has_capture"
        " FROM bite_reports r LEFT JOIN place_captures pc ON pc.report_id = r.id WHERE r.course_id=?",
        (course_id,),
    ).fetchone()
    if cap is None:
        return False
    now = clock.iso(clock.now())
    changed = False
    if cap["has_capture"] and (cap["audio_path"] or cap["transcript"]):
        if cap["audio_path"]:
            delete_clip_file(ctx, cap["audio_path"])
        conn.execute(
            "UPDATE place_captures SET clip_deleted_at=COALESCE(clip_deleted_at, ?), audio_path=NULL,"
            " transcript=NULL, candidates_json=NULL WHERE report_id=?",
            (now, cap["report_id"]),
        )
        conn.execute(
            "INSERT INTO purge_log(course_id, what, reason, purged_at) VALUES (?, 'clip', 'withdrawn', ?)",
            (course_id, now),
        )
        audit.record(conn, actor, "clip.purge", "bite_report", cap["report_id"], {"reason": "withdrawn"})
        changed = True
    if cap["decided_at"] is None and cap["location_status"] in ("pending", "needs_review"):
        conn.execute("UPDATE bite_reports SET location_status='cant_tell' WHERE id=?", (cap["report_id"],))
        changed = True
    return changed


def _ended_key(course_id: str) -> str:
    return f"ended:{course_id}"


def _note_ended(conn: sqlite3.Connection, course_id: str, kind: str, reason: str) -> None:
    """Leave a note for the clinic that a patient ended the calls, until staff clear it (`ended_seen`)."""
    conn.execute(
        "INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)",
        (_ended_key(course_id), json.dumps({"kind": kind, "reason": reason, "at": clock.iso(clock.now())})),
    )


WithdrawKind = Literal["declined", "withdrawn", "wrong_number"]


def _withdraw(
    ctx: AppContext,
    conn: sqlite3.Connection,
    course_id: str,
    actor: str,
    reason: str,
    events: _Events,
    kind: WithdrawKind = "withdrawn",
) -> bool:
    """End the calls and delete the number. Returns False when there was nothing left to do.

    A patient who says the number is wrong (`wrong_number`) has not left the course: only the number goes, and
    the course goes back to waiting for a number, so the clinic can ask the patient for the right one. Every
    other kind stops the course and takes the place recording with it. When the patient did it by phone, the
    clinic's list keeps a note of it until staff clear it."""
    course = _course(conn, course_id)
    _cancel_scheduled(conn, course_id, "the patient withdrew", events)
    changed = False
    open_course = course["status"] in ("awaiting_call", "active")
    if open_course:
        status = "awaiting_call" if kind == "wrong_number" else "stopped"
        conn.execute("UPDATE courses SET status=?, stopped_reason=? WHERE id=?", (status, reason, course_id))
        events.append(("course", {"course_id": course_id, "status": status}))
        changed = True
    if conn.execute("DELETE FROM contacts WHERE course_id=?", (course_id,)).rowcount:
        conn.execute(
            "INSERT INTO purge_log(course_id, what, reason, purged_at) VALUES (?, 'phone', 'withdrawn', ?)",
            (course_id, clock.iso(clock.now())),
        )
        changed = True
    if kind != "wrong_number" and _delete_place_clip(ctx, conn, course_id, actor):
        changed = True
    if changed:
        audit.record(conn, actor, "course.withdraw", "course", course_id, {"reason": reason, "kind": kind})
        if open_course and actor.startswith("patient:"):
            _note_ended(conn, course_id, kind, reason)
    return changed


def withdraw(
    ctx: AppContext, course_id: str, by_actor: str, reason: str, kind: WithdrawKind = "withdrawn"
) -> None:
    events: _Events = []
    with ctx.db.transaction() as conn:
        _withdraw(ctx, conn, course_id, by_actor, reason, events, kind)
    emit(ctx, events)


def stop(ctx: AppContext, course_id: str, reason: str, actor: str) -> None:
    events: _Events = []
    with ctx.db.transaction() as conn:
        course = _course(conn, course_id)
        if course["status"] not in ("awaiting_call", "active"):
            raise EnrolError("course_closed", "This course has already ended.")
        conn.execute("UPDATE courses SET status='stopped', stopped_reason=? WHERE id=?", (reason, course_id))
        _cancel_scheduled(conn, course_id, "the clinic stopped the course", events)
        # The course is over, so the number leaves 7 days from now at the latest, not at the planned last dose.
        keep = clock.at_local(
            _today(ctx) + timedelta(days=ctx.policy["retention"]["phone_purge_days_after_last_dose"]),
            ctx.policy["retention"]["purge_local_time"],
            _tz(ctx),
        )
        conn.execute(
            "UPDATE contacts SET purge_after=? WHERE course_id=? AND (purge_after IS NULL OR purge_after > ?)",
            (clock.iso(keep), course_id, clock.iso(keep)),
        )
        audit.record(conn, actor, "course.stop", "course", course_id, {"reason": reason})
        events.append(("course", {"course_id": course_id, "status": "stopped"}))
    emit(ctx, events)


class EndedByPatient(BaseModel):
    """A patient ended the calls by phone (or the system gave up on a number): the clinic's note until cleared."""

    course_id: str
    register_no: str | None
    kind: Literal["declined", "withdrawn", "wrong_number", "no_consent"]
    reason: str
    at: str
    needs_new_number: (
        bool  # the course is still open and is waiting for a number the patient can be called on
    )


def drop_unconsented(ctx: AppContext, conn: sqlite3.Connection, course_id: str, events: _Events) -> bool:
    """The first call used all its attempts and the patient never agreed to reminders: the number goes now,
    not at the end of a course that never started. The course waits for a new missed call, and the clinic's
    list says so until staff clear it. Returns False when there was nothing to remove."""
    course = _course(conn, course_id)
    if course["status"] != "active" or course["consent_reminders_at"] is not None:
        return False
    if not conn.execute("DELETE FROM contacts WHERE course_id=?", (course_id,)).rowcount:
        return False
    now = clock.iso(clock.now())
    conn.execute(
        "INSERT INTO purge_log(course_id, what, reason, purged_at) VALUES (?, 'phone', 'no consent', ?)",
        (course_id, now),
    )
    conn.execute("UPDATE courses SET status='awaiting_call' WHERE id=?", (course_id,))
    _cancel_scheduled(
        conn, course_id, "the number was removed: the patient never agreed to reminders", events
    )
    reason = "the patient could not be reached to agree to reminders, so the number was removed"
    _note_ended(conn, course_id, "no_consent", reason)
    audit.record(conn, SCHEDULER_ACTOR, "phone.purge", "course", course_id, {"reason": "no consent"})
    events.append(("course", {"course_id": course_id, "status": "awaiting_call"}))
    return True


def ended_by_patient(ctx: AppContext, clinic_id: str) -> list[EndedByPatient]:
    """Patients who stopped the calls, said the number is wrong or could not be reached to agree, newest first,
    until the clinic clears each one with `ended_seen`."""
    out = []
    for r in ctx.db.query(
        "SELECT k.value, c.id, c.register_no, c.status FROM kv k JOIN courses c ON k.key = 'ended:' || c.id"
        " WHERE c.clinic_id=?",
        (clinic_id,),
    ):
        note = json.loads(r["value"])
        out.append(
            EndedByPatient(
                course_id=r["id"],
                register_no=r["register_no"],
                kind=note["kind"],
                reason=note["reason"],
                at=note["at"],
                needs_new_number=r["status"] == "awaiting_call",
            )
        )
    return sorted(out, key=lambda e: e.at, reverse=True)


def ended_seen(ctx: AppContext, course_id: str, actor: str) -> None:
    """Staff have read the note that a patient ended the calls: clear it (audited)."""
    with ctx.db.transaction() as conn:
        _course(conn, course_id)
        if not conn.execute("DELETE FROM kv WHERE key=?", (_ended_key(course_id),)).rowcount:
            raise EnrolError("not_found", "There is no such note to clear.")
        audit.record(conn, actor, "course.ended_seen", "course", course_id)


def set_vaccine_out(ctx: AppContext, clinic_id: str, out: bool, actor: str) -> None:
    events: _Events = []
    with ctx.db.transaction() as conn:
        clinic = _clinic(conn, clinic_id)
        if bool(clinic["vaccine_out"]) == out:
            return
        conn.execute(
            "UPDATE clinics SET vaccine_out=?, vaccine_out_since=? WHERE id=?",
            (1 if out else 0, clock.iso(clock.now()) if out else None, clinic_id),
        )
        audit.record(conn, actor, "clinic.vaccine_out", "clinic", clinic_id, {"out": out})
        events.append(("system", {"check": f"vaccine_out:{clinic_id}", "ok": not out}))
    emit(ctx, events)


# ------------------------------------------------------------- phone access
def reveal_phone(ctx: AppContext, course_id: str, actor: str) -> str:
    """Show the clinic a patient's number. Every reveal, and every refusal, is written to the audit log."""
    with ctx.db.transaction() as conn:
        _course(conn, course_id)
        contact = conn.execute("SELECT * FROM contacts WHERE course_id=?", (course_id,)).fetchone()
        refused = contact is None or (
            contact["purge_after"] is not None and clock.parse_iso(contact["purge_after"]) <= clock.now()
        )
        if refused:
            audit.record(conn, actor, "phone.reveal_refused", "course", course_id, {"why": "purged"})
        else:
            number = ctx.vault.decrypt(contact["phone_cipher"])
            audit.record(conn, actor, "phone.reveal", "course", course_id)
    if refused:
        raise EnrolError("phone_purged", "The phone number has been removed, as promised to the patient.")
    return number


def dial_number(ctx: AppContext, call_id: str) -> str:
    """For the call runner only: the number to dial for a call. Not for screens and never logged."""
    call = ctx.db.one("SELECT course_id FROM calls WHERE id=?", (call_id,))
    if call is None:
        raise EnrolError("not_found", "This call was not found.")
    row = ctx.db.one("SELECT phone_cipher, purge_after FROM contacts WHERE course_id=?", (call["course_id"],))
    if row is not None and row["purge_after"] and clock.parse_iso(row["purge_after"]) <= clock.now():
        row = None  # past its keep time: gone for every purpose, whether or not the purge job has run yet
    if row is None:
        raise EnrolError("phone_purged", "The phone number has been removed.")
    return ctx.vault.decrypt(row["phone_cipher"])


# --------------------------------------------------------------------- reads
def get_course(ctx: AppContext, course_id: str) -> CourseOut:
    c = ctx.db.one("SELECT * FROM courses WHERE id=?", (course_id,))
    if c is None:
        raise EnrolError("not_found", "This patient's course was not found.")
    contact = ctx.db.one("SELECT phone_last4 FROM contacts WHERE course_id=?", (course_id,))
    doses = ctx.db.query("SELECT * FROM doses WHERE course_id=? ORDER BY dose_no", (course_id,))
    calls = ctx.db.query(
        "SELECT * FROM calls WHERE course_id=? ORDER BY scheduled_for, attempt, id", (course_id,)
    )
    return CourseOut(
        id=c["id"],
        clinic_id=c["clinic_id"],
        register_no=c["register_no"],
        patient_type=c["patient_type"],
        regimen_id=c["regimen_id"],
        regimen_name=regimens.regimens()[c["regimen_id"]].name,
        day0=c["day0"],
        lang=c["lang"],
        status=c["status"],
        eligibility=c["eligibility"],
        phone_last4=(contact["phone_last4"] or None) if contact else None,
        phone_purged=contact is None and c["status"] != "awaiting_call",
        consent_reminders_at=c["consent_reminders_at"],
        doses=[
            DoseOut(
                dose_no=d["dose_no"],
                offset_days=d["offset_days"],
                due_date=d["due_date"],
                status=d["status"],
                given_on=d["given_on"],
                marked_by=d["marked_by"],
            )
            for d in doses
        ],
        calls=[
            CallOut(
                id=k["id"],
                kind=k["kind"],
                dose_no=k["dose_no"],
                scheduled_for=k["scheduled_for"],
                attempt=k["attempt"],
                status=k["status"],
                answered_at=k["answered_at"],
                ended_at=k["ended_at"],
                seconds_listened=k["seconds_listened"],
                error=k["error"],
            )
            for k in calls
        ],
        source=c["source"],
    )


_ATTEMPTED = ("completed", "answered", "no_answer", "failed", "ringing", "dialing")


def _call_summary(
    calls: list[sqlite3.Row], kinds: tuple[str, ...], dose_no: int
) -> tuple[int, str | None, bool]:
    """(attempts made, status of the latest attempt, whether another attempt is still waiting)."""
    mine = [c for c in calls if c["dose_no"] == dose_no and c["kind"] in kinds]
    done = sorted(
        (c for c in mine if c["status"] in _ATTEMPTED), key=lambda c: (c["scheduled_for"], c["attempt"])
    )
    waiting = any(c["status"] in ("scheduled", "dialing", "ringing") for c in mine)
    return len(done), (done[-1]["status"] if done else None), waiting


def patient_reported_doses(db, course_id: str) -> set[int]:
    """Doses the patient says they took, in a morning-after call. It is their word: the dose stays open until
    the clinic marks it given."""
    return {
        r["dose_no"]
        for r in db.query(
            "SELECT DISTINCT c.dose_no FROM answers a JOIN calls c ON c.id = a.call_id"
            " WHERE c.course_id=? AND a.question='dose_taken' AND a.value='yes' AND c.dose_no IS NOT NULL",
            (course_id,),
        )
    }


def _due_state(
    dose: sqlite3.Row, attempts: int, last: str | None, waiting: bool, reported: bool = False
) -> str:
    if dose["status"] == "given":
        return "marked_given" if dose["marked_by"] == "clinic" else "patient_says_taken"
    if reported:
        return "patient_says_taken"
    if last in ("completed", "answered"):
        return "reminded"
    if last in ("no_answer", "failed"):
        return "tried" if waiting else "no_answer"
    return "due_today"


def clinic_today(ctx: AppContext, clinic_id: str) -> ClinicToday:
    """What the clinic sees today. One row per patient; counts are the lengths of the lists."""
    refresh_states(ctx)
    today = _today(ctx)
    clinic = ctx.db.one("SELECT * FROM clinics WHERE id=?", (clinic_id,))
    if clinic is None:
        raise EnrolError("unknown_clinic", "This clinic is not in the system.")
    courses = ctx.db.query(
        "SELECT c.*, ct.phone_last4 AS last4 FROM courses c LEFT JOIN contacts ct ON ct.course_id = c.id"
        " WHERE c.clinic_id=? AND c.status IN ('active','awaiting_call') ORDER BY c.register_no, c.id",
        (clinic_id,),
    )
    due_today: list[ClinicRow] = []
    missed: list[ClinicRow] = []
    entered: list[tuple[str, ClinicRow]] = []  # (when the entry was made, row): newest first below
    next_due: str | None = None
    for c in courses:
        doses = ctx.db.query("SELECT * FROM doses WHERE course_id=? ORDER BY dose_no", (c["id"],))
        calls = ctx.db.query("SELECT * FROM calls WHERE course_id=?", (c["id"],))
        pending = [d for d in doses if d["status"] in PENDING]
        reported = patient_reported_doses(ctx.db, c["id"])

        def row(
            dose: sqlite3.Row | None, state: str, attempts: int = 0, last: str | None = None, c=c
        ) -> ClinicRow:
            days_late = (today - date.fromisoformat(dose["due_date"])).days if dose else None
            return ClinicRow(
                course_id=c["id"],
                register_no=c["register_no"],
                phone_last4=(c["last4"] or None),
                regimen_id=c["regimen_id"],
                next_dose_no=dose["dose_no"] if dose else None,
                next_due_date=dose["due_date"] if dose else None,
                state=state,
                attempts=attempts,
                last_call_status=last,
                lang=c["lang"],
                source=c["source"],
                created_at=c["created_at"],
                days_late=days_late if days_late and days_late > 0 else None,
            )

        if c["status"] == "awaiting_call":
            entered.append((c["created_at"], row(pending[0] if pending else None, "awaiting_call")))
            continue
        future = [d["due_date"] for d in pending if d["due_date"] > today.isoformat()]
        if future:
            next_due = min([next_due, *future]) if next_due else min(future)
        late = [d for d in pending if d["status"] in ("late_minor", "late_review")]
        if late:
            d = late[0]
            attempts, last, _ = _call_summary(calls, ("missed_dose",), d["dose_no"])
            missed.append(
                row(d, "patient_says_taken" if d["dose_no"] in reported else d["status"], attempts, last)
            )
            continue
        # dose 1 is the enrolment visit itself, so it never makes a patient "due today"
        todays = [
            d
            for d in doses
            if d["dose_no"] > 1 and d["due_date"] == today.isoformat() and d["status"] != "not_needed"
        ]
        if todays:
            d = todays[0]
            attempts, last, waiting = _call_summary(calls, ("reminder",), d["dose_no"])
            due_today.append(
                row(d, _due_state(d, attempts, last, waiting, d["dose_no"] in reported), attempts, last)
            )
    # A patient who says they took it comes first, to be confirmed in a moment. The rest are newest first: the
    # dose missed yesterday can still be saved, one missed months ago needs a different conversation.
    missed.sort(key=lambda r: r.register_no or "")
    missed.sort(key=lambda r: r.next_due_date or "", reverse=True)
    missed.sort(key=lambda r: r.state != "patient_says_taken")
    awaiting = [r for _, r in sorted(entered, key=lambda w: w[0], reverse=True)]
    cutoff = clock.now() - timedelta(hours=ctx.policy["retention"]["unlinked_missed_call_hours"])
    # A repeat call from a number that already has a patient is offered too, when there is an entry waiting for
    # a number: it may be another member of the same household.
    unlinked = [
        UnlinkedMissedCall(
            id=m["id"],
            at=m["at"],
            phone_last4=m["phone_last4"],
            transport=m["transport"],
            already_enrolled_register_no=m["enrolled_register_no"],
        )
        for m in ctx.db.query(
            "SELECT m.*, c.register_no AS enrolled_register_no FROM missed_calls m"
            " LEFT JOIN courses c ON c.id = m.course_id AND m.status = 'duplicate'"
            " WHERE (m.status='unlinked' OR (m.status='duplicate' AND m.phone_cipher IS NOT NULL AND ?"
            " AND c.clinic_id=?)) AND (m.clinic_hint IS NULL OR m.clinic_hint=?) AND m.at > ? ORDER BY m.at",
            (1 if awaiting else 0, clinic_id, clinic_id, clock.iso(cutoff)),
        )
    ]
    callbacks = [
        CallbackRequestOut(
            id=r["id"],
            course_id=r["course_id"],
            register_no=r["register_no"],
            ward_no=r["ward_no"],
            requested_at=r["requested_at"],
            status=r["status"],
        )
        for r in ctx.db.query(
            "SELECT cb.id, cb.course_id, cb.requested_at, cb.status, c.register_no, a.ward_no"
            " FROM callback_requests cb JOIN courses c ON c.id = cb.course_id"
            " LEFT JOIN alerts a ON a.id = cb.alert_id"
            " WHERE cb.status='open' AND c.clinic_id=? ORDER BY cb.requested_at",
            (clinic_id,),
        )
    ]
    counts = {
        "due_today": len(due_today),
        "missed": len(missed),
        "late_review": sum(1 for r in missed if r.state == "late_review"),
        "awaiting_call": len(awaiting),
        "unlinked": len(unlinked),
        "callbacks": len(callbacks),
    }
    return ClinicToday(
        clinic_id=clinic_id,
        clinic_name=clinic["name"],
        local_date=today.isoformat(),
        vaccine_out=bool(clinic["vaccine_out"]),
        vaccine_out_since=clinic["vaccine_out_since"],
        counts=counts,
        due_today=due_today,
        missed=missed,
        awaiting_call=awaiting,
        unlinked=unlinked,
        callbacks=callbacks,
        next_due_date=next_due,
        ended=ended_by_patient(ctx, clinic_id),
    )
