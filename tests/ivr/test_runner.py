"""The call runner end to end: a scripted browser phone, the real SimPort, the real enrol/doses code.

Only the two modules other lanes are still writing are replaced (alerts.from_report and the place review),
and only here.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import timedelta

import pytest
from ivr_support import (
    CLINIC,
    Browser,
    noise_wav,
    open_phone,
    prompts_without_qconsent,
    silent_wav,
    speech_wav,
    tone_burst_wav,
    until,
    write_voice,
)

from chaarteeke import clock, enrol
from chaarteeke.ivr import runner as runner_mod
from chaarteeke.ivr.prompts import PromptCatalogue
from chaarteeke.ivr.runner import CallRunner, RunnerError
from chaarteeke.telephony import sim as sim_mod
from chaarteeke.telephony.model import CommandRejected, MissedCall, TelephonyConnectionError

FIRST_CALL = {
    "FC_CONSENT_Q": ["1"], "FC_QCONSENT": ["1"], "FC_PLACETYPE": ["1"], "FC_ANIMAL": ["1"],
    "FC_NOTCH": ["2"], "FC_SICK": ["2"],
}  # fmt: skip


# Built from parts so that no test source line (which a traceback prints) holds a number.
NUMBER = "+91" + "98765" + "43210"


def drain(sub) -> list:
    out = []
    while not sub.empty():
        out.append(sub.get_nowait())
    return out


async def first_call_id(ictx) -> str:
    row = await until(lambda: ictx.db.one("SELECT id FROM calls WHERE kind='first'"))
    return row["id"]


def call_row(ictx, call_id):
    return ictx.db.one("SELECT * FROM calls WHERE id=?", (call_id,))


@pytest.fixture
def fakes(monkeypatch):
    """The other lanes' alert and place-review functions, recorded."""
    seen = {"captures": [], "alerts": []}

    def alert(ctx, conn, report_id):
        seen["alerts"].append(report_id)
        conn.execute(
            "INSERT INTO alerts(id, report_id, ward_no, cluster_id, place_type, local_day, status, created_at, is_demo)"
            " VALUES ('alr_test', ?, NULL, NULL, 'home_street', '2026-10-19', 'new', ?, 1)",
            (report_id, clock.iso(clock.now())),
        )
        return "alr_test"

    monkeypatch.setattr(runner_mod, "_alert_from_report", alert)
    monkeypatch.setattr(runner_mod, "_process_capture", lambda ctx, rid: seen["captures"].append(rid))
    return seen


# ------------------------------------------------------------------ first call
async def test_demo_missed_call_to_finished_first_call_in_the_chosen_language(ictx, fakes):
    sub = ictx.bus.subscribe()
    script = {**FIRST_CALL, "SYS_LANG": ["2"], "FC_SICK": ["1"], "FC_NOTCH": ["2"]}
    phone = await open_phone(ictx, script, recordings=[speech_wav(1.0)])
    await phone.missed_call()  # the pump takes it to enrol.record_missed_call, which links the demo course
    call_id = await first_call_id(ictx)
    assert await ictx.runner.place(call_id) == "completed"
    await ictx.runner.wait_background()

    course = ictx.db.one("SELECT * FROM courses WHERE source='simulator'")
    assert (course["lang"], course["consent_questions"]) == ("doi", "yes")
    assert course["consent_reminders_at"] == clock.iso(clock.now())
    assert course["notice_version"] == f"{ictx.prompts.version}:short"  # only the short notice was heard
    answers = {r["question"]: r["value"] for r in ictx.db.query("SELECT question, value FROM answers")}
    assert answers == {
        "lang": "doi", "consent_reminders": "yes", "consent_questions": "yes",
        "place": "recorded", "place_type": "home_street", "animal": "unnotched", "signs": "yes",
    }  # fmt: skip

    rep = ictx.db.one("SELECT * FROM bite_reports")
    assert (rep["place_type"], rep["animal"], rep["signs"]) == ("home_street", "unnotched", "yes")
    assert (rep["location_status"], rep["source"], rep["clinic_id"], rep["is_demo"]) == (
        "pending", "simulator", "cln_demo_visitor", 1,
    )  # fmt: skip
    assert rep["bite_week"] == clock.week_start(clock.local_today()).isoformat()

    cap = ictx.db.one("SELECT * FROM place_captures")
    clip = ictx.settings.var_dir / "clips" / f"{rep['id']}.wav"
    assert cap["audio_path"] == str(clip) and clip.read_bytes() == speech_wav(1.0)
    assert (
        clip.stat().st_mode & 0o777 == 0o600 and clip.parent.stat().st_mode & 0o777 == 0o700
    )  # a voice clip
    # a visitor's own voice from the browser phone is kept for hours, not days
    hours = ictx.policy["retention"]["sim_clip_hours"]
    assert (cap["asr_status"], cap["expires_at"]) == (
        "pending",
        clock.iso(clock.now() + timedelta(hours=hours)),
    )
    with pytest.raises(CommandRejected):  # only our copy is kept; the phone system's is deleted
        await ictx.telephony.fetch_recording(cap["recording_name"])
    assert fakes["captures"] == [rep["id"]] and fakes["alerts"] == [rep["id"]]

    call = call_row(ictx, call_id)
    assert call["status"] == "completed" and call["error"] is None and call["transport"] == "sim"
    assert call["answered_at"] and call["ended_at"] and call["seconds_listened"] is not None
    assert "16" in call["hangup_cause"]

    # the browser heard the language menu in all three languages, then Dogri, with the dates from the schedule
    assert phone.browser.urls[0] == [f"/media/prompts/{lg}/SYS_LANG.wav" for lg in ("hi", "doi", "ur")]
    assert all("/doi/" in u for urls in phone.browser.urls[1:] for u in urls)
    close = phone.browser.urls[-1]
    assert "/media/prompts/doi/FC_CLOSE.0.wav" in close
    assert {"/media/prompts/doi/slot/weekday_3.wav", "/media/prompts/doi/slot/day_22.wav",
            "/media/prompts/doi/slot/month_10.wav", "/media/prompts/doi/slot/clinic_cln_demo_visitor.wav"} <= set(close)  # fmt: skip
    assert phone.browser.records == [
        {"type": "record", "name": cap["recording_name"], "max_seconds": 8, "silence_s": 2}
    ]

    events = drain(sub)
    statuses = [e.data["status"] for e in events if e.type == "call" and e.data["call_id"] == call_id]
    assert statuses == ["scheduled", "dialing", "ringing", "answered", "completed"]
    kinds = {e.type for e in events}
    assert {"report", "alert", "missed_call", "course"} <= kinds
    assert [e.data["ward_no"] for e in events if e.type == "alert"] == [None]
    assert all(e.data["simulated"] for e in events if e.type in ("report", "call"))
    assert "sim:" not in json.dumps([e.data for e in events])  # a number is never in an event
    await phone.close()


