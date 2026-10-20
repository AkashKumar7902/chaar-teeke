"""The sterilisation team and the vets: coverage by area, area detail, ward counts, sick-dog alerts, the
wards the team has worked and its own sight counts.

Everything here is an aggregate or an alert (ward, day, place type, answer): no patient, no phone number, no
street-level point. Only a vet claims, closes or asks for a call-back.
"""

# ruff: noqa: B008 (FastAPI reads `Depends(...)` in argument defaults)

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .. import alerts, audit, clock
from ..models import AlertOut, CallbackRequestOut, Coverage, WorkedAffected
from ..stats import coverage as stats_coverage
from ..stats.clusters import ward_table
from ..stats.intervals import hide_small
from .auth import AuthError, Principal
from .deps import get_ctx, reader, writer

router = APIRouter()
MAX_WARDS_PER_ENTRY = 100  # the city has fewer than this many wards; a bound on one request, not a policy


class NoteIn(BaseModel):
    note: str | None = Field(default=None, max_length=300)


class WorkedIn(BaseModel):
    ward_nos: list[int] = Field(min_length=1, max_length=MAX_WARDS_PER_ENTRY)
    worked_on: str = Field(max_length=10)  # YYYY-MM-DD local date
    note: str | None = Field(default=None, max_length=200)


class SightIn(BaseModel):
    ward_no: int
    counted_on: str = Field(max_length=10)
    notched: int = Field(ge=0, le=100_000)
    unnotched: int = Field(ge=0, le=100_000)
    unsure: int = Field(ge=0, le=100_000)


def _day(ctx, text: str) -> date:
    try:
        day = date.fromisoformat(text)
    except ValueError:
        day = None
    if day is None or day.isoformat() != text:
        raise AuthError("bad_date", "That is not a valid date (use YYYY-MM-DD).", 400)
    if day > clock.local_today(ctx.settings.local_tz):
        raise AuthError("date_in_future", "That day has not come yet.", 400)
    return day


def _since(ctx) -> str:
    """Field records are listed for the coverage window, the same weeks the statistics look at."""
    weeks = ctx.policy["stats"]["coverage_window_weeks"]
    return (clock.local_today(ctx.settings.local_tz) - timedelta(weeks=weeks)).isoformat()


def _wards(ctx, ward_nos: list[int]) -> dict:
    table = ward_table(ctx.settings.data_dir / "wards.geojson")
    unknown = sorted({w for w in ward_nos if w not in table})
    if unknown:
        raise AuthError("unknown_ward", f"These are not ward numbers: {', '.join(map(str, unknown))}", 404)
    return table


# --------------------------------------------------------------------- coverage
@router.get("/api/coverage")
def coverage(
    request: Request, window: str | None = None, who: Principal = Depends(reader("team"))
) -> Coverage:
    ctx = get_ctx(request)
    weeks = ctx.policy["stats"]["coverage_window_weeks"]
    if window is not None and window != f"{weeks}w":
        raise AuthError("bad_request", f"Only the {weeks}-week window is available.", 400)
    return stats_coverage.coverage(ctx)


@router.get("/api/clusters/{cluster_id}")
def cluster(cluster_id: str, request: Request, who: Principal = Depends(reader("team"))) -> dict:
    return stats_coverage.cluster_detail(get_ctx(request), cluster_id)


@router.get("/api/wards")
def wards(request: Request, who: Principal = Depends(reader("team"))) -> list[dict]:
    return [w.model_dump() for w in stats_coverage.ward_counts(get_ctx(request))]


# ----------------------------------------------------------------------- alerts
@router.get("/api/alerts")
def list_alerts(
    request: Request, status: str | None = None, who: Principal = Depends(reader("alerts"))
) -> list[AlertOut]:
    ctx = get_ctx(request)
    # Opening the list is how a vet shows they have looked. A link on another site must not be able to do
    # it for them (the cookie travels on a cross-site GET), so the browser's own fetch-site hint decides.
    if who.role == "vet" and request.headers.get("sec-fetch-site") in (None, "same-origin", "none"):
        alerts.mark_seen(ctx, who.actor)
    return alerts.list_alerts(ctx, status, who.actor)


@router.post("/api/alerts/{alert_id}/claim")
def claim(
    alert_id: str,
    request: Request,
    body: NoteIn | None = None,
    who: Principal = Depends(writer("alerts.claim")),
) -> AlertOut:
    return alerts.claim(get_ctx(request), alert_id, who.actor, body.note if body else None)


@router.post("/api/alerts/{alert_id}/acted")
def acted(
    alert_id: str,
    request: Request,
    body: NoteIn | None = None,
    who: Principal = Depends(writer("alerts.claim")),
) -> AlertOut:
    return alerts.act(get_ctx(request), alert_id, who.actor, body.note if body else None)


@router.post("/api/alerts/{alert_id}/not-acted")
def not_acted(
    alert_id: str,
    request: Request,
    body: NoteIn | None = None,
    who: Principal = Depends(writer("alerts.claim")),
) -> AlertOut:
    return alerts.not_act(get_ctx(request), alert_id, who.actor, body.note if body else None)


@router.post("/api/alerts/{alert_id}/callback")
def callback(
    alert_id: str, request: Request, who: Principal = Depends(writer("alerts.callback"))
) -> CallbackRequestOut:
    return alerts.request_callback(get_ctx(request), alert_id, who.actor)


