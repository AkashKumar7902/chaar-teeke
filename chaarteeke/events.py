"""In-process event bus feeding the SSE stream. Publishers never block; slow subscribers drop."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any

from . import clock

EVENT_TYPES = (
    "call",
    "missed_call",
    "report",
    "alert",
    "dose",
    "review",
    "clock",
    "system",
    "course",
    "callback",
)


@dataclass
class Event:
    type: str
    data: dict[str, Any]
    id: int = 0
    at: str = field(default_factory=lambda: clock.iso(clock.now()))


class EventBus:
    def __init__(self, maxsize: int = 256) -> None:
        self._subs: set[asyncio.Queue[Event]] = set()
        self._seq = itertools.count(1)
        self._maxsize = maxsize
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subs.discard(q)

    def publish(self, type: str, data: dict[str, Any]) -> Event:
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type}")
        ev = Event(type=type, data=data, id=next(self._seq))
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self._fanout, ev)
        else:
            self._fanout(ev)
        return ev

    def _fanout(self, ev: Event) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # the SSE client re-syncs on reconnect via the page's own GETs