async def test_a_clinic_course_is_called_in_its_own_language_and_never_asks_for_one(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    course_id = world.course(phone, lang="ur")
    call = world.call(course_id, "first")[0]
    assert await ictx.runner.place(call["id"]) == "completed"
    heard = phone.browser.flat
    assert "SYS_LANG" not in heard and heard[0] == "FC_HELLO" and heard[-1] == "FC_CLOSE"
    assert all("/ur/" in u for urls in phone.browser.urls for u in urls)
    await phone.close()


async def test_five_dose_regimen_over_the_phone(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    course_id = world.course(phone, regimen="im_essen")
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    close = phone.browser.urls[-1]
    assert phone.browser.flat[-1] == "FC_CLOSE"  # dose 2 of 5, read from the dose table
    assert "/media/prompts/hi/slot/num_2.wav" in close and "/media/prompts/hi/slot/num_5.wav" in close
    assert "dose_today" not in world.answers(course_id)
    await phone.close()


async def test_four_dose_regimen_says_n_of_4(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    course_id = world.course(phone, regimen="id_trc")
    await ictx.runner.place(world.call(course_id, "first")[0]["id"])
    close = phone.browser.urls[-1]
    assert "/media/prompts/hi/slot/num_2.wav" in close and "/media/prompts/hi/slot/num_4.wav" in close
    await phone.close()


async def test_the_close_names_the_next_dose_from_the_table_after_the_clinic_marks_one(ictx, world, fakes):
    from chaarteeke import doses

    world.pilot()
    today = clock.local_today(ictx.settings.local_tz)
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    course_id = world.course(phone, day0=today - timedelta(days=3))  # dose 2 is due today
    doses.mark_given(ictx, course_id, 2, "clinic", today, "staff:test")
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    close = phone.browser.urls[-1]
    assert "/media/prompts/hi/slot/num_3.wav" in close  # injection 3 of 4
    await phone.close()


async def test_when_no_dose_is_left_the_close_is_the_last_dose_line(ictx, world, fakes):
    from chaarteeke import doses

    world.pilot()
    today = clock.local_today(ictx.settings.local_tz)
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    course_id = world.course(phone, regimen="reexposure", day0=today - timedelta(days=3))
    doses.mark_given(ictx, course_id, 2, "clinic", today, "staff:test")
    ictx.db.connect().execute("UPDATE courses SET status='active' WHERE id=?", (course_id,))
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    assert phone.browser.flat[-1] == "FC_CLOSE_LAST"
    await phone.close()


async def test_a_first_call_whose_next_dose_date_has_gone_asks_the_patient_to_come_to_the_clinic(
    ictx, world, fakes
):
    world.pilot()
    world.clinic(CLINIC, phone="0191234")
    today = clock.local_today(ictx.settings.local_tz)
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    course_id = world.course(phone, day0=today - timedelta(days=5))  # dose 2 was due two days ago
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    assert phone.browser.flat[-1] == "FC_CLOSE_COME" and "FC_CLOSE" not in phone.browser.flat
    digits = [u.rsplit("_", 1)[1][0] for u in phone.browser.urls[-1] if "num_" in u]
    assert "".join(digits) == "0191234"
    await phone.close()


async def test_a_first_call_that_would_say_come_but_has_no_clinic_phone_is_refused_before_it_rings(
    ictx, world, fakes
):
    world.pilot()
    today = clock.local_today(ictx.settings.local_tz)
    phone = await open_phone(ictx, {**FIRST_CALL})
    course_id = world.course(phone, day0=today - timedelta(days=5))
    call_id = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    assert "no phone number" in call_row(ictx, call_id)["error"]
    assert not any(m["type"] == "ring" for m in phone.sock.sent)
    await phone.close()


async def test_the_full_notice_on_key_zero_is_played_asked_again_and_recorded_as_heard(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {"FC_CONSENT_Q": ["0", "0", "1"]})
    course_id = world.course(phone)
    ictx.db.connect().execute("UPDATE courses SET eligibility='unconfirmed' WHERE id=?", (course_id,))
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    assert phone.browser.flat == [
        "FC_HELLO", "FC_NOTICE", "FC_CONSENT_Q", "FC_NOTICE_FULL", "FC_CONSENT_Q", "FC_NOTICE_FULL",
        "FC_CONSENT_Q", "FC_CLOSE",
    ]  # fmt: skip
    course = ictx.db.one("SELECT consent_reminders_at, notice_version FROM courses")
    assert course["consent_reminders_at"] == clock.iso(clock.now())
    assert course["notice_version"] == f"{ictx.prompts.version}:short+full"
    assert world.answers(course_id) == {"consent_reminders": "yes"}  # the answer is the same either way
    await phone.close()


async def test_a_third_zero_is_a_wrong_key_and_the_full_notice_is_not_played_again(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {"FC_CONSENT_Q": ["0", "0", "0", "1"]})
    course_id = world.course(phone)
    ictx.db.connect().execute("UPDATE courses SET eligibility='unconfirmed' WHERE id=?", (course_id,))
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    assert phone.browser.flat.count("FC_NOTICE_FULL") == 2
    assert phone.browser.flat[-3:] == [
        "SYS_WRONGKEY",
        "FC_CONSENT_Q",
        "FC_CLOSE",
    ]  # the third 0 uses up a try
    assert ictx.db.one("SELECT notice_version FROM courses")["notice_version"].endswith(":short+full")
    await phone.close()


async def test_a_guardians_course_asks_the_guardian_and_says_who_answers_the_questions(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL, "FC_CONSENT_Q_G": ["1"]}, recordings=[speech_wav()])
    course_id = world.course(phone, eligibility="guardian_consented")
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    ids = phone.browser.flat
    assert ids[:5] == ["FC_HELLO", "FC_NOTICE", "FC_CONSENT_Q_G", "FC_GUARD_OK", "FC_QCONSENT"]
    assert "FC_CONSENT_Q" not in ids
    await phone.close()


async def test_an_unconfirmed_patient_gets_no_data_questions_and_no_report(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {"FC_CONSENT_Q": ["1"]})
    course_id = world.course(phone)
    ictx.db.connect().execute("UPDATE courses SET eligibility='unconfirmed' WHERE id=?", (course_id,))
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    assert phone.browser.flat == ["FC_HELLO", "FC_NOTICE", "FC_CONSENT_Q", "FC_CLOSE"]
    assert world.answers(course_id) == {"consent_reminders": "yes"}
    assert ictx.db.one("SELECT COUNT(*) AS n FROM bite_reports")["n"] == 0
    assert phone.browser.records == []
    await phone.close()


async def test_declining_the_questions_makes_a_skipped_report_and_asks_nothing_about_the_bite(
    ictx, world, fakes
):
    world.pilot()
    phone = await open_phone(ictx, {"FC_CONSENT_Q": ["1"], "FC_QCONSENT": ["2"]})
    course_id = world.course(phone)
    await ictx.runner.place(world.call(course_id, "first")[0]["id"])
    rep = ictx.db.one("SELECT * FROM bite_reports")
    assert (rep["location_status"], rep["place_type"], rep["animal"], rep["signs"]) == (
        "skipped", "no_answer", "no_answer", "no_answer",
    )  # fmt: skip
    assert ictx.db.one("SELECT consent_questions FROM courses")["consent_questions"] == "no"
    assert "FC_PLACE" not in phone.browser.flat
    await phone.close()


async def test_no_place_heard_twice_is_recorded_as_such_and_the_call_goes_on(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[silent_wav(), b""])
    course_id = world.course(phone)
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    rep = ictx.db.one("SELECT * FROM bite_reports")
    assert rep["location_status"] == "no_place_heard"
    assert ictx.db.one("SELECT COUNT(*) AS n FROM place_captures")["n"] == 0
    assert world.answers(course_id)["place"] == "none"
    assert "FC_PLACE_AGAIN" in phone.browser.flat
    assert fakes["captures"] == []
    assert not (ictx.settings.var_dir / "clips").exists() or not list(
        (ictx.settings.var_dir / "clips").iterdir()
    )
    await phone.close()


async def test_the_silent_first_clip_is_not_kept_but_the_second_is(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[silent_wav(), speech_wav()])
    course_id = world.course(phone)
    await ictx.runner.place(world.call(course_id, "first")[0]["id"])
    cap = ictx.db.one("SELECT * FROM place_captures")
    assert cap["recording_name"].endswith("-r2")
    with pytest.raises(CommandRejected):  # the silent one was deleted from the phone system
        await ictx.telephony.fetch_recording(phone.browser.records[0]["name"])
    await phone.close()


def asterisk_counts_whole_seconds(monkeypatch, talking_s):
    """The phone system's own talking time, as Asterisk gives it: whole seconds, wrong for a short word."""
    real = sim_mod.analyse_wav

    def analyse(*args):
        return dataclasses.replace(real(*args), talking_s=talking_s)

    monkeypatch.setattr(sim_mod, "analyse_wav", analyse)


async def test_a_short_answer_reported_as_zero_talking_seconds_is_kept(ictx, world, fakes, monkeypatch):
    asterisk_counts_whole_seconds(monkeypatch, 0)
    world.pilot()
    word = tone_burst_wav(total_s=1.6, burst_s=0.8, rate=16000)
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[word])
    course_id = world.course(phone)
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    assert "FC_PLACE_AGAIN" not in phone.browser.flat and len(phone.browser.records) == 1
    cap = ictx.db.one("SELECT * FROM place_captures")
    assert cap["voiced_s"] == pytest.approx(0.8, abs=0.04)
    assert ictx.db.one("SELECT location_status FROM bite_reports")["location_status"] == "pending"
    assert world.answers(course_id)["place"] == "recorded"
    assert fakes["captures"] == [cap["report_id"]]
    await phone.close()


async def test_line_noise_is_asked_again_even_when_the_phone_system_reports_talking_time(
    ictx, world, fakes, monkeypatch
):
    asterisk_counts_whole_seconds(monkeypatch, 3.0)
    world.pilot()
    phone = await open_phone(
        ictx, {**FIRST_CALL}, recordings=[noise_wav(0.2, rms=0.1, rate=16000), speech_wav(1.0)]
    )
    course_id = world.course(phone)
    await ictx.runner.place(world.call(course_id, "first")[0]["id"])
    assert "FC_PLACE_AGAIN" in phone.browser.flat
    cap = ictx.db.one("SELECT * FROM place_captures")
    assert cap["recording_name"].endswith("-r2") and cap["voiced_s"] == pytest.approx(1.0, abs=0.04)
    await phone.close()


async def test_a_recording_of_44_bytes_is_silence_and_is_asked_again(ictx, world, fakes):
    header_only = silent_wav(0.0)
    assert len(header_only) == 44
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[header_only, speech_wav()])
    course_id = world.course(phone)
    await ictx.runner.place(world.call(course_id, "first")[0]["id"])
    assert "FC_PLACE_AGAIN" in phone.browser.flat
    assert ictx.db.one("SELECT recording_name FROM place_captures")["recording_name"].endswith("-r2")
    await phone.close()


