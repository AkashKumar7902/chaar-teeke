"""The browser phone port and the mux, driven by a scripted browser socket."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from ivr_support import FakeSocket, silent_wav, speech_wav

from chaarteeke import clock
from chaarteeke.telephony.model import (
    CallAnswered,
    CommandRejected,
    Dtmf,
    Hangup,
    MissedCall,
    PlaybackFinished,
    RecordingDone,
    TelephonyConnectionError,
    TelephonyError,
)
from chaarteeke.telephony.mux import MuxPort
from chaarteeke.telephony.sim import SimPort, analyse_wav, media_url

PER_MINUTE = 6  # policy visitor.phone_missed_calls_per_minute


async def attach(sim: SimPort, owner="visitor:t", per_minute=PER_MINUTE):
    session = sim.create_session(owner, missed_calls_per_minute=per_minute)
    sock = FakeSocket()
    task = asyncio.create_task(sim.attach(session.id, sock))
    await asyncio.sleep(0)
    return session, sock, task


async def next_event(sim: SimPort, timeout=1.0):
    it = sim.events()
    return await asyncio.wait_for(it.__anext__(), timeout)


async def answered_call(sim: SimPort):
    session, sock, task = await attach(sim)
    number = f"sim:{session.id}"
    call_id = await sim.originate(number, "corr-1", ring_timeout_s=5)
    assert sock.sent[-1] == {"type": "ring", "call_id": call_id}
    sock.push({"type": "answer"})
    ev = await next_event(sim)
    assert isinstance(ev, CallAnswered) and ev.call_id == call_id
    return session, sock, task, call_id


def test_media_names_become_prompt_urls():
    assert media_url("chaar/hi/FC_CLOSE.0") == "/media/prompts/hi/FC_CLOSE.0.wav"
    assert media_url("chaar/ur/slot/day_3") == "/media/prompts/ur/slot/day_3.wav"
    for bad in ("hi/FC_CLOSE", "chaar/../secret", "sound:x"):
        with pytest.raises(ValueError):
            media_url(bad)


async def test_the_socket_gets_ready_and_a_missed_call_becomes_an_event():
    sim = SimPort()
    session, sock, task = await attach(sim)
    assert sock.sent == [{"type": "ready"}]
    sock.push({"type": "missed_call"})
    ev = await next_event(sim)
    assert isinstance(ev, MissedCall) and ev.caller_number == f"sim:{session.id}"
    assert {"type": "missed_call_received"} in sock.sent
    sock.disconnect()
    await task


async def test_missed_calls_are_capped_per_visitor_per_minute(fixed_clock):
    sim = SimPort()
    session, sock, task = await attach(sim, owner="visitor:a", per_minute=2)
    other, other_sock, other_task = await attach(
        sim, owner="visitor:a", per_minute=2
    )  # a second phone, same person
    for _ in range(2):
        sock.push({"type": "missed_call"})
        assert isinstance(await next_event(sim), MissedCall)
    other_sock.push({"type": "missed_call"})  # the cap is the visitor's, not the phone's
    await asyncio.sleep(0.05)
    assert (
        other_sock.sent[-1]["type"] == "error" and "too many missed calls" in other_sock.sent[-1]["message"]
    )
    assert sim._queue.empty()
    fixed_clock.advance(timedelta(seconds=61))
    other_sock.push({"type": "missed_call"})
    assert isinstance(await next_event(sim), MissedCall)
    for t, k in ((task, sock), (other_task, other_sock)):
        k.disconnect()
        await t


async def test_ring_answer_play_key_record_and_hangup_over_the_socket():
    sim = SimPort()
    session, sock, task, call_id = await answered_call(sim)

    await sim.play(call_id, ["chaar/hi/FC_HELLO.0", "chaar/hi/slot/clinic_x"], "pb1")
    assert sock.sent[-1] == {
        "type": "play",
        "playback_id": "pb1",
        "urls": ["/media/prompts/hi/FC_HELLO.0.wav", "/media/prompts/hi/slot/clinic_x.wav"],
    }
    sock.push({"type": "played", "playback_id": "pb1", "ok": True})
    ev = await next_event(sim)
    assert isinstance(ev, PlaybackFinished) and (ev.playback_id, ev.ok) == ("pb1", True)

    sock.push({"type": "dtmf", "digit": "7"})
    ev = await next_event(sim)
    assert isinstance(ev, Dtmf) and ev.digit == "7"

    await sim.record(call_id, "rec1", max_seconds=8, max_silence_s=2)
    assert sock.sent[-1] == {"type": "record", "name": "rec1", "max_seconds": 8, "silence_s": 2}
    sock.push_bytes(speech_wav(1.0))
    ev = await next_event(sim)
    assert isinstance(ev, RecordingDone) and ev.ok and ev.recording_name == "rec1"
    assert ev.duration_s == 1.0 and ev.talking_s > 0.8
    assert await sim.fetch_recording("rec1") == speech_wav(1.0)
    await sim.delete_recording("rec1")
    with pytest.raises(CommandRejected):
        await sim.fetch_recording("rec1")

    sock.push({"type": "hangup"})
    ev = await next_event(sim)
    assert isinstance(ev, Hangup) and ev.answered and ev.correlation_id == "corr-1"
    assert (ev.cause_code, ev.number) == (16, f"sim:{session.id}")
    sock.disconnect()
    await task


async def test_a_silent_recording_is_ok_but_has_no_talking_time():
    sim = SimPort()
    _, sock, task, call_id = await answered_call(sim)
    await sim.record(call_id, "rec1", max_seconds=8, max_silence_s=2)
    sock.push_bytes(silent_wav(1.0))
    ev = await next_event(sim)
    assert ev.ok and ev.talking_s == 0
    sock.disconnect()
    await task


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (b"", "nothing was recorded"),
        (b"not a wav", "not a WAV file"),
        (speech_wav(11.0), "longer than allowed"),
    ],
)
async def test_a_recording_that_cannot_be_used_says_why_and_is_not_kept(data, why):
    sim = SimPort()
    _, sock, task, call_id = await answered_call(sim)
    await sim.record(call_id, "rec1", max_seconds=8, max_silence_s=2)
    sock.push_bytes(data)
    ev = await next_event(sim)
    assert not ev.ok and why in ev.cause
    with pytest.raises(CommandRejected):
        await sim.fetch_recording("rec1")
    sock.disconnect()
    await task


def test_analyse_wav_levels_and_formats():
    loud = analyse_wav(speech_wav(2.0), 8, 2)
    assert loud.ok and loud.duration_s == 2.0 and 1.9 <= loud.talking_s <= 2.0
    assert analyse_wav(silent_wav(2.0), 8, 2).talking_s == 0
    assert analyse_wav(speech_wav(10.0), 8, 2).ok  # up to max + silence
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(8000)
        w.writeframes(b"\x80" * 100)
    assert "16-bit" in analyse_wav(buf.getvalue(), 8, 2).cause


async def test_the_phone_not_connected_ends_at_once_without_a_ring():
    sim = SimPort()
    call_id = await sim.originate("sim:nobody", "corr-9", ring_timeout_s=5)
    ev = await next_event(sim)
    assert isinstance(ev, Hangup) and not ev.answered and ev.call_id == call_id
    assert ev.cause_text == "The browser phone is not connected" and ev.correlation_id == "corr-9"


async def test_a_phone_already_on_a_call_is_busy():
    sim = SimPort()
    session, sock, task, _ = await answered_call(sim)
    await sim.originate(f"sim:{session.id}", "corr-2", ring_timeout_s=5)
    ev = await next_event(sim)
    assert isinstance(ev, Hangup) and ev.cause_code == 17 and ev.correlation_id == "corr-2"
    sock.disconnect()
    await task


async def test_a_ring_nobody_answers_times_out_and_tells_the_browser():
    sim = SimPort()
    session, sock, task = await attach(sim)
    call_id = await sim.originate(f"sim:{session.id}", "corr-1", ring_timeout_s=0.05)
    ev = await next_event(sim)
    assert isinstance(ev, Hangup) and not ev.answered and ev.cause_code == 19 and ev.call_id == call_id
    assert {"type": "hangup", "reason": "no_answer"} in sock.sent
    sock.disconnect()
    await task


async def test_rejecting_a_ring_is_a_declined_call():
    sim = SimPort()
    session, sock, task = await attach(sim)
    await sim.originate(f"sim:{session.id}", "corr-1", ring_timeout_s=5)
    sock.push({"type": "reject"})
    ev = await next_event(sim)
    assert isinstance(ev, Hangup) and not ev.answered and ev.cause_code == 21
    sock.disconnect()
    await task


async def test_closing_the_browser_mid_call_hangs_up_and_forgets_the_session():
    sim = SimPort()
    session, sock, task, call_id = await answered_call(sim)
    sock.disconnect()
    await task
    ev = await next_event(sim)
    assert isinstance(ev, Hangup) and ev.answered and "closed" in ev.cause_text
    assert sim.session(session.id) is None and sim.session_count() == 0


async def test_the_server_can_hang_up_and_the_browser_is_told():
    sim = SimPort()
    _, sock, task, call_id = await answered_call(sim)
    await sim.hangup(call_id)
    ev = await next_event(sim)
    assert isinstance(ev, Hangup) and ev.answered
    assert {"type": "hangup", "reason": "server"} in sock.sent
    await sim.hangup(call_id)  # a second hang-up is harmless
    sock.disconnect()
    await task


async def test_commands_for_a_call_that_is_not_in_progress_are_refused():
    sim = SimPort()
    with pytest.raises(TelephonyError):
        await sim.play("nope", ["chaar/hi/x"], "p1")
    with pytest.raises(TelephonyError):
        await sim.record("nope", "r")
    with pytest.raises(TelephonyError):
        await sim.originate("+919999999999", "c")


async def test_bad_messages_get_a_plain_error_and_do_not_end_the_call():
    sim = SimPort()
    session, sock, task, _ = await answered_call(sim)
    sock.incoming.put_nowait({"type": "websocket.receive", "text": "{not json"})
    sock.push({"type": "teleport"})
    sock.push({"type": "dtmf", "digit": "Z"})
    sock.push({"type": "played", "playback_id": "never-started", "ok": True})
    sock.push({"type": "answer"})
    await asyncio.sleep(0.05)
    errors = [m["message"] for m in sock.sent if m["type"] == "error"]
    assert len(errors) == 5 and all(e.endswith(".") or "Unknown" in e for e in errors)
    sock.push({"type": "dtmf", "digit": "1"})
    assert isinstance(await next_event(sim), Dtmf)
    sock.disconnect()
    await task


async def test_a_session_attaches_once_and_unattached_ones_expire(fixed_clock):
    sim = SimPort()
    session, sock, task = await attach(sim, owner="visitor:a")
    with pytest.raises(TelephonyError):
        await sim.attach(session.id, FakeSocket())
    idle = sim.create_session("visitor:a", missed_calls_per_minute=PER_MINUTE)
    assert sim.sessions_owned_by("visitor:a") == 2 and sim.session_count() == 2
    fixed_clock.advance(timedelta(hours=13))
    assert sim.drop_older_than(timedelta(hours=12)) == 1  # the idle one; the attached one stays
    assert sim.session(idle.id) is None and sim.session(session.id) is not None
    sock.disconnect()
    await task
    assert clock.now() > session.created


async def test_stop_closes_every_socket():
    sim = SimPort()
    _, sock, task = await attach(sim)
    await sim.stop()
    assert sock.closed
    sock.disconnect()
    await task


# ------------------------------------------------------------------------ mux
class StubPort:
    def __init__(self, name):
        self.name, self.calls, self.queue = name, [], asyncio.Queue()

    async def start(self):
        self.calls.append("start")

    async def stop(self):
        self.calls.append("stop")

    async def events(self):
        while True:
            item = await self.queue.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    async def originate(self, number, correlation_id, *, ring_timeout_s=30):
        self.calls.append(("originate", number))
        return f"{self.name}-call"

    async def play(self, call_id, media, playback_id):
        self.calls.append(("play", call_id))

    async def record(self, call_id, name, **kw):
        self.calls.append(("record", name))

    async def hangup(self, call_id):
        self.calls.append(("hangup", call_id))

    async def fetch_recording(self, name):
        return self.name.encode()

    async def delete_recording(self, name):
        self.calls.append(("delete", name))


async def test_mux_routes_by_number_and_by_call():
    ari, sim = StubPort("ari"), StubPort("sim")
    mux = MuxPort(ari, sim)
    await mux.start()
    assert await mux.originate("sim:abc", "c1") == "sim-call"
    assert await mux.originate("+919876543210", "c2") == "ari-call"
    await mux.play("sim-call", ["m"], "p")
    await mux.record("ari-call", "rec-a")
    await mux.hangup("sim-call")
    assert ("play", "sim-call") in sim.calls and ("play", "sim-call") not in ari.calls
    assert ("record", "rec-a") in ari.calls and ("hangup", "sim-call") in sim.calls
    assert await mux.fetch_recording("rec-a") == b"ari"
    await mux.delete_recording("rec-a")
    with pytest.raises(TelephonyError):
        await mux.fetch_recording("rec-a")
    with pytest.raises(TelephonyError):
        await mux.play("unknown", ["m"], "p")
    await mux.stop()
    assert "stop" in ari.calls and "stop" in sim.calls


async def test_mux_merges_events_from_both_ports_and_forgets_ended_calls():
    ari, sim = StubPort("ari"), StubPort("sim")
    mux = MuxPort(ari, sim)
    call_id = await mux.originate("sim:abc", "c1")
    at = clock.now()
    ari.queue.put_nowait(MissedCall("ari-9", "+919876543210", at))
    sim.queue.put_nowait(Hangup(call_id, "c1", "sim:abc", True, 3.0, 16, "Normal", at))
    it = mux.events()
    got = {type(await asyncio.wait_for(it.__anext__(), 1)) for _ in range(2)}
    assert got == {MissedCall, Hangup}
    with pytest.raises(TelephonyError):
        await mux.hangup(call_id)
    await it.aclose()


async def test_mux_does_not_reroute_when_one_port_fails():
    ari, sim = StubPort("ari"), StubPort("sim")
    mux = MuxPort(ari, sim)
    ari.queue.put_nowait(TelephonyConnectionError("ARI event socket closed"))
    with pytest.raises(TelephonyConnectionError):
        await asyncio.wait_for(mux.events().__anext__(), 1)


def test_the_protocol_is_documented_in_the_module():
    import chaarteeke.telephony.sim as sim_module

    doc = sim_module.__doc__
    for word in ("missed_call", "answer", "reject", "dtmf", "played", "record", "ring", "play", "hangup"):
        assert f'"{word}"' in doc
    assert "binary" in doc


# -------------------------------------------------- hostile or broken browser messages
async def test_messages_of_the_wrong_shape_get_an_error_and_never_crash_the_session():
    sim = SimPort()
    session, sock, task, call_id = await answered_call(sim)
    await sim.play(call_id, ["chaar/hi/FC_HELLO"], "p1")
    for bad in (
        {"type": ["answer"]},  # unhashable type
        {"type": "played", "playback_id": ["p1"], "ok": True},  # unhashable id
        {"type": "played", "playback_id": "p1", "ok": "yes"},  # not a boolean
        {"type": "dtmf", "digit": ["1"]},
    ):
        sock.push(bad)
    sock.incoming.put_nowait({"type": "websocket.receive", "text": "[1, 2]"})
    sock.incoming.put_nowait({"type": "websocket.receive", "text": "x" * 5000})
    await asyncio.sleep(0.05)
    assert sum(1 for m in sock.sent if m["type"] == "error") == 6
    assert not task.done()  # the socket is still being served
    sock.push({"type": "played", "playback_id": "p1", "ok": True})  # the real answer still works
    assert isinstance(await next_event(sim), PlaybackFinished)
    sock.disconnect()
    await task


async def test_a_recording_sent_in_a_format_the_page_does_not_make_is_refused_before_it_is_analysed():
    sim = SimPort()
    _, sock, task, call_id = await answered_call(sim)
    await sim.record(call_id, "rec-hi", max_seconds=8, max_silence_s=2)
    sock.push_bytes(speech_wav(0.5, rate=96000))  # a sample rate far above anything the page records
    ev = await next_event(sim)
    assert not ev.ok and "format" in ev.cause
    await sim.record(call_id, "rec-big", max_seconds=8, max_silence_s=2)
    sock.push_bytes(b"RIFF" + bytes(2_000_000))  # far more bytes than 10 s of the largest format
    ev = await next_event(sim)
    assert not ev.ok and "longer than allowed" in ev.cause
    sock.disconnect()
    await task


async def test_mux_stops_the_other_port_even_when_one_fails_to_stop():
    ari, sim = StubPort("ari"), StubPort("sim")

    async def broken_stop():
        raise RuntimeError("sim stuck")

    sim.stop = broken_stop
    with pytest.raises(RuntimeError):
        await MuxPort(ari, sim).stop()
    assert "stop" in ari.calls


async def test_mux_that_cannot_start_the_pbx_does_not_leave_the_browser_phone_running():
    ari, sim = StubPort("ari"), StubPort("sim")

    async def broken_start():
        raise TelephonyConnectionError("PBX unreachable")

    ari.start = broken_start
    with pytest.raises(TelephonyConnectionError):
        await MuxPort(ari, sim).start()
    assert sim.calls == ["start", "stop"]
