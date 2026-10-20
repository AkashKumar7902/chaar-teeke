"""Opt-in integration test against a REAL Asterisk (no dongle needed; Local channels play the network).

Run only when you have started Asterisk with telephony/asterisk/etc/asterisk/*.conf plus
asterisk/test/extensions_test.conf (see TELEPHONY.md, "Test strategy, level 2"):

    CHAAR_REAL_ARI=http://127.0.0.1:8088 CHAAR_REAL_ARI_PASSWORD=... \
    CHAAR_ASTERISK_CLI='/path/to/sbin/asterisk -C /path/to/etc/asterisk/asterisk.conf' \
    uv run --with pytest --with aiohttp --with-editable . python -m pytest tests/test_real_asterisk.py -q

Without CHAAR_REAL_ARI the module is skipped. That is a deliberate opt-in, not a substitute: the
fake-ARI tests and these tests assert the same contract.
"""

import asyncio
import os
import shlex
import subprocess

import pytest

from chaarteeke.telephony import (
    AriAdapter,
    CallAnswered,
    Dtmf,
    Hangup,
    MissedCall,
    PlaybackFinished,
    RecordingDone,
)

URL = os.environ.get("CHAAR_REAL_ARI")
pytestmark = pytest.mark.skipif(not URL, reason="CHAAR_REAL_ARI not set (opt-in real-Asterisk test)")


def cli(cmd: str) -> str:
    base = shlex.split(os.environ["CHAAR_ASTERISK_CLI"])
    return subprocess.run(base + ["-rx", cmd], capture_output=True, text=True, timeout=20).stdout


async def collect(it, want: set[type], timeout=40):
    got = []

    async def run():
        async for ev in it:
            got.append(ev)
            if want <= {type(e) for e in got}:
                return

    await asyncio.wait_for(run(), timeout)
    return got


def new_adapter(template="Local/s@ct-sim-patient/n"):
    return AriAdapter(URL, "chaar", os.environ["CHAAR_REAL_ARI_PASSWORD"], endpoint_template=template)


def test_missed_call_reported_and_rejected_without_answer():
    async def go():
        a = new_adapter()
        await a.start()
        try:
            cli("channel originate Local/miss@ct-sim-in application Wait 8")
            ev = await asyncio.wait_for(a.events().__anext__(), 15)
            assert isinstance(ev, MissedCall) and ev.caller_number == "+919876543210"
            await asyncio.sleep(1)
            assert "ct-sim-in" not in cli("core show channels concise")  # call was torn down
        finally:
            await a.stop()

    asyncio.run(go())


def test_withheld_caller_id_gives_none():
    async def go():
        a = new_adapter()
        await a.start()
        try:
            cli("channel originate Local/miss@ct-sim-in-withheld application Wait 8")
            ev = await asyncio.wait_for(a.events().__anext__(), 15)
            assert isinstance(ev, MissedCall) and ev.caller_number is None
        finally:
            await a.stop()

    asyncio.run(go())


def test_outbound_call_dtmf_prompt_record_fetch_hangup():
    async def go():
        a = new_adapter()
        await a.start()
        it = a.events()
        try:
            cid = await a.originate("+919876543210", "real-1", ring_timeout_s=20)
            ev = await asyncio.wait_for(it.__anext__(), 20)
            assert isinstance(ev, CallAnswered) and ev.correlation_id == "real-1"
            await a.play(cid, ["chaar/test/hello"], "pb-real-1")
            seen = await collect(it, {Dtmf, PlaybackFinished})
            assert any(isinstance(e, Dtmf) and e.digit == "1" for e in seen)
            assert any(isinstance(e, PlaybackFinished) and e.ok for e in seen)
            await a.record(cid, "real-rec-1", max_seconds=15, max_silence_s=5, terminate_on="#")
            seen = await collect(it, {RecordingDone})
            rec = next(e for e in seen if isinstance(e, RecordingDone))
            assert rec.ok
            data = await a.fetch_recording("real-rec-1")
            assert data[:4] == b"RIFF"
            await a.delete_recording("real-rec-1")
            await a.hangup(cid)
            seen = await collect(it, {Hangup})
            h = next(e for e in seen if isinstance(e, Hangup))
            assert h.answered
        finally:
            await a.stop()

    asyncio.run(go())


def test_busy_callee_gives_hangup_not_answered():
    async def go():
        a = new_adapter("Local/s@ct-sim-busy/n")
        await a.start()
        try:
            await a.originate("+919876543210", "real-2", ring_timeout_s=10)
            ev = await asyncio.wait_for(a.events().__anext__(), 20)
            assert isinstance(ev, Hangup) and not ev.answered
        finally:
            await a.stop()

    asyncio.run(go())