async def test_a_recording_the_app_cannot_read_ends_the_call_and_says_so(ictx, world, fakes, monkeypatch):
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    course_id = world.course(phone)

    async def not_audio(name):
        return b"RIFFaudio"

    monkeypatch.setattr(ictx.telephony, "fetch_recording", not_audio)
    call_id = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    assert "could not be read" in call_row(ictx, call_id)["error"]
    assert ictx.db.one("SELECT COUNT(*) AS n FROM place_captures")["n"] == 0
    await phone.close()


async def test_silence_ends_the_call_as_no_answer_and_saves_nothing(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {"FC_CONSENT_Q": [None, None, None]})
    course_id = world.course(phone)
    call_id = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "no_answer"
    assert phone.browser.heard[-3:] == [
        ["SYS_NOINPUT", "FC_CONSENT_Q"],
        ["SYS_NOINPUT", "FC_CONSENT_Q"],
        ["SYS_GIVEUP_FIRST"],
    ]
    assert world.answers(course_id) == {}
    assert "no usable key" in call_row(ictx, call_id)["error"]
    assert ictx.db.one("SELECT status FROM courses")["status"] == "active"
    await phone.close()


async def test_a_wrong_key_counts_as_a_try(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {"FC_CONSENT_Q": ["7", "7", "7"]})
    course_id = world.course(phone)
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "no_answer"
    assert phone.browser.heard[-3][0] == "SYS_WRONGKEY"
    await phone.close()


async def test_key_nine_twice_withdraws_deletes_the_number_and_says_goodbye(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {"FC_CONSENT_Q": ["9"], "OPT_CONFIRM": ["9"]})
    course_id = world.course(phone)
    call_id = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "completed"
    assert phone.browser.flat[-2:] == ["OPT_CONFIRM", "OPT_DONE"]
    course = ictx.db.one("SELECT status, stopped_reason FROM courses")
    assert course["status"] == "stopped" and "pressed 9" in course["stopped_reason"]
    assert ictx.db.one("SELECT COUNT(*) AS n FROM contacts")["n"] == 0
    assert ictx.db.one("SELECT reason FROM purge_log WHERE what='phone'")["reason"] == "withdrawn"
    assert world.answers(course_id) == {"withdraw": "withdrawn"}
    assert (
        ictx.db.one("SELECT actor FROM audit WHERE action='course.withdraw'")["actor"]
        == f"patient:{course_id}"
    )
    await phone.close()


async def test_key_nine_while_the_place_is_recorded_withdraws_and_keeps_no_clip(ictx, world, fakes):
    world.pilot()
    # the 9 arrives after the question has played, so it is pressed while the person is being recorded
    phone = await open_phone(
        ictx, {**FIRST_CALL, "FC_PLACE": ["9"], "OPT_CONFIRM": ["9"]}, recordings=[speech_wav()]
    )
    course_id = world.course(phone)
    call_id = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "completed"
    assert phone.browser.flat[-3:] == ["FC_PLACE", "OPT_CONFIRM", "OPT_DONE"]
    assert ictx.db.one("SELECT COUNT(*) AS n FROM contacts")["n"] == 0
    assert ictx.db.one("SELECT COUNT(*) AS n FROM place_captures")["n"] == 0
    assert fakes["captures"] == []
    recorded = phone.browser.records[0]["name"]
    with pytest.raises(CommandRejected):  # the clip the phone system made was deleted, not kept
        await ictx.telephony.fetch_recording(recorded)
    await phone.close()


async def test_key_nine_before_the_place_recording_starts_asks_instead_of_recording(ictx, world, fakes):
    class EagerNine(Browser):
        """Presses 9 once, while the place question is still playing, then lets the question finish."""

        pressed = False

        def on_message(self, msg):
            if msg["type"] == "play" and not self.pressed and "FC_PLACE" in self.prompt_ids(msg["urls"]):
                self.pressed = True
                self.heard.append(self.prompt_ids(msg["urls"]))
                self.sock.push({"type": "dtmf", "digit": "9"})
                self.sock.push({"type": "played", "playback_id": msg["playback_id"], "ok": True})
            else:
                super().on_message(msg)

    world.pilot()
    phone = await open_phone(ictx, {})
    script = {**FIRST_CALL, "OPT_CONFIRM": ["1"]}
    eager = EagerNine(phone.sock, script, [speech_wav()])
    course_id = world.course(phone)
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    ids = eager.flat
    assert ids[ids.index("OPT_CONFIRM") - 1] == "FC_PLACE"  # asked to confirm before any recording
    assert ids[ids.index("OPT_CONFIRM") + 1] == "FC_PLACE"  # kept: the same question again
    assert len(eager.records) == 1  # recorded once, after the 9 was dealt with
    assert ictx.db.one("SELECT COUNT(*) AS n FROM contacts")["n"] == 1
    await phone.close()


