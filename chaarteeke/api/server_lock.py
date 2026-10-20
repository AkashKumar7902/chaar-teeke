"""Who else is running on this database: the scheduler's lock, read for the command line, the preflight,
the start of `serve` and the system status.

A server that is up refreshes the `scheduler` lock every few seconds. A lock whose last beat is recent
(policy scheduler.lock_stale_s) therefore means another process is serving this database. That process
may have its demo clock jumped, and it stamps the beat in that clock's time, so a process that is not the
server reads the stored clock offset (`kv`) and compares in the server's own time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import clock
from ..context import AppContext
from ..demo.controls import KV_OFFSET
from ..scheduler import LOCK_NAME

SERVER_RUNNING = (
    "A server is running on this database: use the admin Reset demo data button, or stop the server first."
)


@dataclass(frozen=True)
class LockInfo:
    holder: str
    age_s: float
    fresh: bool  # beat within policy scheduler.lock_stale_s of `now`, in either direction


def stored_now(ctx: AppContext) -> datetime:
    """Now on the clock the server of this database runs on: real time plus the demo clock's stored jump."""
    if not isinstance(clock.current(), clock.SystemClock):
        return (
            clock.now()
        )  # a clock was installed in this process (the server itself, or a test): it is the frame
    base = clock.now()
    if not ctx.settings.is_demo:
        return base
    row = ctx.db.one("SELECT value FROM kv WHERE key = ?", (KV_OFFSET,))
    return base + timedelta(seconds=float(row["value"])) if row else base


def read(ctx: AppContext, now: datetime | None = None) -> LockInfo | None:
    """The scheduler lock as it stands, or None when nobody holds it. `now` defaults to the installed clock;
    a process that is not the server passes `stored_now(ctx)`."""
    row = ctx.db.one("SELECT holder, heartbeat_at FROM locks WHERE name = ?", (LOCK_NAME,))
    if row is None:
        return None
    age = ((now or clock.now()) - clock.parse_iso(row["heartbeat_at"])).total_seconds()
    stale_s = ctx.policy["scheduler"]["lock_stale_s"]
    return LockInfo(row["holder"], age, abs(age) <= stale_s)


def foreign(ctx: AppContext, now: datetime | None = None) -> LockInfo | None:
    """A fresh lock held by somebody other than this process's own scheduler."""
    info = read(ctx, now)
    own = getattr(ctx.scheduler, "holder", None)
    return info if info is not None and info.fresh and info.holder != own else None


def refusal_to_serve(ctx: AppContext) -> str | None:
    """The line to print instead of serving, or None. Only the demo refuses: its browser phones ring only
    through the server that holds the scheduler, so a second server would take missed calls and never place
    the call back."""
    if not (ctx.settings.is_demo and ctx.settings.scheduler):
        return None
    info = foreign(ctx, stored_now(ctx))
    if info is None:
        return None
    wait = int(ctx.policy["scheduler"]["lock_stale_s"])
    return (
        f"Another server is already running on this database (its scheduler beat {max(int(abs(info.age_s)), 0)} s ago). "
        f"Stop it first, or wait {wait} s if it was stopped just now."
    )
