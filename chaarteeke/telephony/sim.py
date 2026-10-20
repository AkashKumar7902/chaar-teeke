"""The browser phone: the telephony port over a WebSocket. Explicit mode CT_TELEPHONY=sim; the page says
SIMULATOR. Nothing here dials a real number.

A phone session is created by POST /api/phone/sessions and its number is ``sim:<session id>``. The page
opens WS /api/phone/ws?session=<id>; this port then behaves like the PBX for that browser.

Browser to server (JSON text frames, plus one binary frame per recording):
    {"type": "missed_call"}                     the visitor gives a missed call
    {"type": "answer"} | {"type": "reject"}     pick up or decline a ring
    {"type": "dtmf", "digit": "1"}              a key (0-9 * # A-D)
    {"type": "played", "playback_id": id, "ok": true}   the browser finished (or failed) a play
    <binary WAV, 16-bit PCM, 16 kHz>            the answer to a "record" (an empty frame = nothing recorded)
    {"type": "hangup"}                          the visitor ends the call
Server to browser:
    {"type": "ready"}                           the socket is attached
    {"type": "missed_call_received"}            the missed call was taken
    {"type": "ring", "call_id": id}
    {"type": "play", "playback_id": id, "urls": [...]}      play the files in order, then send "played"
    {"type": "stop", "playback_id": id}         stop that playback now (the person pressed a key that answers
                                                the question); no "played" is expected for it
    {"type": "record", "name": n, "max_seconds": s, "silence_s": s}   beep, record, send one binary frame
    {"type": "hangup", "reason": "no_answer" | "server"}
    {"type": "error", "message": plain words}   a message the server refused
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import math
import secrets
import sys
import time
import wave
from array import array
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from .. import clock
from .model import (
    BROWSER_PHONE_GONE,
    CallAnswered,
    CommandRejected,
    Dtmf,
    Event,
    Hangup,
    MissedCall,
    PlaybackFinished,
    RecordingDone,
    TelephonyError,
)

TIMED_OUT = 4408  # WebSocket close code: the phone sat idle, or reached its longest time
SERVER_STOPPED = "The server stopped"
BROWSER_CLOSED = "The browser phone was closed"
MEDIA_PREFIX = "chaar"
MEDIA_URL_PREFIX = "/media/prompts"
KEYS = frozenset("0123456789*#ABCD")
# A signal level, not an operating threshold: a 20 ms window whose RMS is below this (16-bit samples,
# about -36 dBFS) counts as silence when the simulator works out how long the visitor spoke.
MIN_SPEECH_RMS = 500
WINDOW_S = 0.02
# Format limits that bound the work one browser message can cause (not operating thresholds): the page
# records mono 16 kHz, so anything beyond CD-quality stereo, or a text frame longer than a few keys' worth
# of JSON, is not a phone page speaking.
MAX_RATE_HZ = 48000
MAX_CHANNELS = 2
MAX_TEXT_CHARS = 4096
WAV_HEADER_ALLOWANCE = 1024


def media_url(name: str) -> str:
    """chaar/hi/slot/day_3 -> /media/prompts/hi/slot/day_3.wav"""
    head = MEDIA_PREFIX + "/"
    if not name.startswith(head) or ".." in name:
        raise ValueError(f"not a prompt media name: {name}")
    return f"{MEDIA_URL_PREFIX}/{name[len(head):]}.wav"


@dataclass
class _Record:
    name: str
    max_seconds: int
    silence_s: int


@dataclass
class _SimCall:
    call_id: str
    correlation_id: str
    number: str
    answered_mono: float | None = None
    playing: set[str] = field(default_factory=set)
    stopped: set[str] = field(default_factory=set)  # playbacks cut short; a late "played" for one is ignored
    record: _Record | None = None
    ring: asyncio.Task | None = None


@dataclass
class SimSession:
    id: str
    owner: str
    created: datetime  # from the injected clock
    missed_calls_per_minute: (
        int  # policy visitor.phone_missed_calls_per_minute, counted per owner and address
    )
    address: str | None = None  # the client's network address, as the route saw it
    clinic_id: str | None = None  # set when the phone was opened from a clinic's own view
    idle_s: float | None = None  # policy visitor.phone_socket_idle_s: close after this long without a message
    max_age_s: float | None = None  # policy visitor.phone_socket_max_s: close after this long attached
    ws: WebSocket | None = None
    call: _SimCall | None = None


@dataclass(frozen=True)
class WavInfo:
    ok: bool
    duration_s: float | None
    talking_s: float | None
    cause: str | None


def analyse_wav(data: bytes, max_seconds: int, silence_s: int) -> WavInfo:
    """Length and speech seconds of a recording, or why it cannot be used. Nothing is guessed."""
    if not data:
        return WavInfo(False, None, None, "nothing was recorded")
    if len(data) > (max_seconds + silence_s) * MAX_RATE_HZ * 2 * MAX_CHANNELS + WAV_HEADER_ALLOWANCE:
        return WavInfo(False, None, None, "the recording is longer than allowed")  # before it is parsed
    try:
        with wave.open(io.BytesIO(data)) as w:
            channels, width, rate, frames = (
                w.getnchannels(),
                w.getsampwidth(),
                w.getframerate(),
                w.getnframes(),
            )
            if width != 2 or rate <= 0:
                return WavInfo(False, None, None, "the recording is not 16-bit audio")
            if rate > MAX_RATE_HZ or channels > MAX_CHANNELS:
                return WavInfo(False, None, None, "the recording format is not one the phone page makes")
            duration = frames / rate
            if duration > max_seconds + silence_s:  # checked before the samples are read
                return WavInfo(False, duration, None, "the recording is longer than allowed")
            pcm = w.readframes(frames)
    except (wave.Error, EOFError):
        return WavInfo(False, None, None, "the recording is not a WAV file")
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    step = max(1, int(rate * WINDOW_S)) * channels
    loud = 0
    for i in range(0, len(samples), step):
        window = samples[i : i + step]
        if math.sqrt(sum(x * x for x in window) / len(window)) >= MIN_SPEECH_RMS:
            loud += 1
    return WavInfo(True, duration, round(loud * WINDOW_S, 2), None)


class SimPort:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event | BaseException] = asyncio.Queue()
        self._sessions: dict[str, SimSession] = {}
        self._by_call: dict[str, SimSession] = {}
        self._recordings: dict[str, bytes] = {}
        # owner, and client address, -> when their recent missed calls came
        self._missed_calls: dict[str, deque[datetime]] = {}

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        for s in list(self._sessions.values()):
            if s.call is not None:
                # a call that ends because the server stopped is not the visitor hanging up
                with contextlib.suppress(TelephonyError):
                    await self._send(s, {"type": "hangup", "reason": "server"})
                self._end_call(s, cause_code=None, cause_text=SERVER_STOPPED)
            if s.ws is not None:
                with contextlib.suppress(Exception):
                    await s.ws.close()
        self._sessions.clear()
        self._by_call.clear()
        self._recordings.clear()
        self._missed_calls.clear()

    async def events(self) -> AsyncIterator[Event]:
        while True:
            item = await self._queue.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    # -------------------------------------------------------------- sessions
    def create_session(
        self,
        owner: str,
        *,
        missed_calls_per_minute: int,
        address: str | None = None,
        clinic_id: str | None = None,
        idle_s: float | None = None,
        max_age_s: float | None = None,
    ) -> SimSession:
        s = SimSession(
            secrets.token_urlsafe(18),
            owner,
            clock.now(),
            missed_calls_per_minute,
            address,
            clinic_id,
            idle_s,
            max_age_s,
        )
        self._sessions[s.id] = s
        return s

    def session(self, session_id: str) -> SimSession | None:
        return self._sessions.get(session_id)

    def sessions_owned_by(self, owner: str) -> int:
        return sum(1 for s in self._sessions.values() if s.owner == owner)

    def sessions_at(self, address: str) -> int:
        """Open sessions made from one client address: a whole room shares one wifi address, but one laptop
        opening phones in a loop does too, so this is capped separately from the owner."""
        return sum(1 for s in self._sessions.values() if s.address == address)

    def session_count(self) -> int:
        return len(self._sessions)

    def drop_older_than(self, age: timedelta) -> int:
        """Forget sessions nobody attached within `age`, so an abandoned page does not hold a cap forever."""
        cutoff = clock.now() - age
        stale = [s.id for s in self._sessions.values() if s.ws is None and s.created < cutoff]
        for sid in stale:
            del self._sessions[sid]
        return len(stale)

    async def attach(self, session_id: str, ws: WebSocket) -> None:
        """Serve one accepted WebSocket until the browser leaves. The session ends with the socket."""
        s = self._sessions.get(session_id)
        if s is None or s.ws is not None:
            raise TelephonyError("this phone session is not open")
        s.ws = ws
        attached = time.monotonic()
        try:
            await self._send(s, {"type": "ready"})
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), self._wait_for(s, attached))
                except TimeoutError:
                    await self._close_for_time(s, attached)
                    break
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    self._on_audio(s, msg["bytes"])
                elif msg.get("text") is not None:
                    await self._on_text(s, msg["text"])
        finally:
            self._end_call(s, cause_code=None, cause_text=BROWSER_CLOSED)
            s.ws = None
            self._sessions.pop(s.id, None)

    @staticmethod
    def _wait_for(s: SimSession, attached: float) -> float | None:
        """Seconds until this socket must be closed if nothing arrives: the idle limit or the age limit,
        whichever comes first; None when the session has neither."""
        limits = []
        if s.idle_s is not None:
            limits.append(s.idle_s)
        if s.max_age_s is not None:
            limits.append(attached + s.max_age_s - time.monotonic())
        return max(0.0, min(limits)) if limits else None

    async def _close_for_time(self, s: SimSession, attached: float) -> None:
        """Tell the person why the phone is closing, then close it."""
        too_old = s.max_age_s is not None and time.monotonic() - attached >= s.max_age_s
        why = (
            "This phone has been open for its longest time. Open a new one to carry on."
            if too_old
            else "This phone was closed because nothing was pressed for a while. Open a new one to carry on."
        )
        with contextlib.suppress(TelephonyError):
            await self._send(s, {"type": "error", "message": why})
        with contextlib.suppress(Exception):
            if s.ws is not None:
                await s.ws.close(code=TIMED_OUT)

    # ------------------------------------------------------- browser messages
    async def _on_text(self, s: SimSession, raw: str) -> None:
        try:
            if len(raw) > MAX_TEXT_CHARS:
                raise ValueError("too long")
            msg = json.loads(raw)
            kind = msg["type"]
            if not isinstance(kind, str):
                raise TypeError("type is not a word")
        except (ValueError, KeyError, TypeError):
            await self._send(s, {"type": "error", "message": "That message was not understood."})
            return
        handler = {
            "missed_call": self._on_missed_call,
            "answer": self._on_answer,
            "reject": self._on_reject,
            "dtmf": self._on_dtmf,
            "played": self._on_played,
            "hangup": self._on_hangup,
        }.get(kind)
        if handler is None:
            await self._send(s, {"type": "error", "message": f"Unknown message: {str(kind)[:30]}"})
            return
        error = handler(s, msg)
        if error:
            await self._send(s, {"type": "error", "message": error})
        elif kind == "missed_call":
            await self._send(s, {"type": "missed_call_received"})

    def _on_missed_call(self, s: SimSession, _msg: dict) -> str | None:
        now = clock.now()
        keys = [f"owner:{s.owner}"] + ([f"address:{s.address}"] if s.address else [])
        windows = []
        for key in keys:
            recent = self._missed_calls.setdefault(key, deque())
            while recent and recent[0] <= now - timedelta(minutes=1):
                recent.popleft()
            windows.append(recent)
        if any(len(recent) >= s.missed_calls_per_minute for recent in windows):
            return "That is too many missed calls in a minute. Wait a moment and try again."
        for recent in windows:
            recent.append(now)
        self._queue.put_nowait(
            MissedCall(f"sim-{secrets.token_hex(6)}", f"sim:{s.id}", now, clinic_id=s.clinic_id)
        )
        return None

    def _on_answer(self, s: SimSession, _msg: dict) -> str | None:
        call = s.call
        if call is None or call.answered_mono is not None:
            return "There is no call ringing."
        if call.ring:
            call.ring.cancel()
        call.answered_mono = time.monotonic()
        self._queue.put_nowait(CallAnswered(call.call_id, call.correlation_id, call.number, clock.now()))
        return None

    def _on_reject(self, s: SimSession, _msg: dict) -> str | None:
        if s.call is None or s.call.answered_mono is not None:
            return "There is no call ringing."
        self._end_call(s, cause_code=21, cause_text="Call rejected")
        return None

    def _on_dtmf(self, s: SimSession, msg: dict) -> str | None:
        digit = msg.get("digit")
        if s.call is None or s.call.answered_mono is None:
            return "There is no call in progress."
        if not isinstance(digit, str) or digit not in KEYS:
            return "That is not a phone key."
        self._queue.put_nowait(Dtmf(s.call.call_id, digit, clock.now()))
        return None

    def _on_played(self, s: SimSession, msg: dict) -> str | None:
        pid = msg.get("playback_id")
        if s.call is not None and isinstance(pid, str) and pid in s.call.stopped:
            s.call.stopped.discard(pid)  # it was cut short on purpose; the browser may still report it ended
            return None
        if s.call is None or not isinstance(pid, str) or pid not in s.call.playing:
            return "That playback was not started."
        ok = msg.get("ok", True)
        if not isinstance(ok, bool):
            return "That playback result was not understood."
        s.call.playing.discard(pid)
        self._queue.put_nowait(PlaybackFinished(s.call.call_id, pid, ok, clock.now()))
        return None

    def _on_hangup(self, s: SimSession, _msg: dict) -> str | None:
        self._end_call(s, cause_code=16, cause_text="Normal clearing")
        return None

    def _on_audio(self, s: SimSession, data: bytes) -> None:
        call = s.call
        if call is None or call.record is None:
            return
        rec, call.record = call.record, None
        info = analyse_wav(data, rec.max_seconds, rec.silence_s)
        if info.ok:
            self._recordings[rec.name] = data
        self._queue.put_nowait(
            RecordingDone(
                call.call_id, rec.name, info.ok, info.duration_s, info.talking_s, info.cause, clock.now()
            )
        )

    # ---------------------------------------------------------------- calls
    def _end_call(self, s: SimSession, *, cause_code: int | None, cause_text: str) -> None:
        """Emit the one Hangup for the session's call, if it has one."""
        call, s.call = s.call, None
        if call is None:
            return
        if call.ring:
            call.ring.cancel()
        self._by_call.pop(call.call_id, None)
        answered = call.answered_mono is not None
        duration = round(time.monotonic() - call.answered_mono, 1) if answered else 0.0
        self._queue.put_nowait(
            Hangup(
                call.call_id,
                call.correlation_id,
                call.number,
                answered,
                duration,
                cause_code,
                cause_text,
                clock.now(),
            )
        )

    def _refuse(self, call_id: str, correlation_id: str, number: str, code: int | None, text: str) -> str:
        self._queue.put_nowait(Hangup(call_id, correlation_id, number, False, 0.0, code, text, clock.now()))
        return call_id

    async def _ring_timeout(self, s: SimSession, call: _SimCall, seconds: int) -> None:
        await asyncio.sleep(seconds)
        if s.call is call and call.answered_mono is None:
            await self._send(s, {"type": "hangup", "reason": "no_answer"})
            self._end_call(s, cause_code=19, cause_text="No answer")

    async def originate(self, number: str, correlation_id: str, *, ring_timeout_s: int = 30) -> str:
        if not number.startswith("sim:"):
            raise TelephonyError("the browser phone only calls sim: numbers")
        call_id = f"sim-{secrets.token_hex(8)}"
        s = self._sessions.get(number.removeprefix("sim:"))
        if s is None or s.ws is None:
            return self._refuse(call_id, correlation_id, number, None, BROWSER_PHONE_GONE)
        if s.call is not None:
            return self._refuse(call_id, correlation_id, number, 17, "The browser phone is busy")
        call = _SimCall(call_id, correlation_id, number)
        s.call = call
        self._by_call[call_id] = s
        try:
            await self._send(s, {"type": "ring", "call_id": call_id})
        except TelephonyError:
            s.call = None
            self._by_call.pop(call_id, None)
            return self._refuse(call_id, correlation_id, number, None, BROWSER_PHONE_GONE)
        call.ring = asyncio.create_task(
            self._ring_timeout(s, call, ring_timeout_s), name=f"sim-ring:{call_id}"
        )
        return call_id

    def _live(self, call_id: str) -> tuple[SimSession, _SimCall]:
        s = self._by_call.get(call_id)
        if s is None or s.call is None or s.call.answered_mono is None:
            raise TelephonyError("that browser call is not in progress")
        return s, s.call

    async def play(self, call_id: str, media: Sequence[str], playback_id: str) -> None:
        s, call = self._live(call_id)
        urls = [media_url(m) for m in media]
        call.playing.add(playback_id)
        await self._send(s, {"type": "play", "playback_id": playback_id, "urls": urls})

    async def stop_playback(self, call_id: str, playback_id: str) -> None:
        """Cut a playing prompt short. A playback that already ended is left alone (nothing to stop)."""
        s, call = self._live(call_id)
        if playback_id not in call.playing:
            raise CommandRejected("stop_playback", 404, "no such playback")
        call.playing.discard(playback_id)
        call.stopped.add(playback_id)
        await self._send(s, {"type": "stop", "playback_id": playback_id})

    async def record(
        self,
        call_id: str,
        name: str,
        *,
        max_seconds: int = 20,
        max_silence_s: int = 3,
        terminate_on: str = "#",
    ) -> None:
        s, call = self._live(call_id)
        if call.record is not None:
            raise TelephonyError("a recording is already in progress")
        call.record = _Record(name, max_seconds, max_silence_s)
        await self._send(
            s, {"type": "record", "name": name, "max_seconds": max_seconds, "silence_s": max_silence_s}
        )

    async def hangup(self, call_id: str) -> None:
        s = self._by_call.get(call_id)
        if s is None or s.call is None:
            return
        with contextlib.suppress(TelephonyError):
            await self._send(s, {"type": "hangup", "reason": "server"})
        self._end_call(s, cause_code=16, cause_text="Normal clearing")

    async def fetch_recording(self, name: str) -> bytes:
        try:
            return self._recordings[name]
        except KeyError:
            raise CommandRejected("fetch_recording", 404, "no such recording") from None

    async def delete_recording(self, name: str) -> None:
        if self._recordings.pop(name, None) is None:
            raise CommandRejected("delete_recording", 404, "no such recording")

    async def _send(self, s: SimSession, message: dict[str, Any]) -> None:
        if s.ws is None:
            raise TelephonyError("the browser phone is not connected")
        try:
            await s.ws.send_json(message)
        except (
            RuntimeError,
            OSError,
            WebSocketDisconnect,
        ) as e:  # the socket died; the receive loop ends the call
            raise TelephonyError("the browser phone is not connected") from e