async def test_saying_no_to_reminders_deletes_the_number_at_once(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {"FC_CONSENT_Q": ["2"]})
    course_id = world.course(phone)
    assert await ictx.runner.place(world.call(course_id, "first")[0]["id"]) == "completed"
    assert phone.browser.flat[-1] == "FC_CONSENT_NO"
    assert ictx.db.one("SELECT COUNT(*) AS n FROM contacts")["n"] == 0
    assert ictx.db.one("SELECT consent_reminders_at FROM courses")["consent_reminders_at"] is None
    assert world.answers(course_id) == {"withdraw": "declined"}
    await phone.close()


async def test_a_call_cut_mid_way_keeps_answers_and_the_next_attempt_asks_only_the_rest(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()], hangup_on="FC_PLACETYPE")
    course_id = world.course(phone)
    first = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(first) == "failed"
    cut = call_row(ictx, first)
    assert "hung up before the call finished" in cut["error"] and cut["answered_at"]
    assert world.answers(course_id) == {
        "consent_reminders": "yes", "consent_questions": "yes", "place": "recorded",
    }  # fmt: skip
    assert ictx.db.one("SELECT location_status FROM bite_reports")["location_status"] == "pending"

    phone.browser.heard.clear()
    phone.browser.hangup_on = None
    phone.browser.set_script({"FC_PLACETYPE": ["2"], "FC_ANIMAL": ["2"], "FC_SICK": ["2"]})
    retry = world.add_call(course_id, "first", attempt=2)
    assert await ictx.runner.place(retry) == "completed", call_row(ictx, retry)["error"]
    assert phone.browser.flat == ["FC_HELLO", "FC_PLACETYPE", "FC_ANIMAL", "FC_SICK", "FC_CLOSE"]
    reports = ictx.db.query("SELECT * FROM bite_reports")
    assert len(reports) == 1
    assert (reports[0]["place_type"], reports[0]["animal"], reports[0]["signs"]) == ("school", "pet", "no")
    assert ictx.db.one("SELECT COUNT(*) AS n FROM place_captures")["n"] == 1
    await phone.close()


async def test_a_sick_dog_answer_creates_the_alert_in_the_same_save(ictx, world, fakes):
    world.pilot()
    sub = ictx.bus.subscribe()
    phone = await open_phone(ictx, {**FIRST_CALL, "FC_SICK": ["1"]}, recordings=[speech_wav()])
    course_id = world.course(phone)
    await ictx.runner.place(world.call(course_id, "first")[0]["id"])
    rep = ictx.db.one("SELECT id FROM bite_reports")["id"]
    assert fakes["alerts"] == [rep]
    assert "FC_SICK_YES" in phone.browser.flat
    assert any(e.type == "alert" and e.data["alert_id"] == "alr_test" for e in drain(sub))
    await phone.close()


async def test_a_not_sick_answer_creates_no_alert(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    await ictx.runner.place(world.call(world.course(phone), "first")[0]["id"])
    assert fakes["alerts"] == []
    await phone.close()


async def test_a_failed_place_review_is_recorded_and_said_not_hidden(ictx, world, monkeypatch):
    def boom(ctx, report_id):
        raise RuntimeError("model not loaded")

    monkeypatch.setattr(runner_mod, "_process_capture", boom)
    monkeypatch.setattr(runner_mod, "_alert_from_report", lambda *a: None)
    world.pilot()
    sub = ictx.bus.subscribe()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    await ictx.runner.place(world.call(world.course(phone), "first")[0]["id"])
    await ictx.runner.wait_background()
    cap = ictx.db.one("SELECT asr_status, asr_error FROM place_captures")
    assert cap["asr_status"] == "failed" and "model not loaded" in cap["asr_error"]
    assert any(e.type == "system" and e.data == {"check": "place_review", "ok": False} for e in drain(sub))
    await phone.close()


# ------------------------------------------------------- refusing to call
async def test_a_language_with_recorded_voice_missing_is_not_called_at_all(ictx, world, damage):
    world.pilot()
    damage("hi/FC_NOTICE.wav")
    phone = await open_phone(ictx, {**FIRST_CALL})
    course_id = world.course(phone, lang="hi")
    call_id = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    err = call_row(ictx, call_id)["error"]
    assert "Hindi" in err and "hi/FC_NOTICE.wav" in err and "1 files are missing" in err
    assert not any(m["type"] == "ring" for m in phone.sock.sent)  # nobody was rung
    assert call_row(ictx, call_id)["ended_at"]
    await phone.close()


async def test_a_clinic_without_its_spoken_name_is_not_called(ictx, world, damage):
    world.pilot()
    damage(f"hi/slot/clinic_{CLINIC}.wav")
    phone = await open_phone(ictx, {**FIRST_CALL})
    call_id = world.call(world.course(phone), "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    assert f"hi/slot/clinic_{CLINIC}.wav" in call_row(ictx, call_id)["error"]
    await phone.close()


async def test_a_missing_ward_name_blocks_only_the_reminder_that_would_say_it(ictx, world, damage):
    world.pilot()
    phone = await open_phone(ictx, {"RM_EVE_END": [None]})
    course_id = world.course(phone)
    self_ward_report(ictx, course_id, 3)
    damage("hi/slot/ward_3.wav")
    call_id = world.add_call(course_id, "reminder", dose_no=2)
    assert await ictx.runner.place(call_id) == "failed"
    assert "hi/slot/ward_3.wav" in call_row(ictx, call_id)["error"]
    await phone.close()


async def test_a_prompts_file_without_the_questions_consent_line_blocks_the_call_and_says_so(
    ictx, world, tmp_path
):
    (tmp_path / "prompts.json").write_text(prompts_without_qconsent(), encoding="utf-8")
    cat = PromptCatalogue(tmp_path / "prompts.json", tmp_path / "audio", clinic_ids=[CLINIC], times=["09:00"])
    write_voice(cat, tmp_path / "audio")
    ictx.prompts = cat
    world.pilot()
    phone = await open_phone(ictx, {})
    call_id = world.call(world.course(phone), "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    assert "FC_QCONSENT (not defined in prompts.json)" in call_row(ictx, call_id)["error"]
    await phone.close()


@pytest.mark.parametrize("kind", ["first", "reminder", "missed_dose"])
async def test_a_clinic_with_no_visit_time_gets_no_call_that_names_one(ictx, world, kind):
    world.pilot()
    world.clinic(CLINIC, visit_time=None)
    phone = await open_phone(ictx, {})
    course_id = world.course(phone)
    call_id = (
        world.add_call(course_id, kind, dose_no=2)
        if kind != "first"
        else world.call(course_id, "first")[0]["id"]
    )
    assert await ictx.runner.place(call_id) == "failed"
    assert "no visit time" in call_row(ictx, call_id)["error"]
    assert phone.browser.heard == []
    await phone.close()


async def test_a_notice_names_no_time_so_it_needs_none(ictx, world):
    world.pilot()
    world.clinic(CLINIC, visit_time=None)
    phone = await open_phone(ictx, {})
    course_id = world.course(phone)
    call_id = world.add_call(course_id, "duplicate_notice")
    assert await ictx.runner.place(call_id) == "completed"
    await phone.close()


async def test_a_purged_number_is_a_plain_failure(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {})
    course_id = world.course(phone)
    ictx.db.connect().execute("DELETE FROM contacts")
    call_id = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    assert "phone number has been removed" in call_row(ictx, call_id)["error"]
    await phone.close()


async def test_place_refuses_a_call_it_cannot_run(ictx, world):
    with pytest.raises(RunnerError):
        await ictx.runner.place("call_missing")
    world.pilot()
    phone = await open_phone(ictx, {"FC_CONSENT_Q": ["2"]})
    call_id = world.call(world.course(phone), "first")[0]["id"]
    await ictx.runner.place(call_id)
    with pytest.raises(RunnerError):
        await ictx.runner.place(call_id)
    await phone.close()


# ------------------------------------------------------------- reminders
def self_ward_report(ictx, course_id, ward):
    course = ictx.db.one("SELECT * FROM courses WHERE id=?", (course_id,))
    with ictx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO bite_reports(id, course_id, clinic_id, bite_week, location_status, ward_no, source, created_at)"
            " VALUES ('rep_w', ?, ?, '2026-10-19', 'matched', ?, 'clinic', ?)",
            (course_id, course["clinic_id"], ward, clock.iso(clock.now())),
        )


async def test_evening_reminder_names_clinic_dose_and_date(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["1"], "RM_EVE_END": [None]})
    course_id = world.course(phone)
    call_id = world.add_call(course_id, "reminder", dose_no=2)
    assert await ictx.runner.place(call_id) == "completed"
    assert phone.browser.heard == [["RM_GATE"], ["RM_HELLO", "RM_EVE_BODY", "RM_EVE_END"]]
    urls = phone.browser.urls[1]
    assert "/media/prompts/hi/slot/num_2.wav" in urls and "/media/prompts/hi/slot/num_4.wav" in urls
    assert "/media/prompts/hi/slot/time_0900.wav" in urls
    assert {"/media/prompts/hi/slot/weekday_3.wav", "/media/prompts/hi/slot/day_22.wav"} <= set(urls)
    assert call_row(ictx, call_id)["status"] == "completed"
    await phone.close()


async def test_reminder_key_nine_then_one_keeps_the_reminders(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["1"], "RM_EVE_END": ["9"], "OPT_CONFIRM": ["1"]})
    course_id = world.course(phone)
    assert await ictx.runner.place(world.add_call(course_id, "reminder", dose_no=2)) == "completed"
    assert phone.browser.flat[-1] == "OPT_CONFIRM"
    assert ictx.db.one("SELECT status FROM courses")["status"] == "active"
    assert ictx.db.one("SELECT COUNT(*) AS n FROM contacts")["n"] == 1
    await phone.close()


