"""Keep the phone line connected: try again with a growing wait while it is down, and give back the calls the
outage cost.

The runner marks the link down when the PBX's event socket closes (or when the port never started). Nothing
brought it back until the administrator pressed Reconnect. This loop does it by itself, waiting
policy telephony.reconnect_min_s after the first failure and doubling up to reconnect_max_s, and tells the
page (a `system` event, ok true) when the line is back.

A call that failed only because the line was down never reached the patient, so it must not count as one of
their attempts. When the line is back those calls are put back to `scheduled` for now; the scheduler's own
check still cancels any whose day has passed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from .. import audit, clock
from ..context import AppContext
from ..doses import emit
from ..enrol import SCHEDULER_ACTOR, call_event

log = logging.getLogger("chaarteeke.telephony")

# What the runner writes in `calls.error` when a call fails because the link is down (ivr/runner.py).
OUTAGE_ERRORS = ("The telephone link is down", "The phone system could not place the call")
DOWN_SINCE = "telephony_down_since"
_RETRY_SUFFIX = re.compile(r":a\d+$")  # the scheduler's suffix on a retry's job key


def link_is_up(ctx: AppContext) -> bool:
    """True while the port started, the runner reads its events and has not seen the link drop."""
    runner = ctx.runner
    if not ctx.extras.get("telephony_up") or runner is None or getattr(runner, "_down", None) is not None:
        return False
    pump = getattr(runner, "_pump", None)
    return pump is not None and not pump.done()


def next_wait(ctx: AppContext, previous: float | None) -> float:
    """5 s after the first failure, doubling, never above the policy maximum."""
    pol = ctx.policy["telephony"]
    if previous is None:
        return float(pol["reconnect_min_s"])
    return min(previous * 2, float(pol["reconnect_max_s"]))


async def run(ctx: AppContext, connect: Callable[[AppContext], Awaitable[None]]) -> None:
    """Runs for the life of the server. `connect` is app.connect_telephony: it does nothing while the link is
    up and builds the port and the runner afresh when it is not."""
    wait: float | None = None
    while True:
        if link_is_up(ctx):
            if wait is not None:
                wait = None
                give_back_outage_calls(ctx)
            await asyncio.sleep(ctx.policy["telephony"]["reconnect_min_s"])
            continue
        ctx.extras.setdefault(DOWN_SINCE, clock.now())
        wait = next_wait(ctx, wait)
        await asyncio.sleep(wait)
        try:
            await connect(ctx)
        except Exception:  # a fault in the retry itself must not end the loop
            log.exception("reconnecting the telephone link failed")


def give_back_outage_calls(ctx: AppContext) -> int:
    """Put back to `scheduled` the calls that failed because the line was down. Of one call's tries that
    all failed that way (the scheduler retried it during the outage) only the first comes back, and the
    later ones, failed or still waiting, are deleted: they never reached the patient, and one call must not
    become several. Returns how many calls were given back."""
    since: datetime | None = ctx.extras.pop(DOWN_SINCE, None)
    if since is None:
        return 0
    slack = timedelta(seconds=2 * ctx.policy["telephony"]["reconnect_min_s"])
    marks = " OR ".join("error LIKE ?" for _ in OUTAGE_ERRORS)
    events: list = []
    with ctx.db.transaction() as conn:
        failed = conn.execute(
            f"SELECT * FROM calls WHERE status = 'failed' AND ended_at >= ? AND ({marks}) ORDER BY attempt, id",
            (clock.iso(since - slack), *[f"{e}%" for e in OUTAGE_ERRORS]),
        ).fetchall()
        given_back: set[str] = set()
        for c in failed:
            base = _RETRY_SUFFIX.sub("", c["job_key"])
            if base in given_back:
                conn.execute("DELETE FROM calls WHERE id = ?", (c["id"],))
                continue
            given_back.add(base)
            for later in conn.execute(
                "SELECT id, job_key FROM calls WHERE course_id IS ? AND kind = ? AND status = 'scheduled' AND attempt > ?",
                (c["course_id"], c["kind"], c["attempt"]),
            ).fetchall():
                if _RETRY_SUFFIX.sub("", later["job_key"]) == base:
                    conn.execute("DELETE FROM calls WHERE id = ?", (later["id"],))
            conn.execute(
                "UPDATE calls SET status='scheduled', scheduled_for=?, started_at=NULL, ended_at=NULL, error=NULL"
                " WHERE id=?",
                (clock.iso(clock.now()), c["id"]),
            )
            audit.record(
                conn,
                SCHEDULER_ACTOR,
                "call.replanned",
                "call",
                c["id"],
                {"why": "the phone line was down; this try is not counted"},
            )
            events.append(call_event(c["id"], c["course_id"], c["kind"], "scheduled", c["transport"]))
    emit(ctx, events)
    return len(given_back)
