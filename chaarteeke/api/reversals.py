"""Take back a one-tap write: a dose marked given, a missed call linked to a patient, a call-back closed.

Each is for a slip of the thumb at the clinic desk, so each is allowed only while it is still a slip:

- a dose, for policy clinic.undo_dose_minutes after it was marked, and only a dose the clinic marked
  (a dose the patient confirmed by phone stays);
- a link, until the first call has been placed (it is planned `calls.first_call_delay_s` after the link);
- a call-back, for policy clinic.undo_callback_minutes after it was closed.

A reversal puts the rows back as they were and writes its own audit row; the first write's row stays. Late
states are worked out again from the clock, as after any change. Each function runs in one transaction and
publishes its events after the commit.
"""

from __future__ import annotations

from datetime import timedelta

from .. import alerts, audit, clock
from ..context import AppContext
from ..doses import dose_out, emit, refresh_states
from ..enrol import call_event
from ..models import CallbackRequestOut, DoseOut
from .auth import AuthError

DOSE_CALLS_CANCELLED = (
    "the dose was marked as given"  # what doses.mark_given writes on the reminders it cancels
)


def _refuse(code: str, message: str, status: int = 409) -> AuthError:
    return AuthError(code, message, status)


def _minutes(minutes: int) -> str:
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


# ------------------------------------------------------------------------ a dose
def unmark_dose(ctx: AppContext, course_id: str, dose_no: int, actor: str) -> DoseOut:
    window = ctx.policy["clinic"]["undo_dose_minutes"]
    events: list = []
    with ctx.db.transaction() as conn:
        dose = conn.execute(
            "SELECT d.*, c.status AS course_status FROM doses d JOIN courses c ON c.id = d.course_id"
            " WHERE d.course_id = ? AND d.dose_no = ?",
            (course_id, dose_no),
        ).fetchone()
        if dose is None:
            raise _refuse("not_found", "This course has no such dose.", 404)
        if dose["status"] != "given":
            raise _refuse("nothing_to_undo", "This dose is not marked as given.")
        if dose["marked_by"] != "clinic":
            raise _refuse("nothing_to_undo", "The patient confirmed this dose by phone, so it stays.")
        if dose["course_status"] not in ("active", "completed"):
            raise _refuse("course_closed", "This course has ended.")
        marked = conn.execute(
            "SELECT at FROM audit WHERE action = 'dose.given' AND object_type = 'course' AND object_id = ?"
            " AND json_extract(detail_json, '$.dose_no') = ? ORDER BY seq DESC LIMIT 1",
            (course_id, dose_no),
        ).fetchone()
        if marked is None:  # given at enrolment, or by an earlier version: nothing recent to take back
            raise _refuse("nothing_to_undo", "There is nothing to undo for this dose.")
        if clock.parse_iso(marked["at"]) < clock.now() - timedelta(minutes=window):
            raise _refuse(
                "undo_window_over",
                f"This dose was marked more than {_minutes(window)} ago, so it can't be undone here.",
            )
        conn.execute(
            "UPDATE doses SET status = 'due', given_on = NULL, marked_by = NULL WHERE course_id = ? AND dose_no = ?",
            (course_id, dose_no),
        )
        if dose["course_status"] == "completed":
            conn.execute(
                "UPDATE courses SET status = 'active', completed_at = NULL WHERE id = ?", (course_id,)
            )
            events.append(("course", {"course_id": course_id, "status": "active"}))
        for call in conn.execute(
            "SELECT id, kind, transport FROM calls WHERE course_id = ? AND dose_no = ? AND status = 'cancelled'"
            " AND error = ?",
            (course_id, dose_no, DOSE_CALLS_CANCELLED),
        ).fetchall():
            conn.execute("UPDATE calls SET status = 'scheduled', error = NULL WHERE id = ?", (call["id"],))
            events.append(call_event(call["id"], course_id, call["kind"], "scheduled", call["transport"]))
        audit.record(
            conn,
            actor,
            "dose.unmarked",
            "course",
            course_id,
            {"dose_no": dose_no, "was_on": dose["given_on"]},
        )
        events.append(("dose", {"course_id": course_id, "dose_no": dose_no, "status": "due"}))
    emit(ctx, events)
    refresh_states(ctx, course_id)  # a dose that was due long ago is late again
    row = ctx.db.one("SELECT * FROM doses WHERE course_id = ? AND dose_no = ?", (course_id, dose_no))
    return dose_out(row)


