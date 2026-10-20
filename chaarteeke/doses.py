"""Dose states and marking a dose given.

Late states follow DOMAIN R5 with the numbers in policy.doses. They only decide who appears on the
clinic's list: nothing here moves a later dose, adds a dose or restarts a course (the calendar stays
the one from day 0).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date
from typing import Any, Literal

from . import audit, clock
from .context import AppContext
from .models import DoseOut

PENDING = ("due", "late_minor", "late_review")
SYSTEM_ACTOR = "system:scheduler"


class DoseError(ValueError):
    """A dose change the server refuses. `code` is machine readable, `str(e)` is plain words."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def emit(ctx: AppContext, events: Iterable[tuple[str, dict[str, Any]]]) -> None:
    """Publish collected events. Call only after the transaction that produced them has committed."""
    for type_, data in events:
        ctx.bus.publish(type_, data)


def dose_out(row: sqlite3.Row) -> DoseOut:
    return DoseOut(
        dose_no=row["dose_no"],
        offset_days=row["offset_days"],
        due_date=row["due_date"],
        status=row["status"],
        given_on=row["given_on"],
        marked_by=row["marked_by"],
    )


def _base_state(ctx: AppContext, days_late: int) -> str:
    pol = ctx.policy["doses"]
    if days_late >= pol["late_review_from_days"]:
        return "late_review"
    if days_late >= pol["late_minor_days"][0]:
        return "late_minor"
    return "due"


def _course_states(ctx: AppContext, day0: date, doses: list[sqlite3.Row], today: date) -> dict[int, str]:
    """The state every pending dose of one course should have on `today`."""
    pol = ctx.policy["doses"]
    given = sum(1 for d in doses if d["status"] == "given")
    since_day0 = (today - day0).days
    behind = False
    if len(doses) >= 3 and since_day0 > pol["three_doses_by_day"] and given < 3:
        behind = True
    if since_day0 > pol["all_but_last_by_day"] and given < len(doses) - 1:
        behind = True
    out: dict[int, str] = {}
    for d in doses:
        if d["status"] not in PENDING:
            continue
        days_late = (today - date.fromisoformat(d["due_date"])).days
        state = _base_state(ctx, days_late)
        if behind and days_late >= 0:
            state = "late_review"
        out[d["dose_no"]] = state
    return out


def refresh_states(ctx: AppContext, course_id: str | None = None, today: date | None = None) -> int:
    """Recompute late_minor / late_review for active courses; returns the number of rows changed."""
    today = today or clock.local_today(ctx.settings.local_tz)
    changed: list[tuple[str, int, str]] = []
    with ctx.db.transaction() as conn:
        sql = "SELECT id, day0 FROM courses WHERE status='active'"
        params: tuple = ()
        if course_id is not None:
            sql += " AND id=?"
            params = (course_id,)
        for c in conn.execute(sql, params).fetchall():
            doses = conn.execute(
                "SELECT * FROM doses WHERE course_id=? ORDER BY dose_no", (c["id"],)
            ).fetchall()
            wanted = _course_states(ctx, date.fromisoformat(c["day0"]), doses, today)
            for d in doses:
                new = wanted.get(d["dose_no"])
                if new is not None and new != d["status"]:
                    conn.execute(
                        "UPDATE doses SET status=? WHERE course_id=? AND dose_no=?",
                        (new, c["id"], d["dose_no"]),
                    )
                    changed.append((c["id"], d["dose_no"], new))
        if changed:
            # one row per run: the states are derived from the clock, so there is no actor to name
            audit.record(conn, SYSTEM_ACTOR, "doses.refresh", "dose", None, {"changed": len(changed)})
    emit(ctx, (("dose", {"course_id": c, "dose_no": n, "status": s}) for c, n, s in changed))
    return len(changed)


def mark_given(
    ctx: AppContext,
    course_id: str,
    dose_no: int,
    by: Literal["clinic", "patient"],
    on: date,
    actor: str,
) -> DoseOut:
    """Record that a dose was given. Marking a dose that is already given changes nothing."""
    today = clock.local_today(ctx.settings.local_tz)
    events: list[tuple[str, dict[str, Any]]] = []
    with ctx.db.transaction() as conn:
        course = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        if course is None:
            raise DoseError("not_found", "This patient's course was not found.")
        if course["status"] != "active":
            raise DoseError("course_not_active", "This course is not active, so a dose cannot be marked.")
        dose = conn.execute(
            "SELECT * FROM doses WHERE course_id=? AND dose_no=?", (course_id, dose_no)
        ).fetchone()
        if dose is None:
            raise DoseError("not_found", "This course has no such dose.")
        if dose["status"] == "given":
            return dose_out(dose)
        if dose["status"] == "not_needed":
            raise DoseError("dose_not_needed", "This dose is marked as not needed.")
        if date.fromisoformat(dose["due_date"]) > today:
            raise DoseError("dose_in_future", "This dose is not due yet, so it cannot be marked as given.")
        if on > today:
            raise DoseError("date_in_future", "A dose cannot be marked as given on a day that has not come.")
        if on < date.fromisoformat(course["day0"]):
            raise DoseError("bad_date", "A dose cannot be given before the first dose date.")
        conn.execute(
            "UPDATE doses SET status='given', given_on=?, marked_by=? WHERE course_id=? AND dose_no=?",
            (on.isoformat(), by, course_id, dose_no),
        )
        events.append(("dose", {"course_id": course_id, "dose_no": dose_no, "status": "given"}))
        for call in conn.execute(
            "SELECT id, kind, transport FROM calls WHERE course_id=? AND dose_no=? AND status='scheduled'"
            " AND kind IN ('reminder','missed_dose')",
            (course_id, dose_no),
        ).fetchall():
            conn.execute(
                "UPDATE calls SET status='cancelled', error='the dose was marked as given' WHERE id=?",
                (call["id"],),
            )
            events.append(
                (
                    "call",
                    {
                        "call_id": call["id"],
                        "course_id": course_id,
                        "kind": call["kind"],
                        "status": "cancelled",
                        "simulated": call["transport"] == "sim",
                    },
                )
            )
        left = conn.execute(
            "SELECT COUNT(*) FROM doses WHERE course_id=? AND status IN ('due','late_minor','late_review')",
            (course_id,),
        ).fetchone()[0]
        if left == 0:
            conn.execute(
                "UPDATE courses SET status='completed', completed_at=? WHERE id=?",
                (clock.iso(clock.now()), course_id),
            )
            events.append(("course", {"course_id": course_id, "status": "completed"}))
        audit.record(
            conn,
            actor,
            "dose.given",
            "course",
            course_id,
            {"dose_no": dose_no, "by": by, "on": on.isoformat()},
        )
        row = conn.execute(
            "SELECT * FROM doses WHERE course_id=? AND dose_no=?", (course_id, dose_no)
        ).fetchone()
    emit(ctx, events)
    return dose_out(row)


def next_due(ctx: AppContext, course_id: str) -> DoseOut | None:
    """The first dose of the course that is not yet given (it may already be late)."""
    row = ctx.db.one(
        "SELECT * FROM doses WHERE course_id=? AND status IN ('due','late_minor','late_review')"
        " ORDER BY dose_no LIMIT 1",
        (course_id,),
    )
    return dose_out(row) if row else None
