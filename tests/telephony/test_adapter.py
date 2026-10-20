"""Adapter contract tests against the fake ARI server. No hardware, no Asterisk."""

import asyncio

import pytest
from conftest import close_rig, make_rig

from chaarteeke.telephony import (
    CallAnswered,
    CommandRejected,
    Dtmf,
    Hangup,
    MissedCall,
    PlaybackFinished,
    RecordingDone,
    TelephonyConnectionError,
)


def scenario(coro):
    async def wrapper():
        rig = await make_rig()
        try:
            await coro(rig)
        finally:
            await close_rig(rig)

    asyncio.run(wrapper())


def test_missed_call_is_reported_then_rejected_unanswered():
    async def go(rig):
        cid = await rig.fake.incoming_call("+919876543210")
        ev = await rig.next()
        assert isinstance(ev, MissedCall) and ev.caller_number == "+919876543210" and ev.call_id == cid
        await asyncio.sleep(0.1)
        methods = [(m, p) for m, p, _ in rig.fake.requests]
        assert ("DELETE", f"/ari/channels/{cid}") in methods  # rejected
        assert not any(p.endswith("/answer") for _, p in methods)  # never answered

    scenario(go)


def test_withheld_caller_id_is_reported_as_none_not_guessed():
    async def go(rig):
        await rig.fake.incoming_call(None)
        ev = await rig.next()
        assert isinstance(ev, MissedCall) and ev.caller_number is None

    scenario(go)


def test_full_outbound_ivr_call():
    async def go(rig):
        cid = await rig.adapter.originate("+919876543210", "dose-17", ring_timeout_s=25)
        _, _, q = next(r for r in rig.fake.requests if r[1] == "/ari/channels" and r[0] == "POST")
        assert q["endpoint"] == "Quectel/quectel0/+919876543210"
        assert q["appArgs"] == "out,dose-17" and q["app"] == "chaar" and q["timeout"] == "25"
        assert cid in rig.fake.subscribed  # pre-answer events requested

        await rig.fake.answer(cid)
        ev = await rig.next()
        assert isinstance(ev, CallAnswered) and ev.correlation_id == "dose-17" and ev.call_id == cid

        await rig.adapter.play(cid, ["chaar/dogri/consent", "chaar/dogri/press1"], "pb1")
        assert rig.fake.channels[cid]["last_media"] == "sound:chaar/dogri/consent,sound:chaar/dogri/press1"
        await rig.fake.finish_playback(cid)
        assert isinstance(await rig.next(), PlaybackFinished)

        await rig.fake.press(cid, "1")
        d = await rig.next()
        assert isinstance(d, Dtmf) and d.digit == "1"

        await rig.adapter.record(cid, "bite-17", max_seconds=15, max_silence_s=3)
        await rig.fake.finish_recording(cid, b"RIFFaudio", duration=6, talking=4)
        r = await rig.next()
        assert isinstance(r, RecordingDone) and r.ok and r.recording_name == "bite-17" and r.talking_s == 4
        assert await rig.adapter.fetch_recording("bite-17") == b"RIFFaudio"
        await rig.adapter.delete_recording("bite-17")
        with pytest.raises(CommandRejected):
            await rig.adapter.fetch_recording("bite-17")

        await rig.adapter.hangup(cid)
        h = await rig.next()
        assert isinstance(h, Hangup) and h.answered and h.cause_code == 16 and h.correlation_id == "dose-17"

    scenario(go)


def test_patient_hangs_up_mid_call():
    async def go(rig):
        cid = await rig.adapter.originate("+911234567890", "c1")
        await rig.fake.answer(cid)
        await rig.next()
        await rig.fake.remote_hangup(cid)
        h = await rig.next()
        assert isinstance(h, Hangup) and h.answered

    scenario(go)


def test_unanswered_outbound_call_produces_hangup_answered_false():
    async def go(rig):
        cid = await rig.adapter.originate("+911234567890", "c2")
        await rig.fake.reject(cid, 19, "No Answer")
        h = await rig.next()
        assert isinstance(h, Hangup) and not h.answered and h.cause_code == 19 and h.duration_s == 0

    scenario(go)


def test_failed_recording_is_reported_with_cause():
    async def go(rig):
        cid = await rig.adapter.originate("+911234567890", "c3")
        await rig.fake.answer(cid)
        await rig.next()
        await rig.adapter.record(cid, "r3")
        await rig.fake.finish_recording(cid, fail="channel hung up")
        r = await rig.next()
        assert isinstance(r, RecordingDone) and not r.ok and r.cause == "channel hung up"

    scenario(go)


def test_rejected_command_raises_instead_of_being_swallowed():
    async def go(rig):
        rig.fake.fail_next["channels"] = 500
        with pytest.raises(CommandRejected) as e:
            await rig.adapter.originate("+911234567890", "c4")
        assert e.value.status == 500
        with pytest.raises(CommandRejected):
            await rig.adapter.play("no-such-call", ["x"], "p")

    scenario(go)


def test_failed_reject_of_missed_call_surfaces_in_event_stream():
    async def go(rig):
        rig.fake.fail_next["hangup"] = 500
        await rig.fake.incoming_call("+919876543210")
        assert isinstance(await rig.next(), MissedCall)
        with pytest.raises(CommandRejected):
            await rig.next()

    scenario(go)


def test_event_socket_drop_raises_connection_error_no_silent_reconnect():
    async def go(rig):
        await rig.fake.drop_event_socket()
        with pytest.raises(TelephonyConnectionError):
            await rig.next()

    scenario(go)


def test_bad_credentials_fail_start():
    async def go():
        from chaarteeke.telephony import AriAdapter
        from chaarteeke.telephony.fake_ari import FakeAri

        fake = FakeAri()
        url = await fake.start()
        a = AriAdapter(url, "chaar", "wrong")
        try:
            with pytest.raises(TelephonyConnectionError):
                await a.start()
        finally:
            await a.stop()
            await fake.stop()

    asyncio.run(go())


def test_sip_softphone_endpoint_template():
    async def go():
        rig = await make_rig(tpl="PJSIP/softphone")
        try:
            await rig.adapter.originate("+919876543210", "c5")
            _, _, q = next(r for r in rig.fake.requests if r[1] == "/ari/channels" and r[0] == "POST")
            assert q["endpoint"] == "PJSIP/softphone"
        finally:
            await close_rig(rig)

    asyncio.run(go())
