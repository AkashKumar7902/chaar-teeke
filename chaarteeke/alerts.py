"""Sick-dog alerts: made from a report that says the dog showed signs, worked by vets, and the
callback request that lets a vet ask the clinic to phone the patient (the vet never sees the number).

An alert carries ward, cluster, place type and local day only (DOMAIN R16). Every transition is checked
here, on the server: who may do it and from which state.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from . import audit, clock
from .context import AppContext
from .ids import new_id
from .models import AlertOut, CallbackRequestOut
from .stats.clusters import ward_table
from .stats.errors import StatsError

STATUSES = ("new", "claimed", "acted", "not_acted")
CALLBACK_OUTCOMES = ("done", "declined", "closed_no_contact")


class AlertError(StatsError):
    """code: not_found, forbidden, read_only, already_claimed, already_closed, not_claimed, not_yours,
    note_needed, no_course, bad_status."""


def _who(ctx: AppContext, actor: str, role: str) -> sqlite3.Row:
    """The staff row behind a `staff:<username>` actor, which must hold `role`."""
    if actor.startswith("visitor:"):
        raise AlertError("read_only", "Your role can't do this")
    if not actor.startswith("staff:"):
        raise AlertError("forbidden", "Your role can't do this")
    row = ctx.db.one("SELECT * FROM staff WHERE username = ? AND active = 1", (actor[len("staff:") :],))
    if row is None or row["role"] != role:
        raise AlertError("forbidden", "Your role can't do this")
    return row


def _day_of(ctx: AppContext, instant: str | None) -> str | None:
    """The local calendar day of a stored instant. An alert is served with days only: the second an alert
    was made or taken, set beside a patient's own memory of when they answered, would point to the
    patient. The exact times stay in the table for the unseen check and the audit trail."""
    if instant is None:
        return None
    return clock.parse_iso(instant).astimezone(clock.local_tz(ctx.settings.local_tz)).date().isoformat()


def _out(ctx: AppContext, r: sqlite3.Row, viewer: str | None = None) -> AlertOut:
    # AlertOut (models.py) still names created_at and claimed_at; they carry the day, never a time of day.
    # Only an alert made by the visitor's own phone (source simulator) also says when, in `made_at`.
    simulated = r["source"] == "simulator"
    info = ward_table(ctx.settings.data_dir / "wards.geojson").get(r["ward_no"]) if r["ward_no"] else None
    return AlertOut(
        id=r["id"],
        ward_no=r["ward_no"],
        ward_name=info.name if info else None,
        cluster_id=r["cluster_id"],
        place_type=r["place_type"],
        local_day=r["local_day"],
        status=r["status"],
        claimed_by=r["claimed_by"],
        claimed_at=_day_of(ctx, r["claimed_at"]),
        outcome_note=r["outcome_note"],
        created_at=r["local_day"],
        made_at=r["created_at"] if simulated else None,
        location_status=r["location_status"],
        callback_status=r["cb_status"],
        simulated=simulated,
        claimed_by_me=viewer is not None and r["claimed_by"] == viewer,
    )


_SELECT = (
    "SELECT a.*, r.source AS source, r.location_status AS location_status, "
    "(SELECT cb.status FROM callback_requests cb WHERE cb.alert_id = a.id "
    " ORDER BY cb.requested_at DESC, cb.id DESC LIMIT 1) AS cb_status "
    "FROM alerts a JOIN bite_reports r ON r.id = a.report_id "
)


def get(ctx: AppContext, alert_id: str, viewer: str | None = None) -> AlertOut:
    """`viewer` is the actor string of the person asking; it only decides `claimed_by_me`."""
    r = ctx.db.one(_SELECT + "WHERE a.id = ?", (alert_id,))
    if r is None:
        raise AlertError("not_found", "There is no such alert")
    return _out(ctx, r, viewer)


def list_alerts(ctx: AppContext, status: str | None = None, viewer: str | None = None) -> list[AlertOut]:
    if status is not None and status not in STATUSES:
        raise AlertError("bad_status", "That is not an alert status")
    where, params = ("WHERE a.status = ? ", (status,)) if status else ("", ())
    rows = ctx.db.query(_SELECT + where + "ORDER BY a.created_at DESC, a.id DESC", params)
    return [_out(ctx, r, viewer) for r in rows]


def unseen(ctx: AppContext) -> list[AlertOut]:
    """New alerts no vet has opened yet after the policy time: the one case shown as a system fault.

    In demo mode an alert made by a visitor's own phone (source simulator) is left out: nobody is meant to be
    on duty for it, and a demo clock jump would otherwise turn a fault banner on in front of the visitors. A
    real alert (pilot mode, or demo history) is always counted."""
    minutes = ctx.policy["alerts"]["unseen_by_vet_minutes"]
    cutoff = clock.iso(clock.now() - timedelta(minutes=minutes))
    skip = " AND r.source != 'simulator'" if ctx.settings.is_demo else ""
    rows = ctx.db.query(
        _SELECT
        + "WHERE a.status = 'new' AND a.first_seen_by_vet_at IS NULL AND a.created_at <= ?"
        + skip
        + " ORDER BY a.created_at, a.id",
        (cutoff,),
    )
    return [_out(ctx, r) for r in rows]


def _publish_callback(ctx: AppContext, cb_id: str, alert_id: str, status: str) -> None:
    """Ids and the status only: the clinic's stream filters by the call-back's own clinic, and no stream
    carries a course or a patient."""
    ctx.bus.publish("callback", {"callback_id": cb_id, "alert_id": alert_id, "status": status})


def _publish(
    ctx: AppContext, alert_id: str, status: str, cluster_id: str | None, ward_no: int | None, **more
):
    ctx.bus.publish(
        "alert",
        {"alert_id": alert_id, "status": status, "cluster_id": cluster_id, "ward_no": ward_no, **more},
    )


# ------------------------------------------------------------------------ creation
def from_report(ctx: AppContext, conn: sqlite3.Connection, report_id: str) -> str | None:
    """Make the alert for a report whose dog showed signs. Returns its id, or None for any other answer.

    Runs inside the caller's transaction. The event goes out straight away, so the page should treat it
    as a hint and read the list again.
    """
    r = conn.execute("SELECT * FROM bite_reports WHERE id = ?", (report_id,)).fetchone()
    if r is None:
        raise AlertError("not_found", "There is no such report")
    if r["signs"] != "yes":
        return None
    have = conn.execute("SELECT id FROM alerts WHERE report_id = ?", (report_id,)).fetchone()
    if have:
        return have["id"]
    cluster = r["cluster_id"]
    if cluster is None and r["ward_no"] is not None and ctx.clusters is not None:
        cluster = ctx.clusters.for_ward(r["ward_no"])
    made = clock.parse_iso(r["created_at"])
    local_day = made.astimezone(clock.local_tz(ctx.settings.local_tz)).date().isoformat()
    alert_id = new_id("alr")
    conn.execute(
        "INSERT INTO alerts(id, report_id, ward_no, cluster_id, place_type, local_day, status, created_at, is_demo) "
        "VALUES (?,?,?,?,?,?, 'new', ?, ?)",
        (
            alert_id,
            report_id,
            r["ward_no"],
            cluster,
            r["place_type"],
            local_day,
            clock.iso(clock.now()),
            r["is_demo"],
        ),
    )
    actor = f"patient:{r['course_id']}" if r["course_id"] else "system:ivr"
    audit.record(
        conn, actor, "alert.create", "alert", alert_id, {"ward_no": r["ward_no"], "cluster_id": cluster}
    )
    _publish(ctx, alert_id, "new", cluster, r["ward_no"])
    return alert_id


def update_location(ctx: AppContext, conn: sqlite3.Connection, report_id: str) -> None:
    """After the reviewer places a report, copy its ward and cluster onto the report's alert."""
    r = conn.execute("SELECT ward_no, cluster_id FROM bite_reports WHERE id = ?", (report_id,)).fetchone()
    if r is None:
        raise AlertError("not_found", "There is no such report")
    cluster = r["cluster_id"]
    if cluster is None and r["ward_no"] is not None and ctx.clusters is not None:
        cluster = ctx.clusters.for_ward(r["ward_no"])
    alert = conn.execute(
        "SELECT id FROM alerts WHERE report_id = ? AND (ward_no IS NOT ? OR cluster_id IS NOT ?)",
        (report_id, r["ward_no"], cluster),
    ).fetchone()
    if alert is None:
        return
    conn.execute(
        "UPDATE alerts SET ward_no = ?, cluster_id = ? WHERE id = ?", (r["ward_no"], cluster, alert["id"])
    )
    audit.record(
        conn,
        "system:review",
        "alert.place",
        "alert",
        alert["id"],
        {"ward_no": r["ward_no"], "cluster_id": cluster},
    )


