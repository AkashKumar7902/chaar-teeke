"""CT_TELEPHONY=ari,sim: real calls through the PBX and browser phones side by side.

Calls go to the port that owns the number (``sim:`` to the browser phone, anything else to the PBX).
If either port fails, events() raises its error: nothing is rerouted to the other one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence

from .model import Event, Hangup, TelephonyError, TelephonyPort


class MuxPort:
    def __init__(self, ari: TelephonyPort, sim: TelephonyPort) -> None:
        self.ari = ari
        self.sim = sim
        self._by_call: dict[str, TelephonyPort] = {}
        self._by_recording: dict[str, TelephonyPort] = {}

    async def start(self) -> None:
        await self.sim.start()
        try:
            await self.ari.start()
        except BaseException:
            await self.sim.stop()  # one port up and the other down is a failed start, not a half start
            raise

    async def stop(self) -> None:
        try:
            await self.sim.stop()
        finally:
            await self.ari.stop()

    async def events(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | BaseException] = asyncio.Queue()

        async def pump(port: TelephonyPort) -> None:
            try:
                async for ev in port.events():
                    queue.put_nowait(ev)
            except Exception as e:
                queue.put_nowait(e)

        tasks = [asyncio.create_task(pump(p), name="mux-pump") for p in (self.ari, self.sim)]
        try:
            while True:
                item = await queue.get()
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, Hangup):
                    self._by_call.pop(item.call_id, None)
                yield item
        finally:
            for t in tasks:
                t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)

    def _port(self, call_id: str) -> TelephonyPort:
        try:
            return self._by_call[call_id]
        except KeyError:
            raise TelephonyError("that call is not in progress") from None

    async def originate(self, number: str, correlation_id: str, *, ring_timeout_s: int = 30) -> str:
        port = self.sim if number.startswith("sim:") else self.ari
        call_id = await port.originate(number, correlation_id, ring_timeout_s=ring_timeout_s)
        self._by_call[call_id] = port
        return call_id

    async def play(self, call_id: str, media: Sequence[str], playback_id: str) -> None:
        await self._port(call_id).play(call_id, media, playback_id)

    async def record(
        self,
        call_id: str,
        name: str,
        *,
        max_seconds: int = 20,
        max_silence_s: int = 3,
        terminate_on: str = "#",
    ) -> None:
        port = self._port(call_id)
        self._by_recording[name] = port
        await port.record(
            call_id, name, max_seconds=max_seconds, max_silence_s=max_silence_s, terminate_on=terminate_on
        )

    async def stop_playback(self, call_id: str, playback_id: str) -> None:
        await self._port(call_id).stop_playback(call_id, playback_id)

    async def hangup(self, call_id: str) -> None:
        await self._port(call_id).hangup(call_id)

    def _recording_port(self, name: str) -> TelephonyPort:
        try:
            return self._by_recording[name]
        except KeyError:
            raise TelephonyError("no such recording") from None

    async def fetch_recording(self, name: str) -> bytes:
        return await self._recording_port(name).fetch_recording(name)

    async def delete_recording(self, name: str) -> None:
        port = self._recording_port(name)
        await port.delete_recording(name)
        del self._by_recording[name]