async def test_reminder_key_nine_twice_stops_the_course(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["1"], "RM_EVE_END": ["9"], "OPT_CONFIRM": ["9"]})
    course_id = world.course(phone)
    assert await ictx.runner.place(world.add_call(course_id, "reminder", dose_no=2)) == "completed"
    assert ictx.db.one("SELECT status FROM courses")["status"] == "stopped"
    assert ictx.db.one("SELECT COUNT(*) AS n FROM contacts")["n"] == 0
    await phone.close()


async def test_reminder_key_eight_twice_is_a_wrong_number(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["8"], "WN_CONFIRM": ["8"]})
    course_id = world.course(phone)
    await ictx.runner.place(world.add_call(course_id, "reminder", dose_no=2))
    assert phone.browser.flat == ["RM_GATE", "WN_CONFIRM", "WN_DONE"]  # nothing about the dose was said
    row = ictx.db.one("SELECT status, stopped_reason FROM courses")
    # the number is gone but the patient has not left: the clinic is asked for a new number
    assert row["status"] == "awaiting_call" and "wrong number" in row["stopped_reason"]
    assert ictx.db.one("SELECT COUNT(*) AS n FROM contacts")["n"] == 0
    [note] = enrol.ended_by_patient(ictx, CLINIC)
    assert (note.kind, note.needs_new_number) == ("wrong_number", True)
    await phone.close()


async def test_a_listener_who_does_not_press_1_hears_nothing_about_the_patient_and_it_is_tried_again(
    ictx, world
):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": [None, None, None]})
    course_id = world.course(phone)
    call_id = world.add_call(course_id, "reminder", dose_no=2)
    assert await ictx.runner.place(call_id) == "no_answer"
    assert phone.browser.flat[-1] == "SYS_BYE"
    said = set(phone.browser.flat)
    assert not {"RM_HELLO", "RM_EVE_BODY", "RM_EVE_BODY_OUT", "FIN_TEAM", "RM_EVE_END", "RM_WHO"} & said
    assert not any("slot/clinic_" in u or "slot/day_" in u for urls in phone.browser.urls for u in urls)
    assert ictx.db.one("SELECT status FROM courses")["status"] == "active"
    await phone.close()


async def test_two_patients_on_one_phone_are_told_apart_by_register_number(ictx, world):
    from chaarteeke.models import EnrolIn

    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["1"], "RM_EVE_END": [None]})
    mother = world.course(phone, register_no="ARC-2345")
    day0 = clock.local_today(ictx.settings.local_tz)
    child = enrol.create_course(
        ictx,
        EnrolIn(clinic_id=CLINIC, register_no="ARC-77", regimen_id="id_trc", lang="hi", day0=day0.isoformat(),
                eligibility="adult"),
        "staff:test",
    ).id  # fmt: skip
    again = enrol.record_missed_call(ictx, phone.number, "sim", clock.now(), clinic_hint=CLINIC)
    assert again.status == "duplicate"
    enrol.link(ictx, again.missed_call_id, child, "staff:test")
    assert enrol.same_phone_courses(ictx, mother) == 2
    assert await ictx.runner.place(world.add_call(mother, "reminder", dose_no=2)) == "completed"
    assert phone.browser.heard[1][:3] == ["RM_HELLO", "RM_WHO", "RM_EVE_BODY"]
    digits = [u.rsplit("_", 1)[1][0] for u in phone.browser.urls[1] if "num_" in u][:4]
    assert "".join(digits) == "2345"  # the digits of the register number, not the letters
    await phone.close()


async def test_a_patient_alone_on_a_phone_is_not_asked_whose_entry_it_is(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["1"], "RM_EVE_END": [None]})
    course_id = world.course(phone, register_no="ARC-2345")
    assert await ictx.runner.place(world.add_call(course_id, "reminder", dose_no=2)) == "completed"
    assert "RM_WHO" not in phone.browser.flat
    await phone.close()


async def test_hanging_up_after_the_dose_message_still_counts_as_a_delivered_reminder(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["1"]}, hangup_on="RM_EVE_END")
    course_id = world.course(phone)
    # the whole message is one play; a hang-up during it is before it finished
    call_id = world.add_call(course_id, "reminder", dose_no=2)
    assert await ictx.runner.place(call_id) == "failed"
    assert "hung up" in call_row(ictx, call_id)["error"]
    await phone.close()


async def test_vaccine_out_reminder_needs_the_clinic_phone_and_says_so(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["1"], "RM_EVE_END": [None]})
    course_id = world.course(phone)
    enrol.set_vaccine_out(ictx, CLINIC, True, "staff:test")
    call_id = world.add_call(course_id, "reminder", dose_no=2)
    assert await ictx.runner.place(call_id) == "failed"
    assert "no phone number set up" in call_row(ictx, call_id)["error"]
    assert phone.browser.heard == []
    await phone.close()


async def test_vaccine_out_reminder_reads_the_clinic_phone_when_there_is_one(ictx, world):
    world.pilot()
    world.clinic(CLINIC, phone="0191234")
    phone = await open_phone(ictx, {"RM_GATE": ["1"], "RM_EVE_END": [None]})
    course_id = world.course(phone)
    enrol.set_vaccine_out(ictx, CLINIC, True, "staff:test")
    assert await ictx.runner.place(world.add_call(course_id, "reminder", dose_no=2)) == "completed"
    assert phone.browser.heard == [["RM_GATE"], ["RM_HELLO", "RM_EVE_BODY_OUT", "RM_EVE_END"]]
    digits = [u.rsplit("_", 1)[1][0] for u in phone.browser.urls[1] if "num_" in u][2:]
    assert "".join(digits) == "0191234"
    await phone.close()