# ------------------------------------------------------------------ vet transitions
def _transition(ctx: AppContext, alert_id: str, actor: str, expect: str, new_status: str, note: str | None):
    _who(ctx, actor, "vet")
    with ctx.db.transaction() as conn:
        r = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if r is None:
            raise AlertError("not_found", "There is no such alert")
        if r["status"] in ("acted", "not_acted"):
            raise AlertError("already_closed", "This alert is already closed")
        if new_status == "claimed":
            if r["status"] == "claimed":
                if r["claimed_by"] == actor:
                    return r["cluster_id"], r["ward_no"], False  # a second tap changes nothing
                raise AlertError("already_claimed", "Another vet is already on this alert")
        else:
            if r["status"] != expect:
                raise AlertError("not_claimed", "Say you are on it before closing this alert")
            if r["claimed_by"] != actor:
                raise AlertError("not_yours", "Another vet is on this alert")
        now = clock.iso(clock.now())
        if new_status == "claimed":
            conn.execute(
                "UPDATE alerts SET status = 'claimed', claimed_by = ?, claimed_at = ?, "
                "first_seen_by_vet_at = COALESCE(first_seen_by_vet_at, ?) WHERE id = ?",
                (actor, now, now, alert_id),
            )
        else:
            conn.execute(
                "UPDATE alerts SET status = ?, closed_at = ?, outcome_note = ? WHERE id = ?",
                (new_status, now, note, alert_id),
            )
        audit.record(conn, actor, f"alert.{new_status}", "alert", alert_id, {"note": note} if note else None)
        return r["cluster_id"], r["ward_no"], True


