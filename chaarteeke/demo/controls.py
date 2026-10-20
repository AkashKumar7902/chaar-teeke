"""The demo clock: show where it stands and jump it forward to the next thing worth seeing.

Only the injected OffsetClock can be moved, and only in demo mode. A jump never goes backwards and never
touches a row: the scheduler notices the new time on its next tick and places whatever is then due.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import get_args

from .. import audit, clock
from ..context import AppContext
from ..models import CallKind, DemoState
from ..stats.errors import StatsError

JUMP_TARGETS = ("next_call", "evening_before_next_dose", "morning_after")
ACTOR = "system:demo"
KV_OFFSET = "demo_clock_offset_s"
_KINDS = get_args(CallKind)


class DemoError(StatsError):
    """code: not_demo, clock_fixed, bad_target, nothing_scheduled, no_simulator_course, no_dose_ahead."""


def _scheduler():
    from .. import scheduler  # imported when used: the scheduler module needs the database layer up

    return scheduler


def _require_demo(ctx: AppContext) -> None:
    if not ctx.settings.is_demo:
        raise DemoError("not_demo", "The demo clock only exists in demo mode")


def _offset_clock() -> clock.OffsetClock | None:
    c = clock.current()
    return c if isinstance(c, clock.OffsetClock) else None


def state(ctx: AppContext) -> DemoState:
    _require_demo(ctx)
    now = clock.now()
    movable = _offset_clock()
    nxt_at = _scheduler().next_call_at(ctx)
    kind = None
    if nxt_at is not None:
        planned = _scheduler().next_call(ctx)
        if planned is not None and planned.at == nxt_at and planned.kind in _KINDS:
            kind = planned.kind
    return DemoState(
        now=clock.iso(now),
        local_now=now.astimezone(clock.local_tz(ctx.settings.local_tz)).isoformat(timespec="seconds"),
        offset_seconds=movable.offset.total_seconds() if movable else 0.0,
        next_call_at=clock.iso(nxt_at) if nxt_at else None,
        next_call_kind=kind,
        jump_targets=list(JUMP_TARGETS) if movable else [],
    )


def _next_dose_day(ctx: AppContext, after: datetime, delta_days: int, time_key: str) -> datetime:
    """The first moment at `time_key` local time, `delta_days` from a dose of the latest simulator course,
    that is still ahead of the clock. Dose 1 has no reminder before it, so it is skipped."""
    course = ctx.db.one(
        "SELECT id FROM courses WHERE source = 'simulator' AND status = 'active' ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    if course is None:
        raise DemoError("no_simulator_course", "Give a missed call from the demo phone first")
    hhmm = ctx.policy["calls"][time_key]
    for d in ctx.db.query(
        "SELECT due_date FROM doses WHERE course_id = ? AND dose_no > 1 AND status IN ('due','late_minor','late_review') "
        "ORDER BY dose_no",
        (course["id"],),
    ):
        moment = clock.at_local(
            date.fromisoformat(d["due_date"]) + timedelta(days=delta_days), hhmm, ctx.settings.local_tz
        )
        if moment > after:
            return moment
    raise DemoError("no_dose_ahead", "The demo patient has no dose still to come")


def jump(ctx: AppContext, to: str) -> DemoState:
    _require_demo(ctx)
    if to not in JUMP_TARGETS:
        raise DemoError("bad_target", "That is not a place the demo clock can jump to")
    movable = _offset_clock()
    if movable is None:
        raise DemoError("clock_fixed", "The demo clock is not installed, so it cannot move")
    now = clock.now()
    if to == "next_call":
        target = _scheduler().next_call_at(ctx)
        if target is None:
            raise DemoError("nothing_scheduled", "No call is waiting to be placed")
    elif to == "evening_before_next_dose":
        target = _next_dose_day(ctx, now, -1, "reminder_local_time")
    else:
        target = _next_dose_day(ctx, now, 1, "morning_after_local_time")
    if target > now:
        movable.jump_to(target)
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
            (KV_OFFSET, str(movable.offset.total_seconds())),
        )
        audit.record(conn, ACTOR, "demo.jump", "demo", None, {"to": to})
    after = clock.now()
    ctx.bus.publish(
        "clock",
        {
            "now": clock.iso(after),
            "local_now": after.astimezone(clock.local_tz(ctx.settings.local_tz)).isoformat(
                timespec="seconds"
            ),
        },
    )
    return state(ctx)


def reset_clock(ctx: AppContext) -> None:
    """Put the demo clock back on real time. Used before the demo data is rebuilt."""
    _require_demo(ctx)
    movable = _offset_clock()
    if movable is None:
        raise DemoError("clock_fixed", "The demo clock is not installed, so it cannot move")
    movable.reset()
    with ctx.db.transaction() as conn:
        conn.execute("DELETE FROM kv WHERE key = ?", (KV_OFFSET,))
        # a purge stamped while the clock was ahead would otherwise stop the deletion jobs until real time caught up
        conn.execute("DELETE FROM kv WHERE key = 'last_purge_run'")
    after = clock.now()
    ctx.bus.publish(
        "clock",
        {
            "now": clock.iso(after),
            "local_now": after.astimezone(clock.local_tz(ctx.settings.local_tz)).isoformat(
                timespec="seconds"
            ),
        },
    )


def reset_demo(ctx: AppContext, seed: int | None = None):
    """Back to real time and a freshly built demo history (the Reset button)."""
    from .seed import DEFAULT_SEED
    from .seed import seed as build

    _require_demo(ctx)
    runner = ctx.runner
    if runner is not None:
        runner.cancel_calls()  # a call still running would write to the courses about to be deleted
    try:
        if _offset_clock() is not None:
            reset_clock(ctx)
        return build(ctx, DEFAULT_SEED if seed is None else seed, reset=True)
    finally:
        if runner is not None:
            runner.resume()