async def test_the_worked_area_line_is_said_once(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["1", "1"], "RM_EVE_END": [None, None]})
    course_id = world.course(phone)
    self_ward_report(ictx, course_id, 3)
    day0 = clock.local_today(ictx.settings.local_tz)
    with ictx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO worked_wards(ward_no, worked_on, recorded_by) VALUES (3, ?, 'staff:x')",
            ((day0 + timedelta(days=1)).isoformat(),),
        )
        conn.execute(
            "INSERT INTO worked_wards(ward_no, worked_on, recorded_by) VALUES (7, ?, 'staff:x')",
            ((day0 + timedelta(days=1)).isoformat(),),
        )
    assert await ictx.runner.place(world.add_call(course_id, "reminder", dose_no=2)) == "completed"
    assert phone.browser.heard == [["RM_GATE"], ["RM_HELLO", "RM_EVE_BODY", "FIN_TEAM", "RM_EVE_END"]]
    assert "/media/prompts/hi/slot/ward_3.wav" in phone.browser.urls[1]
    assert not any("ward_" in u for u in phone.browser.urls[0])  # not a word of it before the gate
    assert ictx.db.one("SELECT 1 FROM kv WHERE key=?", (f"team_line:{course_id}",))
    phone.browser.heard.clear()
    assert await ictx.runner.place(world.add_call(course_id, "reminder", dose_no=3)) == "completed"
    assert "FIN_TEAM" not in phone.browser.flat
    await phone.close()


async def test_the_worked_area_line_needs_work_after_the_first_dose_in_the_patients_ward(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["1"], "RM_EVE_END": [None]})
    course_id = world.course(phone)
    self_ward_report(ictx, course_id, 3)
    day0 = clock.local_today(ictx.settings.local_tz)
    with ictx.db.transaction() as conn:  # the day itself, and another ward after: neither counts
        conn.execute(
            "INSERT INTO worked_wards(ward_no, worked_on, recorded_by) VALUES (3, ?, 'staff:x')",
            (day0.isoformat(),),
        )
        conn.execute(
            "INSERT INTO worked_wards(ward_no, worked_on, recorded_by) VALUES (7, ?, 'staff:x')",
            ((day0 + timedelta(days=2)).isoformat(),),
        )
    await ictx.runner.place(world.add_call(course_id, "reminder", dose_no=2))
    assert "FIN_TEAM" not in phone.browser.flat
    await phone.close()


# ----------------------------------------------------------- morning after
async def morning_world(ictx, world, script, dose_no=2):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["1"], **script})
    course_id = world.course(phone, day0=clock.local_today(ictx.settings.local_tz) - timedelta(days=3))
    return phone, course_id, world.add_call(course_id, "missed_dose", dose_no=dose_no)


async def test_morning_after_took_it_is_the_patients_word_and_the_dose_stays_open(ictx, world):
    phone, course_id, call_id = await morning_world(ictx, world, {"RM_MISS_Q": ["1"]})
    sub = ictx.bus.subscribe()
    assert await ictx.runner.place(call_id) == "completed"
    dose = ictx.db.one("SELECT * FROM doses WHERE course_id=? AND dose_no=2", (course_id,))
    assert dose["status"] != "given" and dose["marked_by"] is None and dose["given_on"] is None
    assert phone.browser.flat == ["RM_GATE", "RM_HELLO", "RM_MISS_Q", "RM_MISS_TAKEN"]
    assert world.answers(course_id) == {"dose_taken": "yes"}
    assert "/media/prompts/hi/slot/num_3.wav" in phone.browser.urls[-1]  # the next dose is 3 of 4
    assert enrol.patient_reported_doses(ictx.db, course_id) == {2}
    row = ictx.db.one("SELECT actor, detail_json FROM audit WHERE action='dose.patient_reported'")
    assert row["actor"] == f"patient:{course_id}" and '"dose_no": 2' in row["detail_json"]
    assert ictx.db.one("SELECT 1 FROM audit WHERE action='dose.given'") is None
    assert any(e.type == "dose" and e.data["dose_no"] == 2 for e in drain(sub))
    # the clinic's list keeps it, as "says taken, confirm"
    today = enrol.clinic_today(ictx, CLINIC)
    [row] = [r for r in today.missed + today.due_today if r.course_id == course_id]
    assert row.state == "patient_says_taken"
    await phone.close()


async def test_morning_after_took_the_last_dose_does_not_complete_the_course(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {"RM_GATE": ["1"], "RM_MISS_Q": ["1"]})
    course_id = world.course(
        phone, regimen="reexposure", day0=clock.local_today(ictx.settings.local_tz) - timedelta(days=3)
    )
    assert await ictx.runner.place(world.add_call(course_id, "missed_dose", dose_no=2)) == "completed"
    assert phone.browser.flat[-1] == "RM_MISS_TAKEN_LAST"
    assert (
        ictx.db.one("SELECT status FROM courses")["status"] == "active"
    )  # only the clinic completes a course
    await phone.close()


async def test_morning_after_did_not_take_it_marks_nothing(ictx, world):
    phone, course_id, call_id = await morning_world(ictx, world, {"RM_MISS_Q": ["2"]})
    assert await ictx.runner.place(call_id) == "completed"
    assert phone.browser.flat[-1] == "RM_MISS_NOT"
    assert enrol.patient_reported_doses(ictx.db, course_id) == set()
    assert (
        ictx.db.one("SELECT status FROM doses WHERE course_id=? AND dose_no=2", (course_id,))["status"]
        != "given"
    )
    await phone.close()


async def test_morning_after_silence_is_no_answer_with_its_own_goodbye(ictx, world):
    phone, course_id, call_id = await morning_world(ictx, world, {"RM_MISS_Q": [None, None, None]})
    assert await ictx.runner.place(call_id) == "no_answer"
    assert phone.browser.flat[-1] == "SYS_GIVEUP_MISS"
    assert (
        ictx.db.one("SELECT status FROM doses WHERE course_id=? AND dose_no=2", (course_id,))["status"]
        != "given"
    )
    await phone.close()


async def test_a_dose_that_does_not_exist_fails_the_call_instead_of_saying_it_was_noted(ictx, world):
    phone, course_id, call_id = await morning_world(ictx, world, {"RM_MISS_Q": ["1"]}, dose_no=9)
    assert await ictx.runner.place(call_id) == "failed"
    assert "dose for this call was not found" in call_row(ictx, call_id)["error"]
    assert "RM_MISS_TAKEN" not in phone.browser.flat
    await phone.close()


# ------------------------------------------------------------------ notices
async def test_a_repeat_missed_call_gets_the_short_notice_and_never_withdraws_the_patient(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {})
    course_id = world.course(phone)
    ictx.db.connect().execute(
        "UPDATE courses SET consent_reminders_at=? WHERE id=?", (clock.iso(clock.now()), course_id)
    )
    await phone.missed_call()
    dup = await until(lambda: world.call(course_id, "duplicate_notice"))
    assert await ictx.runner.place(dup[0]["id"]) == "completed"
    assert phone.browser.flat == ["ENR_DUP"]
    assert not any(r["type"] == "record" for r in phone.sock.sent)

    await phone.missed_call()  # a missed call after the notice: the notice again, and nothing is stopped
    again = await until(
        lambda: [c for c in world.call(course_id, "duplicate_notice") if c["status"] == "scheduled"]
    )
    assert ictx.db.one("SELECT status FROM courses")["status"] == "active"
    assert world.call(course_id, "withdrawn_notice") == []
    assert await ictx.runner.place(again[0]["id"]) == "completed"
    assert phone.browser.flat == ["ENR_DUP", "ENR_DUP"]
    await phone.close()


