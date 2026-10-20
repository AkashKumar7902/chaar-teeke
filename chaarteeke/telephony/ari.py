"""Asterisk ARI implementation of the telephony port.

One WebSocket (events) plus REST (commands). Stasis app name is ``app``.

Dialplan contract (see asterisk/etc/asterisk/extensions.conf):
  inbound call  -> Stasis(app, incoming)   : adapter reports MissedCall and rejects the call
  originate()   -> app args "out,<correlation_id>", entered on pick-up

No fallbacks: if the socket drops, ``events()`` raises TelephonyConnectionError. If a REST
command is refused, CommandRejected is raised to the caller. Nothing is retried silently.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import aiohttp

from .model import (
    CallAnswered,
    CommandRejected,
    Dtmf,
    Event,
    Hangup,
    MissedCall,
    PlaybackFinished,
    RecordingDone,
    TelephonyConnectionError,
)

log = logging.getLogger("chaar.telephony.ari")

NOTHING_RECORDED = (
    "Asterisk finished the recording without recording anything (is its 'beep' sound installed?)"
)


@dataclass
class _Call:
    kind: str  # "out" or "missed"
    correlation_id: str = ""
    number: str = ""
    answered_mono: float | None = None


def _line(channel_name: str | None) -> str | None:
    """The modem a call came in on: 'Quectel/quectel0-00000001' -> 'quectel0'."""
    if not channel_name or "/" not in channel_name:
        return None
    return channel_name.split("/", 1)[1].rsplit("-", 1)[0] or None


def _ts(event: dict) -> datetime:
    # ARI timestamps look like 2026-10-19T10:00:00.000+0530
    return datetime.fromisoformat(event["timestamp"])


class AriAdapter:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        app: str = "chaar",
        endpoint_template: str = "Quectel/quectel0/{number}",
        session: aiohttp.ClientSession | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._user, self._pw = username, password
        self._hdr = {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}
        self._app = app
        self._endpoint_template = endpoint_template
        self._session = session
        self._own_session = session is None
        self._queue: asyncio.Queue[Event | BaseException] = asyncio.Queue()
        self._calls: dict[str, _Call] = {}
        self._reader: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        self._reader = asyncio.create_task(self._read_events(), name="ari-events")
        # Fail fast and loud if the PBX is not reachable; no half-started adapter.
        await asyncio.wait_for(self._ready.wait(), timeout=10)
        if self._reader.done():
            self._reader.result()  # re-raises the connection error

    async def stop(self) -> None:
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        for t in list(self._bg):
            t.cancel()
        if self._own_session and self._session:
            await self._session.close()

    async def events(self) -> AsyncIterator[Event]:
        while True:
            item = await self._queue.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    # ------------------------------------------------------------- commands
    async def originate(self, number: str, correlation_id: str, *, ring_timeout_s: int = 30) -> str:
        call_id = f"ct-{uuid.uuid4().hex[:16]}"
        self._calls[call_id] = _Call("out", correlation_id, number)
        try:
            await self._post(
                "/channels",
                "originate",
                params={
                    "endpoint": self._endpoint_template.format(number=number),
                    "app": self._app,
                    "appArgs": f"out,{correlation_id}",
                    "channelId": call_id,
                    "timeout": ring_timeout_s,
                },
            )
            # Before pick-up the channel is not in Stasis, so ask for its events explicitly,
            # otherwise a busy / no-answer / rejected call would never produce a Hangup.
            await self._post(
                f"/applications/{self._app}/subscription",
                "subscribe",
                params={"eventSource": f"channel:{call_id}"},
            )
        except BaseException:
            self._calls.pop(call_id, None)
            raise
        return call_id

    async def play(self, call_id: str, media: Sequence[str], playback_id: str) -> None:
        uris = ",".join(f"sound:{m}" for m in media)
        await self._post(
            f"/channels/{call_id}/play", "play", params={"media": uris, "playbackId": playback_id}
        )

    async def record(
        self,
        call_id: str,
        name: str,
        *,
        max_seconds: int = 20,
        max_silence_s: int = 3,
        terminate_on: str = "#",
    ) -> None:
        await self._post(
            f"/channels/{call_id}/record",
            "record",
            params={
                "name": name,
                "format": "wav",
                "maxDurationSeconds": max_seconds,
                "maxSilenceSeconds": max_silence_s,
                "ifExists": "fail",
                "beep": "true",
                "terminateOn": terminate_on,
            },
        )

    async def stop_playback(self, call_id: str, playback_id: str) -> None:
        await self._request("DELETE", f"/playbacks/{quote(playback_id)}", "stop_playback")

    async def hangup(self, call_id: str) -> None:
        await self._request("DELETE", f"/channels/{call_id}", "hangup")

    async def fetch_recording(self, name: str) -> bytes:
        resp = await self._raw("GET", f"/recordings/stored/{quote(name)}/file", "fetch_recording")
        return resp

    async def delete_recording(self, name: str) -> None:
        await self._request("DELETE", f"/recordings/stored/{quote(name)}", "delete_recording")

    # ----------------------------------------------------------- REST helpers
    async def _post(self, path: str, label: str, params: dict | None = None):
        return await self._request("POST", path, label, params)

    async def _request(self, method: str, path: str, label: str, params: dict | None = None):
        async with self._session.request(
            method, f"{self._base}/ari{path}", params=params, headers=self._hdr
        ) as r:
            body = await r.text()
            if r.status >= 300:
                raise CommandRejected(label, r.status, body)
            return json.loads(body) if body else None

    async def _raw(self, method: str, path: str, label: str) -> bytes:
        async with self._session.request(method, f"{self._base}/ari{path}", headers=self._hdr) as r:
            data = await r.read()
            if r.status >= 300:
                raise CommandRejected(label, r.status, data.decode("utf-8", "replace"))
            return data

    # ------------------------------------------------------------ event loop
    async def _read_events(self) -> None:
        ws_url = (
            self._base.replace("http://", "ws://").replace("https://", "wss://")
            + f"/ari/events?app={quote(self._app)}"
            + f"&api_key={quote(self._user)}:{quote(self._pw)}"
        )
        try:
            async with self._session.ws_connect(ws_url, heartbeat=20) as ws:
                self._ready.set()
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._dispatch(json.loads(msg.data))
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        break
            raise TelephonyConnectionError("ARI event socket closed")
        except asyncio.CancelledError:
            raise
        except TelephonyConnectionError as e:
            self._ready.set()
            self._queue.put_nowait(e)
            raise
        except Exception as e:  # connect refused, 401, protocol error ...
            err = TelephonyConnectionError(f"ARI event socket failed: {e!r}")
            self._ready.set()
            self._queue.put_nowait(err)
            raise err from e

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)

        def done(task: asyncio.Task):
            self._bg.discard(task)
            if not task.cancelled() and task.exception():
                self._queue.put_nowait(task.exception())

        t.add_done_callback(done)

    def _dispatch(self, ev: dict) -> None:
        kind = ev.get("type")
        ch = ev.get("channel") or {}
        cid = ch.get("id")

        if kind == "StasisStart":
            args = ev.get("args") or []
            if args[:1] == ["incoming"]:
                number = (ch.get("caller") or {}).get("number") or None
                self._calls[cid] = _Call("missed", number=number or "")
                self._queue.put_nowait(MissedCall(cid, number, _ts(ev), line=_line(ch.get("name"))))
                # Reject without answering: a missed call costs the patient nothing.
                self._spawn(self._request("DELETE", f"/channels/{cid}", "reject_incoming"))
            elif args[:1] == ["out"] and cid in self._calls:
                call = self._calls[cid]
                call.answered_mono = time.monotonic()
                self._queue.put_nowait(CallAnswered(cid, call.correlation_id, call.number, _ts(ev)))
            else:
                log.error("StasisStart with unexpected args %r on %s; ignored", args, cid)

        elif kind == "ChannelDtmfReceived":
            if cid in self._calls and self._calls[cid].kind == "out":
                self._queue.put_nowait(Dtmf(cid, ev["digit"], _ts(ev)))

        elif kind == "PlaybackFinished":
            pb = ev["playback"]
            target = pb.get("target_uri", "")
            if target.startswith("channel:"):
                self._queue.put_nowait(
                    PlaybackFinished(target[8:], pb["id"], pb.get("state") == "done", _ts(ev))
                )

        elif kind in ("RecordingFinished", "RecordingFailed"):
            rec = ev["recording"]
            target = rec.get("target_uri", "")
            if target.startswith("channel:"):
                ok = kind == "RecordingFinished"
                cause = None if ok else rec.get("cause")
                if ok and rec.get("duration") is None:
                    # A recording that ran has a length, even a silent one (0). Asterisk ends one at once, with no
                    # length and no file, when it cannot start it: with beep=true that is the case if its 'beep'
                    # sound is not installed (found on a real Asterisk, 19 Oct 2026). Nothing was recorded.
                    ok, cause = False, NOTHING_RECORDED
                self._queue.put_nowait(
                    RecordingDone(
                        target[8:],
                        rec["name"],
                        ok,
                        rec.get("duration"),
                        rec.get("talking_duration"),
                        cause,
                        _ts(ev),
                    )
                )

        elif kind == "ChannelDestroyed":
            call = self._calls.pop(cid, None)
            if call and call.kind == "out":
                dur = (time.monotonic() - call.answered_mono) if call.answered_mono else 0.0
                self._queue.put_nowait(
                    Hangup(
                        cid,
                        call.correlation_id,
                        call.number,
                        call.answered_mono is not None,
                        round(dur, 1),
                        ev.get("cause"),
                        ev.get("cause_txt"),
                        _ts(ev),
                    )
                )