# ---------------------------------------------------------------- field records
@router.get("/api/worked")
def worked_list(request: Request, who: Principal = Depends(reader("records"))) -> list[dict]:
    ctx = get_ctx(request)
    table = ward_table(ctx.settings.data_dir / "wards.geojson")
    return [
        {
            "id": r["id"],
            "ward_no": r["ward_no"],
            "ward_name": table[r["ward_no"]].name if r["ward_no"] in table else None,
            "worked_on": r["worked_on"],
            "note": r["note"],
        }
        for r in ctx.db.query(
            "SELECT * FROM worked_wards WHERE worked_on >= ? ORDER BY worked_on DESC, id DESC", (_since(ctx),)
        )
    ]


@router.get("/api/worked/affected")
def worked_affected(
    request: Request, ward_nos: str, who: Principal = Depends(reader("records"))
) -> WorkedAffected:
    """How many patients still have a dose ahead among those bitten in the wards named (comma-separated).

    The patients who would hear the worked-area line in a reminder: courses that are active, placed in one
    of the wards, with a dose still to come after today (its reminder is the evening before) and that have not
    heard the line already (it is said once). Demo-seeded courses are never called, so they are left out. An aggregate only, "<5" when small; no course, patient or number is named.
    """
    ctx = get_ctx(request)
    try:
        wanted = sorted({int(x) for x in ward_nos.split(",") if x.strip()})
    except ValueError:
        wanted = []
    if not wanted or len(wanted) > MAX_WARDS_PER_ENTRY:
        raise AuthError("bad_request", "Give ward numbers separated by commas, for example 1,2.", 400)
    _wards(ctx, wanted)
    marks = ",".join("?" * len(wanted))
    row = ctx.db.one(
        "SELECT COUNT(*) AS n FROM courses c JOIN bite_reports r ON r.course_id = c.id "
        f"WHERE c.status = 'active' AND c.source != 'demo' AND r.ward_no IN ({marks}) "
        "AND r.location_status IN ('matched', 'decided') "
        "AND NOT EXISTS (SELECT 1 FROM kv WHERE kv.key = 'team_line:' || c.id) AND EXISTS ("
        " SELECT 1 FROM doses d WHERE d.course_id = c.id AND d.status = 'due' AND d.due_date > ?)",
        (*wanted, clock.local_today(ctx.settings.local_tz).isoformat()),
    )
    return WorkedAffected(courses_with_doses_ahead=hide_small(row["n"], ctx.policy))


@router.post("/api/worked")
def worked_add(
    body: WorkedIn, request: Request, who: Principal = Depends(writer("team.records"))
) -> list[dict]:
    """Record wards the team worked on a day. A ward already recorded for that day is left as it is."""
    ctx = get_ctx(request)
    day = _day(ctx, body.worked_on)
    _wards(ctx, body.ward_nos)
    note = (body.note or "").strip() or None
    wards_in = sorted(set(body.ward_nos))
    with ctx.db.transaction() as conn:
        for ward_no in wards_in:
            have = conn.execute(
                "SELECT 1 FROM worked_wards WHERE ward_no = ? AND worked_on = ?", (ward_no, day.isoformat())
            ).fetchone()
            if not have:
                conn.execute(
                    "INSERT INTO worked_wards(ward_no, worked_on, recorded_by, note, is_demo) VALUES (?,?,?,?,?)",
                    (ward_no, day.isoformat(), who.actor, note, 1 if ctx.settings.is_demo else 0),
                )
        audit.record(
            conn, who.actor, "worked.record", "worked_ward", None, {"wards": wards_in, "on": day.isoformat()}
        )
    return worked_list(request, who)


@router.get("/api/sight-counts")
def sight_list(request: Request, who: Principal = Depends(reader("records"))) -> list[dict]:
    ctx = get_ctx(request)
    table = ward_table(ctx.settings.data_dir / "wards.geojson")
    return [
        {
            "id": r["id"],
            "ward_no": r["ward_no"],
            "ward_name": table[r["ward_no"]].name if r["ward_no"] in table else None,
            "counted_on": r["counted_on"],
            "notched": r["notched"],
            "unnotched": r["unnotched"],
            "unsure": r["unsure"],
        }
        for r in ctx.db.query(
            "SELECT * FROM sight_counts WHERE counted_on >= ? ORDER BY counted_on DESC, id DESC",
            (_since(ctx),),
        )
    ]


@router.post("/api/sight-counts")
def sight_add(
    body: SightIn, request: Request, who: Principal = Depends(writer("team.records"))
) -> list[dict]:
    ctx = get_ctx(request)
    day = _day(ctx, body.counted_on)
    _wards(ctx, [body.ward_no])
    with ctx.db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO sight_counts(ward_no, counted_on, notched, unnotched, unsure, recorded_by, is_demo)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                body.ward_no,
                day.isoformat(),
                body.notched,
                body.unnotched,
                body.unsure,
                who.actor,
                1 if ctx.settings.is_demo else 0,
            ),
        )
        audit.record(
            conn,
            who.actor,
            "sight_count.record",
            "sight_count",
            str(cur.lastrowid),
            {"ward_no": body.ward_no},
        )
    return sight_list(request, who)