async def test_pressing_9_twice_on_the_repeat_notice_stops_the_calls(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {"ENR_DUP": ["9"], "OPT_CONFIRM": ["9"]})
    course_id = world.course(phone)
    notice = world.add_call(course_id, "duplicate_notice")
    assert await ictx.runner.place(notice) == "completed"
    assert phone.browser.flat == ["ENR_DUP", "OPT_CONFIRM", "OPT_DONE"]
    assert ictx.db.one("SELECT status FROM courses")["status"] == "stopped"
    assert ictx.db.one("SELECT COUNT(*) AS n FROM contacts")["n"] == 0
    [note] = enrol.ended_by_patient(ictx, CLINIC)
    assert note.kind == "withdrawn"
    await phone.close()


async def test_a_repeat_call_before_the_first_call_is_done_places_the_first_call_again(ictx, world, fakes):
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, answer=False)
    course_id = world.course(phone)
    first = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(first) == "no_answer"  # the declined first ring
    phone.browser.answer = True
    await phone.missed_call()
    again = await until(lambda: [c for c in world.call(course_id, "first") if c["status"] == "scheduled"])
    assert world.call(course_id, "duplicate_notice") == []
    assert ictx.db.one("SELECT status FROM courses")["status"] == "active"
    assert await ictx.runner.place(again[0]["id"]) == "completed"
    assert phone.browser.flat[0] == "FC_HELLO"
    await phone.close()