# ----------------------------------------------------------------------- a link
def unlink_missed_call(ctx: AppContext, missed_call_id: str, actor: str) -> str:
    """Return the id of the course that is waiting for a call again. The number goes back from the patient's
    contact row to the missed call, and the first call, which was never placed, is deleted so the next link
    can plan it under the same job key."""
    events: list = []
    with ctx.db.transaction() as conn:
        mc = conn.execute("SELECT * FROM missed_calls WHERE id = ?", (missed_call_id,)).fetchone()
        if mc is None:
            raise _refuse("not_found", "This missed call was not found.", 404)
        if mc["status"] != "linked" or mc["course_id"] is None:
            raise _refuse("nothing_to_undo", "This missed call is not linked to a patient.")
        course_id = mc["course_id"]
        first = conn.execute(
            "SELECT id, transport FROM calls WHERE course_id = ? AND kind = 'first' AND status = 'scheduled'",
            (course_id,),
        ).fetchone()
        contact = conn.execute("SELECT * FROM contacts WHERE course_id = ?", (course_id,)).fetchone()
        if first is None or contact is None:
            raise _refuse("already_placed", "The first call has been placed, so this link can't be undone.")
        conn.execute(
            "UPDATE missed_calls SET status = 'unlinked', course_id = NULL, phone_cipher = ? WHERE id = ?",
            (contact["phone_cipher"], missed_call_id),
        )
        conn.execute("DELETE FROM contacts WHERE course_id = ?", (course_id,))
        conn.execute("DELETE FROM calls WHERE id = ?", (first["id"],))
        conn.execute("UPDATE courses SET status = 'awaiting_call' WHERE id = ?", (course_id,))
        audit.record(
            conn, actor, "missed_call.unlink", "course", course_id, {"missed_call_id": missed_call_id}
        )
        events.append(
            (
                "missed_call",
                {"id": missed_call_id, "status": "unlinked", "phone_last4": mc["phone_last4"]},
            )
        )
        events.append(("course", {"course_id": course_id, "status": "awaiting_call"}))
        events.append(call_event(first["id"], course_id, "first", "cancelled", first["transport"]))
    emit(ctx, events)
    return course_id


# ------------------------------------------------------------------- a call-back
def reopen_callback(ctx: AppContext, callback_id: str, clinic_id: str, actor: str) -> CallbackRequestOut:
    window = ctx.policy["clinic"]["undo_callback_minutes"]
    with ctx.db.transaction() as conn:
        cb = conn.execute(
            "SELECT cb.*, a.status AS alert_status, a.cluster_id, a.ward_no, rep.clinic_id AS clinic_id"
            " FROM callback_requests cb JOIN alerts a ON a.id = cb.alert_id"
            " JOIN bite_reports rep ON rep.id = a.report_id WHERE cb.id = ?",
            (callback_id,),
        ).fetchone()
        if cb is None:
            raise _refuse("not_found", "There is no such call-back.", 404)
        if cb["clinic_id"] != clinic_id:
            raise _refuse("forbidden", "This call-back belongs to another clinic.", 403)
        if cb["status"] == "open":
            raise _refuse("nothing_to_undo", "This call-back is still open.")
        if cb["done_at"] is None or clock.parse_iso(cb["done_at"]) < clock.now() - timedelta(minutes=window):
            raise _refuse(
                "undo_window_over",
                f"This call-back was closed more than {_minutes(window)} ago, so it can't be reopened here.",
            )
        if conn.execute(
            "SELECT 1 FROM callback_requests WHERE alert_id = ? AND status = 'open'", (cb["alert_id"],)
        ).fetchone():
            raise _refuse("already_open", "The vet has asked again, so a call-back is already open.")
        conn.execute(
            "UPDATE callback_requests SET status = 'open', done_at = NULL, note = NULL WHERE id = ?",
            (callback_id,),
        )
        audit.record(conn, actor, "callback.reopen", "callback", callback_id, {"was": cb["status"]})
    ctx.bus.publish(
        "alert",
        {
            "alert_id": cb["alert_id"],
            "status": cb["alert_status"],
            "cluster_id": cb["cluster_id"],
            "ward_no": cb["ward_no"],
            "callback": "open",
        },
    )
    ctx.bus.publish("callback", {"callback_id": callback_id, "alert_id": cb["alert_id"], "status": "open"})
    return next(c for c in alerts.callbacks_for_clinic(ctx, clinic_id, "open") if c.id == callback_id)
