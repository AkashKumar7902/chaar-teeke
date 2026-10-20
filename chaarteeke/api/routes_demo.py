"""The demo clock and the reset. Demo mode only.

Any signed-in session may read the state and move the clock forward, a visitor included: that is the visitor's
control. It is shared by everyone in the room, so it has limits and an owner:

- every jump is audited under the person who pressed it (`visitor:<n>` or `staff:<user>`);
- a visitor may jump policy visitor.clock_jumps_per_session times, and all visitors together
  policy visitor.clock_jumps_per_hour times an hour (real time: the demo clock itself jumps hours);
- the administrator can lock the clock, which turns visitor jumps off (staff keep theirs);
- signed-in staff can put the clock back to now without rebuilding the data (the presenter's way back).

Rebuilding the demo data is the administrator's alone.
"""

# ruff: noqa: B008 (FastAPI reads `Depends(...)` in argument defaults)

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from .. import audit
from ..context import AppContext
from ..demo import controls
from ..models import DemoState
from . import auth
from .auth import AuthError, Principal
from .deps import demo_controller, get_ctx, signed_in, writer
from .routes_auth import set_session_cookie

router = APIRouter()
KV_LOCKED = "demo_clock_locked"
JUMP_ACTION = "demo.jump"
_JUMP_LOCK = threading.Lock()  # one jump at a time: checked, done and counted together


class JumpIn(BaseModel):
    to: Literal["next_call", "evening_before_next_dose", "morning_after"]


class LockIn(BaseModel):
    locked: bool


class DemoStateOut(DemoState):
    """The state as the asker sees it: `jump_targets` is empty when they may not jump now."""

    clock_locked: bool
    jumps_left: int | None  # a visitor's jumps still allowed; null for staff, who have no cap


def _demo_only(ctx: AppContext) -> None:
    if not ctx.settings.is_demo:
        raise AuthError("not_available", "The demo clock only exists in demo mode", 404)


def clock_locked(ctx: AppContext) -> bool:
    return ctx.db.one("SELECT 1 FROM kv WHERE key = ?", (KV_LOCKED,)) is not None


def _jumps_used(ctx: AppContext, who: Principal) -> int:
    return ctx.db.one(
        "SELECT COUNT(*) AS n FROM audit WHERE action = ? AND actor = ?", (JUMP_ACTION, who.actor)
    )["n"]


def jumps_left(ctx: AppContext, who: Principal) -> int | None:
    if who.role != "visitor":
        return None
    return max(ctx.policy["visitor"]["clock_jumps_per_session"] - _jumps_used(ctx, who), 0)


def _hour_limiter(ctx: AppContext) -> auth.AttemptLimiter:
    return ctx.extras.setdefault("clock_jump_limiter", auth.AttemptLimiter())


def _state_for(ctx: AppContext, who: Principal) -> DemoStateOut:
    base = controls.state(ctx)
    locked = clock_locked(ctx)
    left = jumps_left(ctx, who)
    may_jump = who.role != "visitor" or (not locked and (left or 0) > 0)
    fields = base.model_dump()
    if not may_jump:
        fields["jump_targets"] = []
    return DemoStateOut(**fields, clock_locked=locked, jumps_left=left)


def _check_visitor_may_jump(ctx: AppContext, who: Principal) -> None:
    """Raises when a visitor may not move the clock now. Staff are never refused here."""
    if who.role != "visitor":
        return
    if clock_locked(ctx):
        raise AuthError("clock_locked", "The presenter has locked the clock", 403)
    if not jumps_left(ctx, who):
        raise AuthError(
            "jump_limit", "You have moved the clock as many times as one visitor may", 429, retry_after=3600
        )
    wait = _hour_limiter(ctx).wait(
        "jumps", auth.session_now(), timedelta(hours=1), ctx.policy["visitor"]["clock_jumps_per_hour"]
    )
    if wait is not None:
        raise AuthError(
            "jump_limit",
            "The clock has been moved as many times as visitors may in this hour. Ask the presenter.",
            429,
            retry_after=int(wait.total_seconds()) + 1,
        )


@contextmanager
def audited_as(actor: str) -> Iterator[None]:
    """controls.jump writes its audit row under controls.ACTOR (system:demo). For the length of one jump the
    row is written under the person who pressed the button; the caller holds _JUMP_LOCK. (controls.jump has no
    actor argument yet: when it gets one, call it with the actor and drop this.)"""
    was = controls.ACTOR
    controls.ACTOR = actor
    try:
        yield
    finally:
        controls.ACTOR = was


@router.get("/api/demo/state")
def state(request: Request, who: Principal = Depends(signed_in)) -> DemoStateOut:
    ctx = get_ctx(request)
    _demo_only(ctx)
    return _state_for(ctx, who)


@router.post("/api/demo/jump")
def jump(body: JumpIn, request: Request, who: Principal = Depends(demo_controller)) -> DemoStateOut:
    ctx = get_ctx(request)
    with _JUMP_LOCK:  # so two quick presses cannot both take the last place
        _check_visitor_may_jump(ctx, who)
        with audited_as(who.actor):
            controls.jump(ctx, body.to)
        if who.role == "visitor":
            _hour_limiter(ctx).add("jumps", auth.session_now(), timedelta(hours=1))
    return _state_for(ctx, who)


@router.post("/api/demo/clock-lock")
def lock_clock(body: LockIn, request: Request, who: Principal = Depends(writer("admin"))) -> DemoStateOut:
    """Turn visitor jumps off (or back on). Staff can still jump."""
    ctx = get_ctx(request)
    _demo_only(ctx)
    with ctx.db.transaction() as conn:
        if body.locked:
            conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, '1')", (KV_LOCKED,))
        else:
            conn.execute("DELETE FROM kv WHERE key = ?", (KV_LOCKED,))
        audit.record(conn, who.actor, "demo.clock_lock", "demo", None, {"locked": body.locked})
    return _state_for(ctx, who)


@router.post("/api/demo/clock-now")
def clock_to_now(request: Request, who: Principal = Depends(demo_controller)) -> DemoStateOut:
    """Back to now: put the demo clock on real time, leaving the data as it is. Signed-in staff only."""
    ctx = get_ctx(request)
    if not who.can_write:
        raise AuthError("read_only", "Your role can't do this", 403)
    controls.reset_clock(ctx)
    with ctx.db.transaction() as conn:
        audit.record(conn, who.actor, "demo.clock_now", "demo", None, None)
    return _state_for(ctx, who)


@router.post("/api/demo/reset")
def reset(request: Request, response: Response, who: Principal = Depends(writer("admin"))) -> dict:
    """Back to real time and a fresh demo history. Rebuilding the staff rows ends every staff login, so the
    administrator who pressed the button is signed in again here and gets a new CSRF token to keep using."""
    ctx = get_ctx(request)
    _demo_only(ctx)
    report = controls.reset_demo(ctx)
    row = ctx.db.one("SELECT id, role FROM staff WHERE username = ?", (who.actor.removeprefix("staff:"),))
    csrf = None
    if row is not None:
        token, csrf = auth.create_session(ctx, staff_id=row["id"], role=row["role"], visitor_label=None)
        set_session_cookie(request, response, ctx, token)
    return {"ok": True, "csrf": csrf, "seed": report.seed, "counts": report.counts, "digest": report.digest}