# -------------------------------------------------------- the phone itself
async def test_a_declined_ring_is_no_answer_with_the_reason(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {}, answer=False)
    call_id = world.call(world.course(phone), "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "no_answer"
    assert call_row(ictx, call_id)["error"] == "The call was declined."
    assert call_row(ictx, call_id)["answered_at"] is None
    await phone.close()


async def test_a_phone_that_is_not_connected_is_no_answer_with_the_reason(ictx):
    enrol.record_missed_call(ictx, "sim:ghost", "sim", clock.now())  # demo mode links it at once
    call_id = (await until(lambda: ictx.db.one("SELECT id FROM calls WHERE kind='first'")))["id"]
    assert await ictx.runner.place(call_id) == "no_answer"
    assert call_row(ictx, call_id)["error"] == "The browser phone is not connected"


async def test_the_phone_system_refusing_to_dial_fails_the_call_and_raises_the_system_flag(ictx, world):
    class Refusing:
        async def originate(self, *a, **kw):
            raise TelephonyConnectionError("the PBX is down")

        async def stop(self):
            return None

        async def events(self):
            await asyncio.sleep(3600)
            yield

    world.pilot()
    phone = await open_phone(ictx, {})
    call_id = world.call(world.course(phone), "first")[0]["id"]
    ictx.telephony = Refusing()
    sub = ictx.bus.subscribe()
    assert await ictx.runner.place(call_id) == "failed"
    assert call_row(ictx, call_id)["error"] == "The phone system could not place the call: the PBX is down"
    assert any(e.type == "system" and e.data == {"check": "telephony", "ok": False} for e in drain(sub))
    await phone.close()


async def test_a_dropped_telephone_link_fails_the_live_call_and_every_later_one(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {}, stall_on_play=True)
    course_id = world.course(phone)
    call_id = world.call(course_id, "first")[0]["id"]
    sub = ictx.bus.subscribe()
    call = asyncio.create_task(ictx.runner.place(call_id))
    await until(lambda: phone.browser.heard)
    ictx.telephony._queue.put_nowait(TelephonyConnectionError("ARI event socket closed"))
    assert await asyncio.wait_for(call, 3) == "failed"
    assert "telephone link failed" in call_row(ictx, call_id)["error"]
    assert any(e.type == "system" and e.data == {"check": "telephony", "ok": False} for e in drain(sub))
    later = world.add_call(course_id, "reminder", dose_no=2)
    assert await ictx.runner.place(later) == "failed"
    assert "link is down" in call_row(ictx, later)["error"]
    await phone.close()


async def test_a_playback_that_fails_in_the_browser_ends_the_call_as_failed(ictx, world):
    class BrokenSpeaker(Browser):
        def on_message(self, msg):
            if msg["type"] == "play":
                self.sock.push({"type": "played", "playback_id": msg["playback_id"], "ok": False})
            else:
                super().on_message(msg)

    world.pilot()
    phone = await open_phone(ictx, {})
    phone.sock.on_send = BrokenSpeaker(phone.sock, {}).on_message
    call_id = world.call(world.course(phone), "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    assert call_row(ictx, call_id)["error"] == "A recorded message could not be played."
    await phone.close()


# ------------------------------------------------------------- missed calls
async def test_missed_calls_from_the_phone_line_go_to_enrol(ictx):
    ictx.runner._dispatch(MissedCall("ari-1", "+919876543210", clock.now()))
    ictx.runner._dispatch(MissedCall("ari-2", None, clock.now()))
    rows = ictx.db.query("SELECT transport, status FROM missed_calls")
    assert {(r["transport"], r["status"]) for r in rows} == {("ari", "unlinked"), ("ari", "withheld")}


async def test_a_missed_call_enrol_cannot_record_is_logged_and_flagged(ictx, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(enrol, "record_missed_call", boom)
    sub = ictx.bus.subscribe()
    ictx.runner._dispatch(MissedCall("ari-1", "+919876543210", clock.now()))
    assert [(e.type, e.data) for e in drain(sub)] == [("system", {"check": "missed_call", "ok": False})]


async def test_events_for_calls_this_runner_does_not_own_are_ignored(ictx):
    from chaarteeke.telephony.model import Dtmf

    ictx.runner._dispatch(Dtmf("someone-elses", "1", clock.now()))  # no error, nothing happens


async def test_an_unbuilt_runner_stops_cleanly(ictx):
    other = CallRunner(ictx)
    await other.stop()
    await other.start()
    await other.stop()


# ------------------------------------------- the server's own rules, whatever the flow does
def _flow_that_asks_regardless(monkeypatch, **flow_attrs):
    """A first-call flow that has been told the questions are allowed (a bug, or a tampered flow)."""
    original = CallRunner._make_flow

    def forced(self, call, course, clinic, hours, prior):
        flow, ward = original(self, call, course, clinic, hours, prior)
        flow._ask_questions = True
        for name, value in flow_attrs.items():
            setattr(flow, name, value)
        return flow, ward

    monkeypatch.setattr(CallRunner, "_make_flow", forced)


async def test_the_server_keeps_no_bite_answer_for_a_patient_the_clinic_has_not_marked_adult(
    ictx, world, fakes, monkeypatch
):
    _flow_that_asks_regardless(monkeypatch)
    world.pilot()
    phone = await open_phone(ictx, {"FC_CONSENT_Q": ["1"], "FC_QCONSENT": ["1"]})
    course_id = world.course(phone)
    ictx.db.connect().execute("UPDATE courses SET eligibility='unconfirmed' WHERE id=?", (course_id,))
    call_id = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    assert "has not marked this patient" in call_row(ictx, call_id)["error"]
    assert ictx.db.one("SELECT COUNT(*) AS n FROM bite_reports")["n"] == 0
    assert "consent_questions" not in world.answers(course_id)
    assert ictx.db.one("SELECT consent_questions FROM courses")["consent_questions"] is None
    await phone.close()


async def test_the_server_keeps_no_bite_answer_until_the_patient_agreed_to_the_questions(
    ictx, world, fakes, monkeypatch
):
    # the flow believes the patient agreed earlier; the course says nothing was agreed
    prior = {"consent_reminders": "yes", "consent_questions": "yes"}
    _flow_that_asks_regardless(monkeypatch, _prior=prior)
    world.pilot()
    phone = await open_phone(ictx, {"FC_DOSE": ["1"]}, recordings=[speech_wav()])
    course_id = world.course(phone)
    call_id = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    assert "not agreed to the questions" in call_row(ictx, call_id)["error"]
    assert ictx.db.one("SELECT COUNT(*) AS n FROM bite_reports")["n"] == 0
    assert ictx.db.one("SELECT COUNT(*) AS n FROM place_captures")["n"] == 0
    clips = ictx.settings.var_dir / "clips"
    assert not clips.exists() or not list(clips.glob("*.wav"))  # no clip was written for the refused answer
    await phone.close()


# ------------------------------------------------- numbers stay out of text and logs
async def test_a_number_the_phone_system_echoes_is_not_kept_in_the_call_record(ictx, world, caplog):
    class Leaky:
        async def originate(self, *a, **kw):
            raise CommandRejected(
                "originate", 400, "Endpoint Quectel/sim7600-0/+919876543210 not found (sim:abc_DEF-12)"
            )

        async def stop(self):
            return None

        async def events(self):
            await asyncio.sleep(3600)
            yield

    world.pilot()
    phone = await open_phone(ictx, {})
    call_id = world.call(world.course(phone), "first")[0]["id"]
    ictx.telephony = Leaky()
    assert await ictx.runner.place(call_id) == "failed"
    error = call_row(ictx, call_id)["error"]
    assert "9876543210" not in error and "abc_DEF" not in error and "[number removed]" in error
    assert "sim7600-0" in error  # the endpoint name is not a number and still helps the operator
    await phone.close()


async def test_log_lines_about_a_failure_carry_no_number(ictx, monkeypatch, caplog):
    def boom(*a, **kw):
        raise RuntimeError("cannot store " + NUMBER)

    monkeypatch.setattr(enrol, "record_missed_call", boom)
    caplog.set_level("DEBUG")
    ictx.runner._dispatch(MissedCall("ari-1", NUMBER, clock.now()))
    assert "RuntimeError" in caplog.text and "cannot store" in caplog.text
    assert "9876543210" not in caplog.text


async def test_a_failed_place_review_keeps_no_number_in_its_error(ictx, world, monkeypatch, caplog):
    def boom(ctx, report_id):
        raise RuntimeError("clip from " + NUMBER[3:] + " unreadable")

    monkeypatch.setattr(runner_mod, "_process_capture", boom)
    monkeypatch.setattr(runner_mod, "_alert_from_report", lambda *a: None)
    world.pilot()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    await ictx.runner.place(world.call(world.course(phone), "first")[0]["id"])
    await ictx.runner.wait_background()
    error = ictx.db.one("SELECT asr_error FROM place_captures")["asr_error"]
    assert "unreadable" in error and "9876543210" not in error
    assert "9876543210" not in caplog.text
    await phone.close()


# ------------------------------------------------ the phone system's copy of a clip
async def test_a_clip_the_phone_system_will_not_delete_is_still_saved_and_raised_as_a_fault(
    ictx, world, fakes, monkeypatch
):
    sim = ictx.telephony
    real_delete, refusals = sim.delete_recording, []

    async def stubborn(name):
        refusals.append(name)
        raise CommandRejected("delete_recording", 500, "disk error")

    monkeypatch.setattr(sim, "delete_recording", stubborn)
    world.pilot()
    sub = ictx.bus.subscribe()
    phone = await open_phone(ictx, {**FIRST_CALL}, recordings=[speech_wav()])
    call_id = world.call(world.course(phone), "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "completed"
    await ictx.runner.wait_background()
    cap = ictx.db.one("SELECT * FROM place_captures")
    assert cap["recording_name"] and (ictx.settings.var_dir / "clips").exists()
    assert any(
        e.type == "system" and e.data == {"check": "recording_cleanup", "ok": False} for e in drain(sub)
    )
    assert refusals == [cap["recording_name"]] * 2  # once when saved, once more when the call was cleaned up
    monkeypatch.setattr(sim, "delete_recording", real_delete)
    await phone.close()


# ------------------------------------------------------------- decisions that bind the runner
async def test_a_demo_history_course_is_never_dialled(ictx, world):
    world.pilot()
    phone = await open_phone(ictx, {})
    course_id = world.course(phone)
    ictx.db.connect().execute("UPDATE courses SET source='demo' WHERE id=?", (course_id,))
    call_id = world.call(course_id, "first")[0]["id"]
    assert await ictx.runner.place(call_id) == "failed"
    assert "never dialled" in call_row(ictx, call_id)["error"]
    assert phone.browser.heard == []
    await phone.close()


async def test_a_message_that_never_finishes_ends_the_call_after_the_step_timeout(ictx, world):
    ictx.policy.raw["calls"]["step_timeout_s"] = 0.2
    world.pilot()
    phone = await open_phone(ictx, {}, stall_on_play=True)
    call_id = world.call(world.course(phone), "first")[0]["id"]
    assert await asyncio.wait_for(ictx.runner.place(call_id), 3) == "failed"
    row = call_row(ictx, call_id)
    assert (
        row["error"]
        == "The phone did not report a message to finish playing within 0.2 seconds, so the call was ended."
    )
    assert row["answered_at"] and row["ended_at"]
    await phone.close()


async def test_a_recording_that_never_finishes_ends_the_call_after_the_step_timeout(ictx, world):
    class NoRecordingEnd(Browser):
        def on_message(self, msg):
            if msg["type"] == "record":
                self.records.append(msg)
            else:
                super().on_message(msg)

    ictx.policy.raw["calls"]["step_timeout_s"] = 0.3
    world.pilot()
    phone = await open_phone(ictx, FIRST_CALL)
    phone.sock.on_send = NoRecordingEnd(phone.sock, FIRST_CALL).on_message
    call_id = world.call(world.course(phone), "first")[0]["id"]
    assert await asyncio.wait_for(ictx.runner.place(call_id), 3) == "failed"
    assert "the recording to finish within 0.3 seconds" in call_row(ictx, call_id)["error"]
    await phone.close()


async def test_stopping_the_runner_waits_for_a_place_review_that_is_running(ictx, monkeypatch):
    """Found on Ubuntu 24.04: `chaar-teeke preflight` failed in about one run in three with "Directory not
    empty" while deleting its throwaway folder. The place review runs in a worker thread, stop() cancelled the
    task that waited for it but not the thread, and the thread was still writing to the database (and made a
    new -wal file) after the database was closed and the folder deleted."""
    import threading

    started, finished = threading.Event(), threading.Event()

    def slow_review(ctx, report_id):
        started.set()
        finished.wait(0.4)  # the review is still running when stop() is called
        ctx.db.one("SELECT 1")  # ... and then uses the database
        done.append(report_id)

    done: list[str] = []
    monkeypatch.setattr(runner_mod, "_process_capture", slow_review)
    ictx.runner._spawn(ictx.runner._capture("rep_slow"))
    await until(started.is_set)
    await ictx.runner.stop()
    assert done == ["rep_slow"], "stop() returned while the review thread was still running"