def _finish(ctx: AppContext, alert_id: str, actor: str, expect: str, new_status: str, note: str | None):
    cluster, ward, changed = _transition(ctx, alert_id, actor, expect, new_status, note)
    if changed:
        _publish(ctx, alert_id, new_status, cluster, ward)
    return get(ctx, alert_id, actor)


def claim(ctx: AppContext, alert_id: str, actor: str, note: str | None = None) -> AlertOut:
    return _finish(ctx, alert_id, actor, "new", "claimed", note)


def act(ctx: AppContext, alert_id: str, actor: str, note: str | None = None) -> AlertOut:
    return _finish(ctx, alert_id, actor, "claimed", "acted", (note or "").strip() or None)


def not_act(ctx: AppContext, alert_id: str, actor: str, note: str | None = None) -> AlertOut:
    reason = (note or "").strip()
    if not reason:
        raise AlertError("note_needed", "Say why no action was taken")
    return _finish(ctx, alert_id, actor, "claimed", "not_acted", reason)


def mark_seen(ctx: AppContext, actor: str) -> int:
    """A vet opened the alerts page: stamp every alert that had not been seen. Returns how many."""
    _who(ctx, actor, "vet")
    with ctx.db.transaction() as conn:
        cur = conn.execute(
            "UPDATE alerts SET first_seen_by_vet_at = ? WHERE first_seen_by_vet_at IS NULL",
            (clock.iso(clock.now()),),
        )
        if cur.rowcount:
            audit.record(conn, actor, "alert.seen", "alert", None, {"count": cur.rowcount})
        return cur.rowcount


# --------------------------------------------------------------------- callbacks
def _callback_out(r: sqlite3.Row, *, for_clinic: bool) -> CallbackRequestOut:
    return CallbackRequestOut(
        id=r["id"],
        course_id=r["course_id"] if for_clinic else None,
        register_no=r["register_no"] if for_clinic else None,
        ward_no=r["ward_no"],
        requested_at=r["requested_at"],
        status=r["status"],
    )


_CB_SELECT = (
    "SELECT cb.*, a.ward_no AS ward_no, c.register_no AS register_no, rep.clinic_id AS clinic_id "
    "FROM callback_requests cb JOIN alerts a ON a.id = cb.alert_id "
    "JOIN bite_reports rep ON rep.id = a.report_id LEFT JOIN courses c ON c.id = cb.course_id "
)


