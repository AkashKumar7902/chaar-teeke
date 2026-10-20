"""The place reviewer: the next recording to place, its audio, the decision, and the names the system has
learned. Recordings are heard by the reviewer only (not by the administrator), and every listen is logged.
"""

# ruff: noqa: B008 (FastAPI reads `Depends(...)` in argument defaults)

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from .. import audit, clock
from ..models import DecideIn, ReviewItem
from ..places import review
from ..places.normalise import alias_text
from .auth import AuthError, Principal
from .deps import get_ctx, reader, writer

router = APIRouter()
MAX_SKIP = (
    100_000  # far past any real queue; a larger number would overflow SQLite's OFFSET and fail as a 500
)


class AliasIn(BaseModel):
    text: str = Field(min_length=1, max_length=80)
    ward_no: int


@router.get("/api/review/next")
def next_item(
    request: Request, skip: int = Query(0, ge=0, le=MAX_SKIP), who: Principal = Depends(reader("review"))
) -> ReviewItem | None:
    """The oldest recording waiting for a decision (skip=0), or the k-th oldest (skip=k) for a reviewer who
    passes one over for now; null when fewer than k + 1 wait. `remaining` counts the whole queue."""
    return review.next_item(get_ctx(request), skip)


@router.get("/api/review/{report_id}/audio")
def audio(report_id: str, request: Request, who: Principal = Depends(reader("review"))) -> Response:
    ctx = get_ctx(request)
    try:
        data = review.audio(ctx, report_id)
    except review.ReviewError as e:
        # "Deleted" is only true of a clip that was kept and then removed. An item that never had audio
        # (the demo's) must not claim it was.
        row = ctx.db.one(
            "SELECT audio_path, clip_deleted_at FROM place_captures WHERE report_id = ?", (report_id,)
        )
        if e.code == "no_audio" and row is not None and not row["audio_path"] and not row["clip_deleted_at"]:
            raise AuthError("no_audio", "This item has no recording", 410) from e
        raise
    with ctx.db.transaction() as conn:
        audit.record(conn, who.actor, "clip.listen", "bite_report", report_id)
    return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.post("/api/review/{report_id}/decide")
def decide(
    report_id: str, body: DecideIn, request: Request, who: Principal = Depends(writer("review"))
) -> dict[str, int | bool]:
    """Place the recording in a ward, outside the city, or as one that cannot be told: exactly one of those
    (review.decide checks). Returns how many recordings still wait."""
    return {"ok": True, "remaining": review.decide(get_ctx(request), report_id, body, who.actor)}


def _alias_rows(ctx) -> list[dict]:
    names = {w.ward_no: w.name for w in ctx.gazetteer.wards()}
    return [
        {
            "id": r["id"],
            "text": r["text_norm"],
            "ward_no": r["ward_no"],
            "ward_name": names.get(r["ward_no"]),
            "added_by": r["added_by"],
            "added_at": r["added_at"],
        }
        for r in ctx.db.query("SELECT * FROM aliases ORDER BY id DESC")
    ]


@router.get("/api/aliases")
def aliases(request: Request, who: Principal = Depends(reader("review"))) -> list[dict]:
    return _alias_rows(get_ctx(request))


@router.post("/api/aliases")
def alias_add(body: AliasIn, request: Request, who: Principal = Depends(writer("review"))) -> list[dict]:
    """Teach the system a name for a ward. It is stored folded, the same way the matcher folds a transcript."""
    ctx = get_ctx(request)
    if not ctx.gazetteer.has_ward(body.ward_no):
        raise AuthError("unknown_ward", "That is not a ward number.", 404)
    text = alias_text(body.text)
    if not text:
        raise AuthError("bad_request", "That name has no place word in it.", 400)
    with ctx.db.transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO aliases(text_norm, ward_no, added_by, added_at) VALUES (?,?,?,?)",
            (text, body.ward_no, who.actor, clock.iso(clock.now())),
        )
        if cur.rowcount:
            audit.record(conn, who.actor, "alias.add", "alias", str(cur.lastrowid), {"ward_no": body.ward_no})
    return _alias_rows(ctx)
