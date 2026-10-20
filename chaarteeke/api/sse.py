"""The SSE stream. Payloads carry ids and small facts only, never a phone number.

Each person gets only the event types their role's pages use, and a clinic gets only its own patients'
events. The page reloads what it shows from the normal GET routes after an event or a reconnect, so the
stream is a nudge, never the record.
"""

from __future__ import annotations

import asyncio
import json
import time
import weakref
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..context import AppContext
from ..events import Event
from .auth import COOKIE, AuthError, Principal, principal_for
from .deps import get_ctx, signed_in

router = APIRouter()
RETRY_MS = 5000  # the `retry:` the stream sends: how long a browser waits before it reconnects
PING_S = 15  # an SSE comment every 15 s keeps proxies from closing a quiet stream
POLL_S = 1.0  # how often a quiet stream looks for a closed browser

# Which event types each view's pages use (MODULES.md "HTTP and SSE").
TYPES_BY_VIEW: dict[str, frozenset[str]] = {
    "clinic": frozenset({"call", "missed_call", "dose", "course", "callback", "clock", "system"}),
    # The team gets call and missed_call too, but only the simulated ones (the visitor's own phone), and
    # without the course or the number (see Filter and `shaped`).
    "abc": frozenset({"report", "alert", "callback", "call", "missed_call", "clock", "system"}),
    "vet": frozenset({"report", "alert", "callback", "call", "missed_call", "clock", "system"}),
    # Oversight reads aggregates and has no alert list (auth.READ), so it gets no alert events either.
    "oversight": frozenset({"report", "clock", "system"}),
    "reviewer": frozenset({"review", "clock", "system"}),
    "admin": frozenset({"clock", "system"}),
}
TEAM_VIEWS = frozenset({"abc", "vet"})
SIMULATED_ONLY = frozenset({"call", "missed_call"})  # real ones are patient events: the team never gets them
# What a team stream keeps of a simulated call or missed call: ids and the state, never a course or a number.
TEAM_KEEPS = {
    "call": ("call_id", "kind", "status", "simulated"),
    "missed_call": ("id", "status"),
}
PATIENT_EVENTS = frozenset({"call", "dose", "course"})  # these name a course, so a clinic checks whose it is
# A live report would let a watcher count single bites by area and answer, below the "<5" rule. A real
# report is therefore announced without its answer; the page reloads the suppressed counts from the GET
# routes. A simulated one (the visitor's own phone) carries its facts so the demo shows them at once.
REPORT_KEEPS_WHEN_REAL = ("report_id", "cluster_id", "simulated")


class Filter:
    """Decides, per event, whether one stream may carry it."""

    def __init__(self, ctx: AppContext, who: Principal) -> None:
        self.ctx = ctx
        self.view = who.view
        self.types = TYPES_BY_VIEW[who.view]
        # Clinic staff get their own clinic's events only. A visitor viewing as a clinic sees demo data
        # from every demo clinic (the visitor desk where their own phone's patient appears).
        self.scoped = who.view == "clinic" and who.role != "visitor"
        self.clinic = who.clinic_id
        self._course_clinic: dict[str, str | None] = {}

    def _clinic_of(self, course_id: str) -> str | None:
        if course_id not in self._course_clinic:
            row = self.ctx.db.one("SELECT clinic_id FROM courses WHERE id = ?", (course_id,))
            self._course_clinic[course_id] = row["clinic_id"] if row else None
        return self._course_clinic[course_id]

    def _simulated(self, ev: Event) -> bool:
        if ev.type == "call":
            return ev.data.get("simulated") is True
        row = self.ctx.db.one("SELECT transport FROM missed_calls WHERE id = ?", (ev.data.get("id"),))
        return row is not None and row["transport"] == "sim"

    def _callback_clinic(self, callback_id: str | None) -> str | None:
        row = self.ctx.db.one(
            "SELECT rep.clinic_id AS clinic_id FROM callback_requests cb JOIN alerts a ON a.id = cb.alert_id"
            " JOIN bite_reports rep ON rep.id = a.report_id WHERE cb.id = ?",
            (callback_id,),
        )
        return row["clinic_id"] if row else None

    def allows(self, ev: Event) -> bool:
        if ev.type not in self.types:
            return False
        if self.view in TEAM_VIEWS and ev.type in SIMULATED_ONLY:
            return self._simulated(ev)
        if not self.scoped:
            return True
        if ev.type == "callback":
            return (
                self.clinic is not None and self._callback_clinic(ev.data.get("callback_id")) == self.clinic
            )
        if ev.type in PATIENT_EVENTS:
            course_id = ev.data.get("course_id")
            return (
                self.clinic is not None
                and course_id is not None
                and self._clinic_of(course_id) == self.clinic
            )
        if ev.type == "missed_call":
            row = self.ctx.db.one("SELECT clinic_hint FROM missed_calls WHERE id = ?", (ev.data.get("id"),))
            return row is not None and row["clinic_hint"] in (None, self.clinic)
        return True


