"""The telephony port: the only thing the rest of Chaar Teeke imports.

Events flow out of ``TelephonyPort.events()``; commands go in as method calls.
Nothing above this module knows about Asterisk, ARI, Quectel or SIP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

# The cause a browser phone gives when its page has gone, so the call can never be answered. A retry would
# ring a phone that is not there.
BROWSER_PHONE_GONE = "The browser phone is not connected"


# ----------------------------------------------------------------- events
@dataclass(frozen=True)
class MissedCall:
    """An inbound call rang and was rejected without being answered.

    `line` names the modem or number it arrived on (the ARI endpoint, e.g. "quectel0"), so a deployment with a
    line per clinic can tell which clinic was rung. `clinic_id` is set by the browser phone when it was opened
    from a clinic's own view: that clinic's desk is giving the call, and nobody else should see it."""

    call_id: str
    caller_number: str | None  # E.164 as delivered by CLIP; None = withheld/unknown
    at: datetime
    line: str | None = None
    clinic_id: str | None = None


@dataclass(frozen=True)
class CallAnswered:
    """An outbound call placed by ``originate`` was picked up."""

    call_id: str
    correlation_id: str  # the id the app passed to originate()
    number: str
    at: datetime


@dataclass(frozen=True)
class Dtmf:
    call_id: str
    digit: str  # one of 0-9 * # A-D
    at: datetime


@dataclass(frozen=True)
class PlaybackFinished:
    """Needed to sequence prompts. Not in the original five-event list; see TELEPHONY.md."""

    call_id: str
    playback_id: str
    ok: bool
    at: datetime


@dataclass(frozen=True)
class RecordingDone:
    call_id: str
    recording_name: str
    ok: bool
    duration_s: float | None
    talking_s: float | None  # speech seconds as measured by Asterisk's silence detector
    cause: str | None  # set when ok is False
    at: datetime


@dataclass(frozen=True)
class Hangup:
    """Terminal event for an outbound call, answered or not."""

    call_id: str
    correlation_id: str
    number: str
    answered: bool  # False => ringing ended without pick-up (busy / no answer / failed)
    duration_s: float  # seconds from answer to hangup, 0 if never answered
    cause_code: int | None  # Q.850 cause, e.g. 16 normal, 17 busy, 19 no answer, 21 rejected
    cause_text: str | None
    at: datetime


Event = MissedCall | CallAnswered | Dtmf | PlaybackFinished | RecordingDone | Hangup


# ---------------------------------------------------------------- errors
class TelephonyError(Exception):
    """Base class. The adapter never swallows these or substitutes another path."""


class TelephonyConnectionError(TelephonyError):
    """The event stream to the PBX is down. The supervisor must decide what to do."""


class CommandRejected(TelephonyError):
    def __init__(self, command: str, status: int, body: str):
        super().__init__(f"{command} rejected by PBX: HTTP {status}: {body[:300]}")
        self.command, self.status, self.body = command, status, body


# ------------------------------------------------------------------ port
class TelephonyPort(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def events(self) -> AsyncIterator[Event]: ...

    # commands
    async def originate(self, number: str, correlation_id: str, *, ring_timeout_s: int = 30) -> str:
        """Place a call. Returns call_id immediately; CallAnswered or Hangup(answered=False) follows."""

    async def play(self, call_id: str, media: Sequence[str], playback_id: str) -> None:
        """Play sound names (e.g. 'chaar/dogri/consent') in order; PlaybackFinished follows."""

    async def record(
        self,
        call_id: str,
        name: str,
        *,
        max_seconds: int = 20,
        max_silence_s: int = 3,
        terminate_on: str = "#",
    ) -> None:
        """Record caller speech to ``name``; RecordingDone follows."""

    async def stop_playback(self, call_id: str, playback_id: str) -> None:
        """Cut a playing prompt short; PlaybackFinished may or may not follow. A playback that has already
        ended on its own is not an error for the caller to act on (CommandRejected with status 404)."""

    async def hangup(self, call_id: str) -> None: ...

    # recording retrieval (needed by the ASR step; deletion by the privacy rule)
    async def fetch_recording(self, name: str) -> bytes: ...
    async def delete_recording(self, name: str) -> None: ...
