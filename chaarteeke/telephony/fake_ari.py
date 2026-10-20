"""A fake Asterisk ARI server for hardware-free tests.

It implements only the ARI surface the adapter uses, with the same URL shapes, auth and event
JSON as Asterisk 22 (taken from the ARI docs, not captured from a live PBX; the real-Asterisk
test in tests/test_real_asterisk.py is what pins them to reality).

Faithfulness rules that matter:
  * An originated channel is NOT in Stasis (no events delivered) until it is answered or the app
    has subscribed to it, exactly like Asterisk.
  * Commands on an unknown channel return 404.
  * The test drives the "network" through helper methods: incoming_call(), answer(), press(),
    finish_recording(), reject(), remote_hangup().
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime

from aiohttp import WSMsgType, web

USER, PASSWORD = "chaar", "test-secret"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000"


class FakeAri:
    def __init__(self, app: str = "chaar"):
        self.app = app
        self.requests: list[tuple[str, str, dict]] = []  # (method, path, query) for assertions
        self.channels: dict[str, dict] = {}
        self.subscribed: set[str] = set()
        self.recordings: dict[str, bytes] = {}
        self.stopped_playbacks: list[str] = []
        self.fail_next: dict[str, int] = {}  # command label -> HTTP status to return once
        self._ws: web.WebSocketResponse | None = None
        # Like Asterisk, any application name may connect (it is registered by connecting). Measured on Ubuntu 24.04
        # with Asterisk 20.6 (20 Oct 2026): when ANY event socket of an application closes, the application is
        # unregistered, even though another socket of the same name is still open and the app's calls stop.
        self.apps: set[str] = set()
        self.event_sockets_seen: list[str] = []  # the application name of every event socket, in order
        self._ids = itertools.count(1)
        self._runner: web.AppRunner | None = None
        self.port = 0

    # ------------------------------------------------------------- server
    async def start(self) -> str:
        app = web.Application(middlewares=[self._auth_mw])
        r = app.router
        r.add_get("/ari/events", self._events)
        r.add_get("/ari/applications/{app}", self._application)
        r.add_post("/ari/channels", self._originate)
        r.add_delete("/ari/channels/{id}", self._delete_channel)
        r.add_post("/ari/channels/{id}/play", self._play)
        r.add_delete("/ari/playbacks/{id}", self._stop_playback)
        r.add_post("/ari/channels/{id}/record", self._record)
        r.add_post("/ari/applications/{app}/subscription", self._subscribe)
        r.add_get("/ari/recordings/stored/{name}/file", self._rec_file)
        r.add_delete("/ari/recordings/stored/{name}", self._rec_delete)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._runner:
            await self._runner.cleanup()

    async def drop_event_socket(self) -> None:
        await self._ws.close()

    @web.middleware
    async def _auth_mw(self, request, handler):
        ok = request.query.get("api_key") == f"{USER}:{PASSWORD}"
        if not ok:
            h = request.headers.get("Authorization", "")
            import base64

            ok = h == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if not ok:
            return web.json_response({"message": "Authentication required"}, status=401)
        self.requests.append((request.method, request.path, dict(request.query)))
        label = request.path.rsplit("/", 1)[-1]
        if request.method == "DELETE" and request.path.startswith("/ari/channels/"):
            label = "hangup"
        if request.method == "DELETE" and request.path.startswith("/ari/playbacks/"):
            label = "stop_playback"
        if label in self.fail_next:
            return web.json_response({"message": "forced failure"}, status=self.fail_next.pop(label))
        return await handler(request)

    # ---------------------------------------------------------- ARI routes
    async def _events(self, request):
        name = request.query.get("app", "")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws = ws
        self.apps.add(name)
        self.event_sockets_seen.append(name)
        try:
            async for msg in ws:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self.apps.discard(name)
        return ws

    async def _application(self, request):
        name = request.match_info["app"]
        if name not in self.apps:
            return web.json_response({"message": f"Application does not exist: {name}"}, status=404)
        return web.json_response({"name": name})

    async def _originate(self, request):
        q = request.query
        cid = q.get("channelId") or f"fake-{next(self._ids)}"
        self.channels[cid] = {
            "id": cid,
            "name": q["endpoint"] + f"-{next(self._ids):08x}",
            "state": "Ring",
            "caller": {"name": "", "number": ""},
            "connected": {"name": "", "number": ""},
            "app_args": q.get("appArgs", ""),
            "in_stasis": False,
            "answered": False,
        }
        return web.json_response(self._chan_json(cid))

    async def _subscribe(self, request):
        src = request.query["eventSource"]
        cid = src.split(":", 1)[1]
        if cid not in self.channels:
            return web.json_response({"message": "Event source not found"}, status=422)
        self.subscribed.add(cid)
        return web.json_response({"name": self.app, "channel_ids": sorted(self.subscribed)})

    async def _delete_channel(self, request):
        cid = request.match_info["id"]
        if cid not in self.channels:
            return web.json_response({"message": "Channel not found"}, status=404)
        await self._destroy(cid, 16, "Normal Clearing")
        return web.Response(status=204)

    async def _play(self, request):
        cid = request.match_info["id"]
        ch = self.channels.get(cid)
        if not ch or not ch["answered"]:
            return web.json_response(
                {"message": "Channel not found" if not ch else "Channel not in Stasis"},
                status=404 if not ch else 409,
            )
        pid = request.query.get("playbackId") or f"pb-{next(self._ids)}"
        ch.setdefault("playbacks", []).append(pid)
        ch["last_media"] = request.query["media"]
        return web.json_response(
            {
                "id": pid,
                "media_uri": request.query["media"].split(",")[0],
                "target_uri": f"channel:{cid}",
                "state": "queued",
            },
            status=201,
        )

    async def _stop_playback(self, request):
        """Asterisk ends a stopped playback and still reports it finished."""
        pid = request.match_info["id"]
        for cid, ch in self.channels.items():
            if pid in ch.get("playbacks", []):
                ch["playbacks"].remove(pid)
                self.stopped_playbacks.append(pid)
                await self._send(
                    {
                        "type": "PlaybackFinished",
                        "playback": {
                            "id": pid,
                            "media_uri": "sound:x",
                            "target_uri": f"channel:{cid}",
                            "state": "done",
                        },
                    }
                )
                return web.Response(status=204)
        return web.json_response({"message": "Playback not found"}, status=404)

    async def _record(self, request):
        cid = request.match_info["id"]
        ch = self.channels.get(cid)
        if not ch or not ch["answered"]:
            return web.json_response({"message": "Channel not found"}, status=404)
        name = request.query["name"]
        if name in self.recordings and request.query.get("ifExists", "fail") == "fail":
            return web.json_response({"message": "Recording already exists"}, status=409)
        ch["recording"] = name
        return web.json_response(
            {
                "name": name,
                "format": request.query.get("format", "wav"),
                "target_uri": f"channel:{cid}",
                "state": "queued",
            },
            status=201,
        )

    async def _rec_file(self, request):
        name = request.match_info["name"]
        if name not in self.recordings:
            return web.json_response({"message": "Recording not found"}, status=404)
        return web.Response(body=self.recordings[name], content_type="audio/wav")

    async def _rec_delete(self, request):
        if self.recordings.pop(request.match_info["name"], None) is None:
            return web.json_response({"message": "Recording not found"}, status=404)
        return web.Response(status=204)

    # --------------------------------------------- network-side test helpers
    def _chan_json(self, cid: str) -> dict:
        c = self.channels[cid]
        return {k: c[k] for k in ("id", "name", "state", "caller", "connected")} | {
            "accountcode": "",
            "dialplan": {"context": "quectel-incoming", "exten": "s", "priority": 1},
            "creationtime": _now(),
            "language": "hi",
        }

    async def _send(self, ev: dict) -> None:
        ev = {"application": self.app, "timestamp": _now()} | ev
        await self._ws.send_str(json.dumps(ev))

    async def incoming_call(self, number: str | None) -> str:
        """A patient's missed call arrives: dialplan hands it to Stasis(app,incoming)."""
        cid = f"in-{next(self._ids)}"
        self.channels[cid] = {
            "id": cid,
            "name": f"Quectel/quectel0-{next(self._ids):08x}",
            "state": "Ring",
            "caller": {"name": "", "number": number or ""},
            "connected": {"name": "", "number": ""},
            "in_stasis": True,
            "answered": False,
        }
        await self._send({"type": "StasisStart", "args": ["incoming"], "channel": self._chan_json(cid)})
        return cid

    async def answer(self, cid: str) -> None:
        ch = self.channels[cid]
        ch.update(state="Up", answered=True, in_stasis=True)
        args = ch["app_args"].split(",") if ch.get("app_args") else []
        await self._send({"type": "StasisStart", "args": args, "channel": self._chan_json(cid)})

    async def press(self, cid: str, digit: str) -> None:
        await self._send(
            {
                "type": "ChannelDtmfReceived",
                "digit": digit,
                "duration_ms": 120,
                "channel": self._chan_json(cid),
            }
        )

    async def finish_playback(self, cid: str, state: str = "done") -> None:
        pid = self.channels[cid]["playbacks"].pop(0)
        await self._send(
            {
                "type": "PlaybackFinished",
                "playback": {
                    "id": pid,
                    "media_uri": "sound:x",
                    "target_uri": f"channel:{cid}",
                    "state": state,
                },
            }
        )

    async def finish_recording(
        self,
        cid: str,
        audio: bytes = b"RIFFfake",
        duration: int = 6,
        talking: int = 4,
        fail: str | None = None,
    ) -> None:
        name = self.channels[cid].pop("recording")
        if fail:
            await self._send(
                {
                    "type": "RecordingFailed",
                    "recording": {
                        "name": name,
                        "format": "wav",
                        "state": "failed",
                        "cause": fail,
                        "target_uri": f"channel:{cid}",
                    },
                }
            )
            return
        self.recordings[name] = audio
        await self._send(
            {
                "type": "RecordingFinished",
                "recording": {
                    "name": name,
                    "format": "wav",
                    "state": "done",
                    "target_uri": f"channel:{cid}",
                    "duration": duration,
                    "talking_duration": talking,
                    "silence_duration": duration - talking,
                },
            }
        )

    async def reject(self, cid: str, cause: int = 21, text: str = "Call Rejected") -> None:
        """Callee rejected / busy / no answer: channel dies without ever entering Stasis."""
        await self._destroy(cid, cause, text)

    async def remote_hangup(self, cid: str) -> None:
        await self._destroy(cid, 16, "Normal Clearing")

    async def _destroy(self, cid: str, cause: int, text: str) -> None:
        ch = self.channels[cid]
        deliver = ch["in_stasis"] or cid in self.subscribed
        body = self._chan_json(cid)
        del self.channels[cid]
        if deliver:
            await self._send({"type": "ChannelDestroyed", "cause": cause, "cause_txt": text, "channel": body})