def shaped(ev: Event, view: str | None = None) -> Event:
    """The event as the stream of `view` may carry it."""
    if view in TEAM_VIEWS and ev.type in TEAM_KEEPS:
        data = {k: v for k, v in ev.data.items() if k in TEAM_KEEPS[ev.type]}
        return Event(type=ev.type, data=data, id=ev.id, at=ev.at)
    if ev.type == "report" and not ev.data.get("simulated"):
        data = {k: v for k, v in ev.data.items() if k in REPORT_KEEPS_WHEN_REAL}
        return Event(type=ev.type, data=data, id=ev.id, at=ev.at)
    return ev


def frame(ev: Event) -> bytes:
    body = json.dumps({"id": ev.id, "type": ev.type, "at": ev.at, "data": ev.data}, ensure_ascii=False)
    return f"event: {ev.type}\nid: {ev.id}\ndata: {body}\n\n".encode()


async def stream_events(
    ctx: AppContext,
    who: Principal,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    still_signed_in: Callable[[], bool] | None = None,
    ping_s: float | None = None,
    poll_s: float | None = None,
) -> AsyncIterator[bytes]:
    ping, poll = ping_s or PING_S, poll_s or POLL_S
    allowed = Filter(ctx, who)
    q = ctx.bus.subscribe()
    try:
        yield f"retry: {RETRY_MS}\n\n".encode()
        last_sent = last_checked = time.monotonic()
        while True:
            if await is_disconnected():
                break
            if still_signed_in is not None and time.monotonic() - last_checked >= ping:
                # A login that ended (signed out, switched off, expired) must not keep receiving events.
                last_checked = time.monotonic()
                if not still_signed_in():
                    break
            try:
                ev = await asyncio.wait_for(q.get(), timeout=min(poll, ping))
            except TimeoutError:
                ev = None
            if ev is not None and allowed.allows(ev):
                yield frame(shaped(ev, who.view))
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= ping:
                yield b": ping\n\n"
                last_sent = time.monotonic()
    finally:
        ctx.bus.unsubscribe(q)


def _open_streams(ctx: AppContext) -> Counter[str]:
    return ctx.extras.setdefault("open_event_streams", Counter())


class Place:
    """One of a person's stream places. Given back once, whichever comes first: the stream ending, or the
    stream being thrown away without ever having started (a browser that left at once)."""

    def __init__(self, ctx: AppContext, actor: str) -> None:
        self.open_now, self.actor, self.held = _open_streams(ctx), actor, True
        self.open_now[actor] += 1

    def release(self) -> None:
        if self.held:
            self.held = False
            self.open_now[self.actor] -= 1
            if self.open_now[self.actor] <= 0:
                del self.open_now[self.actor]


async def counted(place: Place, stream: AsyncGenerator[bytes]) -> AsyncIterator[bytes]:
    try:
        async for chunk in stream:
            yield chunk
    finally:
        place.release()
        await stream.aclose()  # unsubscribes from the bus now, not when the generator is collected


@router.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    who = signed_in(request)
    ctx = get_ctx(request)
    token = request.cookies.get(COOKIE)
    open_now = _open_streams(ctx)
    if open_now[who.actor] >= ctx.policy["limits"]["event_streams_per_principal"]:
        raise AuthError(
            "too_many_streams",
            "This login already has the most live pages open. Close one and try again.",
            429,
            retry_after=int(RETRY_MS / 1000),
        )
    place = Place(ctx, who.actor)
    stream = stream_events(
        ctx,
        who,
        request.is_disconnected,
        still_signed_in=lambda: principal_for(ctx, token) is not None,
    )
    body = counted(place, stream)
    weakref.finalize(body, place.release)
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