def request_callback(ctx: AppContext, alert_id: str, actor: str) -> CallbackRequestOut:
    """The vet on this alert asks the patient's clinic to phone the patient. The reply to the vet carries
    no course or register number."""
    _who(ctx, actor, "vet")
    with ctx.db.transaction() as conn:
        a = conn.execute(
            "SELECT a.*, r.course_id AS course_id FROM alerts a JOIN bite_reports r ON r.id = a.report_id "
            "WHERE a.id = ?",
            (alert_id,),
        ).fetchone()
        if a is None:
            raise AlertError("not_found", "There is no such alert")
        if a["status"] != "claimed" or a["claimed_by"] != actor:
            raise AlertError("not_claimed", "Say you are on this alert before asking for a call-back")
        if a["course_id"] is None:
            raise AlertError("no_course", "There is no patient record to call back")
        open_one = conn.execute(
            "SELECT id FROM callback_requests WHERE alert_id = ? AND status = 'open'", (alert_id,)
        ).fetchone()
        cb_id = open_one["id"] if open_one else new_id("cb")
        if not open_one:
            conn.execute(
                "INSERT INTO callback_requests(id, alert_id, course_id, requested_by, requested_at, status) "
                "VALUES (?,?,?,?,?, 'open')",
                (cb_id, alert_id, a["course_id"], actor, clock.iso(clock.now())),
            )
            audit.record(conn, actor, "callback.request", "callback", cb_id, {"alert_id": alert_id})
        row = conn.execute(_CB_SELECT + "WHERE cb.id = ?", (cb_id,)).fetchone()
        cluster, ward = a["cluster_id"], a["ward_no"]
    if not open_one:
        _publish(ctx, alert_id, a["status"], cluster, ward, callback="open")
        _publish_callback(ctx, cb_id, alert_id, "open")
    return _callback_out(row, for_clinic=False)


def callbacks_for_clinic(ctx: AppContext, clinic_id: str, status: str = "open") -> list[CallbackRequestOut]:
    rows = ctx.db.query(
        _CB_SELECT + "WHERE rep.clinic_id = ? AND cb.status = ? ORDER BY cb.requested_at, cb.id",
        (clinic_id, status),
    )
    return [_callback_out(r, for_clinic=True) for r in rows]


def callback_done(
    ctx: AppContext, cb_id: str, actor: str, note: str | None = None, outcome: str = "done"
) -> CallbackRequestOut:
    """The clinic says it phoned the patient (done), could not reach them (closed_no_contact) or will not
    (declined)."""
    if outcome not in CALLBACK_OUTCOMES:
        raise AlertError("bad_status", "A call-back ends as done, declined or closed with no contact")
    staff = _who(ctx, actor, "clinic")
    with ctx.db.transaction() as conn:
        row = conn.execute(_CB_SELECT + "WHERE cb.id = ?", (cb_id,)).fetchone()
        if row is None:
            raise AlertError("not_found", "There is no such call-back")
        if row["clinic_id"] != staff["clinic_id"]:
            raise AlertError("forbidden", "This call-back belongs to another clinic")
        if row["status"] != "open":
            raise AlertError("already_closed", "This call-back is already closed")
        conn.execute(
            "UPDATE callback_requests SET status = ?, done_at = ?, note = ? WHERE id = ?",
            (outcome, clock.iso(clock.now()), (note or "").strip() or None, cb_id),
        )
        audit.record(conn, actor, f"callback.{outcome}", "callback", cb_id, None)
        done = conn.execute(_CB_SELECT + "WHERE cb.id = ?", (cb_id,)).fetchone()
        alert = conn.execute("SELECT * FROM alerts WHERE id = ?", (row["alert_id"],)).fetchone()
    _publish(ctx, alert["id"], alert["status"], alert["cluster_id"], alert["ward_no"], callback=outcome)
    _publish_callback(ctx, cb_id, alert["id"], outcome)
    return _callback_out(done, for_clinic=True)


# MODULES.md calls this alerts.list(ctx, status). Defined last so no code above sees the shadowed builtin.
list = list_alerts  # noqa: A001
