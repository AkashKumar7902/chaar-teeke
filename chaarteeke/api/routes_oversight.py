"""Health oversight: the summary and the two CSV exports.

The summary and the weekly CSV hold aggregates only. The register CSV is one row per patient course (no
name, phone number or date), so only the reviewer role reads it. Both CSVs are stamped DEMO DATA in demo
mode by the stats module; the file name says so too. Every download is written to the audit log.
"""

# ruff: noqa: B008 (FastAPI reads `Depends(...)` in argument defaults)

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from .. import audit, clock
from ..models import OversightSummary
from ..stats import coverage as stats_coverage
from ..stats import exports
from .auth import Principal
from .deps import get_ctx, reader

router = APIRouter()


def _csv(request: Request, who: Principal, kind: str, text: str) -> Response:
    ctx = get_ctx(request)
    stamp = "DEMO-" if ctx.settings.is_demo else ""
    day = clock.local_today(ctx.settings.local_tz).isoformat()
    with ctx.db.transaction() as conn:
        audit.record(conn, who.actor, "export.download", "export", kind)
    return Response(
        text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="chaar-teeke-{stamp}{kind}-{day}.csv"'},
    )


@router.get("/api/oversight/summary")
def summary(request: Request, who: Principal = Depends(reader("oversight"))) -> OversightSummary:
    return stats_coverage.oversight_summary(get_ctx(request))


@router.get("/api/exports/weekly.csv")
def weekly_csv(request: Request, who: Principal = Depends(reader("oversight"))) -> Response:
    return _csv(request, who, "weekly", exports.weekly_csv(get_ctx(request)))


@router.get("/api/exports/register.csv")
def register_csv(request: Request, who: Principal = Depends(reader("register"))) -> Response:
    return _csv(request, who, "register", exports.register_csv(get_ctx(request)))
