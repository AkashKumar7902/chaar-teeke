"""The one clock. Scheduling code calls now(); the demo clock and tests move it.

Never call datetime.now() elsewhere in chaarteeke/ (a test checks this).
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class OffsetClock:
    """System time plus a movable offset. The demo clock: 'jump' moves the offset forward only."""

    def __init__(self, offset: timedelta = timedelta(0)) -> None:
        self._offset = offset
        self._lock = threading.Lock()

    @property
    def offset(self) -> timedelta:
        return self._offset

    def now(self) -> datetime:
        return datetime.now(UTC) + self._offset

    def jump_to(self, when: datetime) -> timedelta:
        """Move forward so that now() == when. Moving backwards is refused."""
        with self._lock:
            delta = when - self.now()
            if delta < timedelta(0):
                raise ValueError("the demo clock only moves forward")
            self._offset += delta
            return self._offset

    def reset(self, offset: timedelta = timedelta(0)) -> None:
        with self._lock:
            self._offset = offset


class FixedClock:
    """For tests: time stands still until advance()."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock needs an aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> None:
        self._at += delta

    def set(self, at: datetime) -> None:
        self._at = at


_clock: Clock = SystemClock()


def now() -> datetime:
    return _clock.now()


def current() -> Clock:
    return _clock


def install(clock: Clock) -> None:
    global _clock
    _clock = clock


@contextlib.contextmanager
def use_clock(clock: Clock) -> Iterator[Clock]:
    global _clock
    old = _clock
    _clock = clock
    try:
        yield clock
    finally:
        _clock = old


def local_tz(name: str = "Asia/Kolkata") -> ZoneInfo:
    return ZoneInfo(name)


def local_today(tz: str = "Asia/Kolkata") -> date:
    return now().astimezone(ZoneInfo(tz)).date()


def at_local(day: date, hhmm: str, tz: str = "Asia/Kolkata") -> datetime:
    """The UTC instant of HH:MM local time on a local date."""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def iso(dt: datetime) -> str:
    """Serialise an aware datetime as 2026-10-19T03:04:05.000Z."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def week_start(day: date) -> date:
    """Monday of the ISO week containing day."""
    return day - timedelta(days=day.weekday())
