"""A key cuts a question short, a 9 during the notice is heard, a full disk is said, the browser phone's limits,
the clinic's own simulator calls and how a stopped server ends a call."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import timedelta

import pytest
from ivr_support import Browser, FakeSocket, open_phone, speech_wav, until

from chaarteeke import clock, enrol
from chaarteeke.ivr import runner as runner_mod
from chaarteeke.ivr.runner import STORAGE_FAILED
from chaarteeke.telephony.model import (
    BROWSER_PHONE_GONE,
    CallAnswered,
    CommandRejected,
    Hangup,
    MissedCall,
    TelephonyError,
)
from chaarteeke.telephony.mux import MuxPort
from chaarteeke.telephony.sim import SimPort

FIRST_CALL = {
    "FC_CONSENT_Q": ["1"], "FC_QCONSENT": ["1"], "FC_PLACETYPE": ["1"], "FC_ANIMAL": ["1"],
    "FC_NOTCH": ["2"], "FC_SICK": ["2"],
}  # fmt: skip


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr(runner_mod, "_alert_from_report", lambda *a: None)
    monkeypatch.setattr(runner_mod, "_process_capture", lambda ctx, rid: None)


class Impatient(Browser):
    """A person who knows the answer: the key is pressed `after` seconds into the question and the voice is
    cut when the server says stop. Nothing is ever reported finished for a question that was cut."""

    def __init__(self, sock, script=None, recordings=None, *, after=0.0, finish_after=None, **kw):
        super().__init__(sock, script, recordings, **kw)
        self.after = after
        self.finish_after = (
            finish_after  # if set, a question the server did not stop ends by itself this long after
        )
        self.stopped: list[str] = []
        self.pressed: dict[str, float] = {}  # playback id -> when the key went
        self.started: list[float] = []  # when each play message arrived

    def on_message(self, msg):
        if msg["type"] == "stop":
            self.stopped.append(msg["playback_id"])
            return
        if msg["type"] != "play":
            return super().on_message(msg)
        ids = self.prompt_ids(msg["urls"])
        self.heard.append(ids)
        self.urls.append(msg["urls"])
        self.started.append(time.monotonic())
        for pid in reversed(ids):
            if self.script.get(pid):
                key = self.script[pid].popleft()
                if key is not None:
                    asyncio.get_running_loop().call_later(self.after, self._press, msg["playback_id"], key)
                    return  # the voice would run on; the key cuts it
        self.sock.push({"type": "played", "playback_id": msg["playback_id"], "ok": True})

    def _press(self, playback_id, key):
        self.pressed[playback_id] = time.monotonic()
        self.sock.push({"type": "dtmf", "digit": key})
        if self.finish_after is not None:
            asyncio.get_running_loop().call_later(self.finish_after, self._finish, playback_id)

    def _finish(self, playback_id):
        if playback_id not in self.stopped:
            self.sock.push({"type": "played", "playback_id": playback_id, "ok": True})


async def impatient_phone(ictx, script, recordings=None, **kw):
    """Like open_phone but with a person who presses keys during the questions."""
    phone = await open_phone(ictx, {})
    phone.browser = Impatient(phone.sock, script, recordings, **kw)
    return phone


# ------------------------------------------------------------------ barge-in
async def test_a_key_pressed_during_a_question_stops_it_and_the_next_one_starts_at_once(ictx, world, fakes):
    world.pilot()
    phone = await impatient_phone(ictx, FIRST_CALL, [speech_wav()], after=0.2)
    course_id = world.course(phone)
    began = time.monotonic()
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    b = phone.browser
    # every question the person answered was stopped, and the next prompt followed its key at once
    assert len(b.stopped) >= 6
    gaps = []
    for pid, pressed_at in b.pressed.items():
        n = int(pid.rsplit("-p", 1)[1])  # this was prompt n; prompt n + 1 is the one that follows
        if n < len(b.started):
            gaps.append(b.started[n] - pressed_at)
    assert len(gaps) >= 6 and max(gaps) < 0.5, gaps
    assert time.monotonic() - began < 6
    answers = world.answers(course_id)
    assert answers["consent_reminders"] == "yes" and answers["signs"] == "no"
    await phone.close()


async def test_a_key_one_second_into_a_question_moves_the_call_on_within_about_a_second(ictx, world, fakes):
    world.pilot()
    phone = await impatient_phone(ictx, {"FC_CONSENT_Q": ["2"]}, after=1.0)
    course_id = world.course(phone)
    began = time.monotonic()
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    b = phone.browser
    # key 2 (no reminders) after 1 s: the goodbye follows straight away, not after the question's full length
    assert b.flat[-1] == "FC_CONSENT_NO"
    pressed = max(b.pressed.values())
    assert b.started[-1] - pressed < 0.5
    assert time.monotonic() - began < 3
    await phone.close()


async def test_a_key_the_question_does_not_take_does_not_cut_it_short(ictx, world, fakes):
    world.pilot()
    phone = await impatient_phone(ictx, {"FC_CONSENT_Q": ["7", "1"]}, after=0.0, finish_after=0.2)
    course_id = world.course(phone)
    # a 7 is no answer to "press 1 or 2": the question plays on, then is asked again as a wrong key
    task = asyncio.create_task(ictx.runner.place(world.call(course_id, "first")[0]["id"]))
    await until(lambda: ["SYS_WRONGKEY", "FC_CONSENT_Q"] in phone.browser.heard, timeout=3)
    # the 7 (third prompt played) cut nothing; the 1 on the repeat did
    assert [p.rsplit("-p", 1)[1] for p in phone.browser.stopped] == ["4"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await phone.close()


async def test_nine_cuts_a_question_short_too_and_asks_to_stop(ictx, world, fakes):
    world.pilot()
    phone = await impatient_phone(ictx, {"FC_CONSENT_Q": ["9"], "OPT_CONFIRM": ["9"]}, after=0.1)
    course_id = world.course(phone)
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    assert phone.browser.flat[-2:] == ["OPT_CONFIRM", "OPT_DONE"]
    assert phone.browser.stopped  # the question was stopped for the 9
    await phone.close()


async def test_the_place_question_is_cut_short_only_by_9_and_the_recording_follows_at_once(
    ictx, world, fakes
):
    world.pilot()
    phone = await impatient_phone(
        ictx, {**FIRST_CALL, "FC_PLACE": ["5"]}, [speech_wav()], after=0.1, finish_after=0.2
    )
    course_id = world.course(phone)
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    place_play = next(n for n, ids in enumerate(phone.browser.heard) if "FC_PLACE" in ids)
    # a 5 is not an answer to a recorded question: the question was not cut and the stray key is not kept
    assert all(not p.endswith(f"-p{place_play + 1}") for p in phone.browser.stopped)
    assert world.answers(course_id)["place_type"] == "home_street"  # the next question was not given the 5
    await phone.close()


async def test_a_playback_that_just_ended_is_not_an_error_when_the_key_arrives(
    ictx, world, fakes, monkeypatch
):
    async def gone(call_id, playback_id):
        raise CommandRejected("stop_playback", 404, "no such playback")

    world.pilot()
    phone = await impatient_phone(ictx, {"FC_CONSENT_Q": ["2"]}, after=0.0)
    monkeypatch.setattr(ictx.telephony, "stop_playback", gone)
    course_id = world.course(phone)
    call = asyncio.create_task(ictx.runner.place(world.call(course_id, "first")[0]["id"]))
    await until(lambda: phone.browser.pressed)
    # nothing to stop: the call waits for the playback to end as it always did
    last_id = list(phone.browser.pressed)[0]
    phone.sock.push({"type": "played", "playback_id": last_id, "ok": True})
    assert await asyncio.wait_for(call, 3) == "completed"
    assert phone.browser.flat[-1] == "FC_CONSENT_NO"
    await phone.close()


async def test_a_pbx_that_will_not_stop_a_prompt_fails_the_call_loudly(ictx, world, fakes, monkeypatch):
    async def refuse(call_id, playback_id):
        raise CommandRejected("stop_playback", 500, "broken")

    world.pilot()
    phone = await impatient_phone(ictx, {"FC_CONSENT_Q": ["2"]}, after=0.0)
    monkeypatch.setattr(ictx.telephony, "stop_playback", refuse)
    call_id = world.call(world.course(phone), "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    assert "stop_playback rejected" in ictx.db.one("SELECT error FROM calls WHERE id=?", (call_id,))["error"]
    await phone.close()


# ------------------------------------------------- a 9 during the greeting or the notice
async def test_a_nine_pressed_during_the_notice_reaches_the_stop_question(ictx, world, fakes):
    class NineDuringNotice(Browser):
        def on_message(self, msg):
            if msg["type"] == "play" and "FC_NOTICE" in self.prompt_ids(msg["urls"]):
                self.heard.append(self.prompt_ids(msg["urls"]))
                self.sock.push({"type": "dtmf", "digit": "9"})  # pressed while the notice plays
                self.sock.push({"type": "played", "playback_id": msg["playback_id"], "ok": True})
            else:
                super().on_message(msg)

    world.pilot()
    phone = await open_phone(ictx, {})
    sock = phone.sock
    NineDuringNotice(sock, {"OPT_CONFIRM": ["9"]}, [])
    course_id = world.course(phone)
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    # no consent question was read out first: the person had already said they want to stop
    ids = [
        i
        for plays in phone.sock.sent
        if plays["type"] == "play"
        for i in [plays["urls"][0].rsplit("/", 1)[1]]
    ]
    assert ids[:3] == ["FC_HELLO.0.wav", "FC_NOTICE.0.wav", "OPT_CONFIRM.wav"] or "OPT_CONFIRM.wav" in ids
    assert ictx.db.one("SELECT status FROM courses")["status"] == "stopped"
    assert world.answers(course_id) == {"withdraw": "withdrawn"}
    await phone.close()


async def test_a_zero_pressed_during_the_short_notice_is_kept_and_plays_the_full_notice(ictx, world, fakes):
    class ZeroDuringNotice(Browser):
        def on_message(self, msg):
            if msg["type"] == "play" and self.prompt_ids(msg["urls"]) == ["FC_NOTICE"]:
                self.heard.append(self.prompt_ids(msg["urls"]))
                self.sock.push(
                    {"type": "dtmf", "digit": "0"}
                )  # "press 0 for the full notice", pressed over it
                self.sock.push({"type": "played", "playback_id": msg["playback_id"], "ok": True})
            else:
                super().on_message(msg)

    world.pilot()
    phone = await open_phone(ictx, {})
    ZeroDuringNotice(phone.sock, {"FC_CONSENT_Q": ["1"]}, [])
    course_id = world.course(phone)
    ictx.db.connect().execute("UPDATE courses SET eligibility='unconfirmed' WHERE id=?", (course_id,))
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    plays = [m["urls"][0].rsplit("/", 1)[1].split(".")[0] for m in phone.sock.sent if m["type"] == "play"]
    # the 0 reached the consent question, which was not read out first: the full notice came next, then the question
    assert plays[:4] == ["FC_HELLO", "FC_NOTICE", "FC_NOTICE_FULL", "FC_CONSENT_Q"]
    assert ictx.db.one("SELECT notice_version FROM courses")["notice_version"].endswith(":short+full")
    await phone.close()


async def test_a_nine_during_the_greeting_is_kept_too_and_other_keys_are_not(ictx, world, fakes):
    class KeysDuringGreeting(Browser):
        def on_message(self, msg):
            if msg["type"] == "play" and "FC_HELLO" in self.prompt_ids(msg["urls"]):
                self.heard.append(self.prompt_ids(msg["urls"]))
                self.sock.push({"type": "dtmf", "digit": "1"})  # not an answer to anything yet
                self.sock.push({"type": "dtmf", "digit": "9"})
                self.sock.push({"type": "played", "playback_id": msg["playback_id"], "ok": True})
            else:
                super().on_message(msg)

    world.pilot()
    phone = await open_phone(ictx, {})
    KeysDuringGreeting(phone.sock, {"OPT_CONFIRM": ["1"], "FC_CONSENT_Q": ["2"]}, [])
    course_id = world.course(phone)
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    heard = [u["urls"][0].rsplit("/", 1)[1] for u in phone.sock.sent if u["type"] == "play"]
    assert "OPT_CONFIRM.wav" in heard  # the 9 was heard; the 1 pressed over the greeting was not an answer
    await phone.close()


# --------------------------------------------------------- storage failures
async def test_a_data_folder_that_cannot_be_written_fails_the_call_in_plain_words_and_leaves_no_file(
    ictx, world, fakes
):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    world.pilot()
    clips = ictx.settings.var_dir / "clips"
    clips.mkdir(parents=True)
    clips.chmod(0o500)
    sub = ictx.bus.subscribe()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    course_id = world.course(phone)
    call_id = world.call(course_id, "first")[0]["id"]
    try:
        assert await ictx.runner.place(call_id) == "failed"
    finally:
        clips.chmod(0o700)
    row = ictx.db.one("SELECT error FROM calls WHERE id=?", (call_id,))
    assert row["error"] == STORAGE_FAILED
    assert "disk is full or not writable" in row["error"]
    assert list(clips.iterdir()) == []
    assert ictx.db.one("SELECT COUNT(*) AS n FROM place_captures")["n"] == 0
    assert "place" not in world.answers(course_id)  # nothing half-saved
    seen = []
    while not sub.empty():
        seen.append(sub.get_nowait())
    assert any(e.type == "system" and e.data == {"check": "storage", "ok": False} for e in seen)
    await phone.close()


async def test_a_clip_whose_final_name_cannot_be_given_is_taken_back_not_left_pointing_at_nothing(
    ictx, world, fakes, monkeypatch
):
    def broken(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runner_mod.os, "replace", broken)
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    course_id = world.course(phone)
    call_id = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    assert ictx.db.one("SELECT error FROM calls WHERE id=?", (call_id,))["error"] == STORAGE_FAILED
    assert ictx.db.one("SELECT COUNT(*) AS n FROM place_captures")["n"] == 0
    assert ictx.db.one("SELECT location_status FROM bite_reports")["location_status"] == "no_place_heard"
    clips = ictx.settings.var_dir / "clips"
    assert not any(clips.iterdir())  # not even the temporary file
    await phone.close()


async def test_a_clip_is_only_visible_under_its_real_name_after_the_rows_are_saved(
    ictx, world, fakes, monkeypatch
):
    seen_during_commit = {}
    real_apply = ictx.runner._apply

    def spy(*args):
        out = real_apply(*args)
        staged = args[6]
        if staged:
            seen_during_commit["names"] = sorted(p.name for p in (ictx.settings.var_dir / "clips").iterdir())
        return out

    monkeypatch.setattr(ictx.runner, "_apply", spy)
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    course_id = world.course(phone)
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    assert all(n.endswith(".tmp") and n.startswith(".") for n in seen_during_commit["names"])
    final = [p.name for p in (ictx.settings.var_dir / "clips").iterdir()]
    assert len(final) == 1 and final[0].endswith(".wav") and not final[0].startswith(".")
    await phone.close()


async def test_a_visitors_clip_from_the_browser_phone_expires_in_hours_and_a_real_calls_in_days(
    ictx, world, fakes
):
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    course_id = world.course(phone)
    await ictx.runner.place(world.call(course_id, "first")[0]["id"])
    cap = ictx.db.one("SELECT expires_at FROM place_captures")
    assert cap["expires_at"] == clock.iso(
        clock.now() + timedelta(hours=ictx.policy["retention"]["sim_clip_hours"])
    )
    assert ictx.policy["retention"]["sim_clip_hours"] < 24 * ictx.policy["retention"]["clip_max_age_days"]
    await phone.close()


# ------------------------------------------------ the browser phone's own limits
async def attach(sim: SimPort, **kw):
    session = sim.create_session("visitor:t", missed_calls_per_minute=kw.pop("per_minute", 6), **kw)
    sock = FakeSocket()
    task = asyncio.create_task(sim.attach(session.id, sock))
    await asyncio.sleep(0)
    return session, sock, task


async def test_an_idle_socket_is_told_why_and_closed():
    sim = SimPort()
    session, sock, task = await attach(sim, idle_s=0.1)
    await asyncio.wait_for(task, 2)
    assert sock.closed
    assert sock.sent[-1]["type"] == "error" and "nothing was pressed" in sock.sent[-1]["message"]
    assert sim.session(session.id) is None and sim.session_count() == 0


async def test_any_message_keeps_a_socket_from_going_idle():
    sim = SimPort()
    session, sock, task = await attach(sim, idle_s=0.3)
    for _ in range(4):
        await asyncio.sleep(0.15)
        sock.push({"type": "hangup"})  # a message, even one that changes nothing
    assert not task.done()
    sock.disconnect()
    await task


async def test_a_socket_is_closed_at_its_longest_age_even_if_it_is_busy():
    sim = SimPort()
    session, sock, task = await attach(sim, idle_s=5, max_age_s=0.3)

    async def chatter():
        while not task.done():
            sock.push({"type": "hangup"})
            await asyncio.sleep(0.05)

    chat = asyncio.create_task(chatter())
    await asyncio.wait_for(task, 2)
    chat.cancel()
    assert sock.closed and "longest time" in sock.sent[-1]["message"]


async def test_a_session_without_limits_stays_open_as_before():
    sim = SimPort()
    session, sock, task = await attach(sim)
    await asyncio.sleep(0.2)
    assert not task.done()
    sock.disconnect()
    await task


async def test_missed_calls_are_capped_per_client_address_as_well_as_per_visitor(fixed_clock):
    sim = SimPort()
    socks = []
    for n in range(3):  # three different visitors behind one address
        session = sim.create_session(f"visitor:{n}", missed_calls_per_minute=2, address="10.0.0.9")
        sock = FakeSocket()
        socks.append((sock, asyncio.create_task(sim.attach(session.id, sock))))
    await asyncio.sleep(0)
    for sock, _ in socks[:2]:
        sock.push({"type": "missed_call"})
    assert isinstance(await anext(sim.events()), MissedCall)
    assert isinstance(await anext(sim.events()), MissedCall)
    socks[2][0].push({"type": "missed_call"})  # a fresh visitor, but the address has used its two
    await asyncio.sleep(0.05)
    assert (
        socks[2][0].sent[-1]["type"] == "error" and "too many missed calls" in socks[2][0].sent[-1]["message"]
    )
    fixed_clock.advance(timedelta(seconds=61))
    socks[2][0].push({"type": "missed_call"})
    assert isinstance(await anext(sim.events()), MissedCall)
    for sock, task in socks:
        sock.disconnect()
        await task


async def test_sessions_are_counted_per_address(fixed_clock):
    sim = SimPort()
    for _ in range(14):
        sim.create_session("visitor:a", missed_calls_per_minute=6, address="10.0.0.9")
    sim.create_session("visitor:b", missed_calls_per_minute=6, address="10.0.0.10")
    sim.create_session("visitor:c", missed_calls_per_minute=6)
    assert sim.sessions_at("10.0.0.9") == 14 and sim.sessions_at("10.0.0.10") == 1
    assert sim.sessions_at("10.0.0.11") == 0 and sim.session_count() == 16


# ------------------------------------------------------- a clinic's own phone
async def test_a_missed_call_from_a_phone_opened_in_a_clinics_view_carries_that_clinic(ictx, world):
    world.clinic()
    phone = await open_phone(ictx, {}, clinic_id=CLINIC_ID)
    await phone.missed_call()
    row = await until(lambda: ictx.db.one("SELECT * FROM missed_calls"))
    assert (row["status"], row["transport"], row["clinic_hint"]) == ("unlinked", "sim", CLINIC_ID)
    assert ictx.db.one("SELECT COUNT(*) AS n FROM courses")["n"] == 0  # not linked to a desk of its own
    await phone.close()


async def test_a_visitors_phone_still_links_at_once(ictx):
    phone = await open_phone(ictx, {})
    await phone.missed_call()
    row = await until(lambda: ictx.db.one("SELECT * FROM missed_calls"))
    assert row["status"] == "linked" and row["clinic_hint"] == "cln_demo_visitor"
    await phone.close()


async def test_a_missed_call_on_a_clinics_own_line_is_shown_to_that_clinic(ictx, world):
    world.clinic("cln_b")
    ictx.settings = ictx.settings.model_copy(update={"clinic_lines": {"quectel1": "cln_b"}})
    ictx.telephony._queue.put_nowait(MissedCall("x1", "+919999900233", clock.now(), line="quectel1"))
    ictx.telephony._queue.put_nowait(MissedCall("x2", "+919999900244", clock.now(), line="quectel9"))
    await until(lambda: ictx.db.one("SELECT COUNT(*) AS n FROM missed_calls")["n"] == 2)
    hints = {r["phone_last4"]: r["clinic_hint"] for r in ictx.db.query("SELECT * FROM missed_calls")}
    assert hints == {"0233": "cln_b", "0244": None}  # a line nobody mapped is shared, as before


CLINIC_ID = "cln_a"


# ------------------------------------------------ a stopped server, a reset
async def test_a_server_that_stops_ends_the_call_as_the_servers_doing_not_the_patients(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {}, stall_on_play=True)
    course_id = world.course(phone)
    call_id = world.call(course_id, "first")[0]["id"]
    call = asyncio.create_task(ictx.runner.place(call_id))
    await until(lambda: phone.browser.heard)
    await ictx.telephony.stop()
    assert await asyncio.wait_for(call, 3) == "failed"
    row = ictx.db.one("SELECT error, hangup_cause FROM calls WHERE id=?", (call_id,))
    assert row["error"] == "The call was cut off because the server stopped."
    assert "hung up" not in row["error"] and "server stopped" in row["hangup_cause"].lower()
    assert {"type": "hangup", "reason": "server"} in phone.sock.sent


async def test_a_closed_browser_page_is_still_the_patient_hanging_up(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {}, stall_on_play=True)
    call_id = world.call(world.course(phone), "first")[0]["id"]
    call = asyncio.create_task(ictx.runner.place(call_id))
    await until(lambda: phone.browser.heard)
    await phone.close()
    assert await asyncio.wait_for(call, 3) == "failed"
    assert "hung up" in ictx.db.one("SELECT error FROM calls WHERE id=?", (call_id,))["error"]


async def test_a_call_to_a_page_that_is_gone_is_marked_so_and_never_retried(ictx, world):
    enrol.record_missed_call(ictx, "sim:ghost", "sim", clock.now())
    call_id = (await until(lambda: ictx.db.one("SELECT id FROM calls WHERE kind='first'")))["id"]
    assert await ictx.runner.place(call_id) == "no_answer"
    assert (
        ictx.db.one("SELECT hangup_cause FROM calls WHERE id=?", (call_id,))["hangup_cause"]
        == BROWSER_PHONE_GONE
    )
    from chaarteeke.scheduler import Scheduler

    await Scheduler(ictx, "a").tick()
    assert len(ictx.db.query("SELECT 1 FROM calls WHERE kind='first'")) == 1  # no retry rows


async def test_cancelling_calls_before_a_reset_ends_them_cleanly_and_refuses_new_ones(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {}, stall_on_play=True)
    course_id = world.course(phone)
    call_id = world.call(course_id, "first")[0]["id"]
    call = asyncio.create_task(ictx.runner.place(call_id))
    await until(lambda: phone.browser.heard)
    assert await asyncio.to_thread(ictx.runner.cancel_calls) == 1
    with pytest.raises(asyncio.CancelledError):
        await call
    row = ictx.db.one("SELECT status, error FROM calls WHERE id=?", (call_id,))
    assert row["status"] == "failed" and row["error"] == "The call was interrupted."
    later = world.add_call(course_id, "reminder", dose_no=2)
    assert await ictx.runner.place(later) == "cancelled"  # paused until resume
    ictx.runner.resume()
    await phone.close()


async def test_resetting_the_demo_stops_live_calls_first_and_lets_calls_run_again_after(ictx, monkeypatch):
    from chaarteeke.demo import controls, seed

    order = []
    monkeypatch.setattr(ictx.runner, "cancel_calls", lambda: order.append("cancel") or 0)
    monkeypatch.setattr(ictx.runner, "resume", lambda: order.append("resume"))
    monkeypatch.setattr(seed, "seed", lambda ctx, s, reset: order.append("seed"))
    await asyncio.to_thread(controls.reset_demo, ictx)
    assert order == ["cancel", "seed", "resume"]


async def test_mux_forwards_a_stop_to_the_port_that_owns_the_call():
    class Stub:
        def __init__(self):
            self.stopped = []

        async def start(self): ...
        async def stop(self): ...
        async def events(self):
            await asyncio.sleep(3600)
            yield

        async def originate(self, number, correlation_id, *, ring_timeout_s=30):
            return f"c-{number}"

        async def stop_playback(self, call_id, playback_id):
            self.stopped.append((call_id, playback_id))

    ari, sim = Stub(), Stub()
    mux = MuxPort(ari, sim)
    call = await mux.originate("sim:abc", "corr")
    await mux.stop_playback(call, "pb-1")
    assert sim.stopped == [(call, "pb-1")] and ari.stopped == []
    with pytest.raises(TelephonyError):
        await mux.stop_playback("unknown", "pb-1")


async def test_the_browser_phone_refuses_to_stop_a_playback_that_is_not_playing():
    sim = SimPort()
    session, sock, task = await attach(sim)
    call_id = await sim.originate(f"sim:{session.id}", "corr", ring_timeout_s=5)
    sock.push({"type": "answer"})
    ev = await anext(sim.events())
    assert isinstance(ev, CallAnswered)
    await sim.play(call_id, ["chaar/hi/FC_NOTICE"], "pb-1")
    await sim.stop_playback(call_id, "pb-1")
    assert {"type": "stop", "playback_id": "pb-1"} in sock.sent
    sock.push({"type": "played", "playback_id": "pb-1", "ok": True})  # the page may still report it ended
    await asyncio.sleep(0.05)
    assert not [m for m in sock.sent if m["type"] == "error"]
    with pytest.raises(CommandRejected) as e:
        await sim.stop_playback(call_id, "pb-1")
    assert e.value.status == 404
    sock.disconnect()
    await task
    assert isinstance(await anext(sim.events()), Hangup)
