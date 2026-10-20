"""The SIP end-to-end run (scripts/sip_e2e.py).

Two kinds of test live here:

* Always on, offline, quick: the pieces of the run that can be wrong without a phone call (the config it writes,
  the listener and driver logic, the finding that a recording with no length is not a recording, and the
  shipped Asterisk files the run found faults in).
* The run itself, opt-in: `CT_TEST_SIP=1 uv run pytest tests/telephony/test_sip_e2e.py -q`. It starts a real
  Asterisk, a real softphone (baresip) and the app, places a missed call and a first call over SIP and RTP, and
  needs the tools named in scripts/sip_e2e.sh. Without CT_TEST_SIP=1 it is skipped on purpose, not replaced.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import close_rig, make_rig

from chaarteeke.telephony import RecordingDone

ROOT = Path(__file__).resolve().parents[2]
ASTERISK = ROOT / "telephony" / "asterisk" / "etc" / "asterisk"


def _load():
    spec = importlib.util.spec_from_file_location("sip_e2e", ROOT / "scripts" / "sip_e2e.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sip_e2e"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


rig = _load()


def make_plan(tmp_path: Path):
    plan = rig.Plan(prefix=tmp_path / "prefix", run_dir=tmp_path / "run")
    plan.lan_address = "192.0.2.7"
    plan.ari_password = "ari-secret"
    plan.softphone_password = "phone-secret"
    return plan


# ----------------------------------------------------------------------------- the softphone's control socket
def test_netstrings_round_trip_and_wait_for_the_rest():
    buf = bytearray()
    one, two = rig.netstring(b'{"a":1}'), rig.netstring(b"hello")
    buf.extend(one + two[:3])
    assert rig.read_netstrings(buf) == [b'{"a":1}']
    assert bytes(buf) == two[:3]  # the half message stays until the rest arrives
    buf.extend(two[3:])
    assert rig.read_netstrings(buf) == [b"hello"] and not buf


def test_a_bad_netstring_is_an_error_not_skipped():
    with pytest.raises(rig.RigError):
        rig.read_netstrings(bytearray(b"x:1,"))
    with pytest.raises(rig.RigError):
        rig.read_netstrings(bytearray(b"2:abX"))


def test_prompt_ids_come_from_the_media_name():
    assert rig.prompt_id("sound:chaar/hi/FC_CONSENT_Q") == "FC_CONSENT_Q"
    assert rig.prompt_id("sound:chaar/hi/FC_NOTICE.0") == "FC_NOTICE"
    assert rig.prompt_id("sound:chaar/hi/slot/num_3") is None  # a slot clip is not a prompt
    assert rig.prompt_id("sound:beep") is None and rig.prompt_id(None) is None


# ------------------------------------------------------------------------------- the config the run writes
def test_each_shipped_value_is_replaced_exactly_once_or_the_run_refuses(tmp_path):
    with pytest.raises(rig.RigError):
        rig.substitute_once("a=1\n", r"^b=\d+", "b=2", "b")  # nothing to replace
    with pytest.raises(rig.RigError):
        rig.substitute_once("b=1\nb=2\n", r"^b=\d+", "b=3", "b")  # two places
    assert rig.substitute_once("a=1\nb=2\n", r"^b=\d+", "b=9", "b") == "a=1\nb=9\n"


def test_the_pjsip_file_is_the_shipped_one_with_four_things_changed(tmp_path):
    plan = make_plan(tmp_path)
    shipped = (ASTERISK / "pjsip.conf").read_text()
    out = rig.render_pjsip(shipped, plan)
    assert "bind=192.0.2.7:15060" in out and "bind=127.0.0.1" not in out
    assert 'callerid="Test Patient" <+919999909995>' in out
    assert "password=phone-secret" in out and "dev-only-softphone" not in out
    assert "[test-acl]" in out and "permit=192.0.2.7/255.255.255.255" in out and "deny=0.0.0.0/0.0.0.0" in out
    # everything else is untouched: the endpoint still takes RFC 4733 keys and only these codecs
    assert "dtmf_mode=rfc4733" in out and "allow=ulaw,alaw,slin16" in out and "type=endpoint" in out


def test_the_shipped_softphone_endpoint_is_marked_test_only_and_reachable_from_this_machine_alone():
    """The softphone is the one test-only endpoint in the shipped files. It stays there only because every
    address the shipped PBX listens on is loopback, and it says so where an operator reads it."""
    text = (ASTERISK / "pjsip.conf").read_text()
    binds = re.findall(r"^bind=(\S+)", text, flags=re.MULTILINE)
    assert binds and all(b.startswith("127.0.0.1:") for b in binds), binds
    assert "(test only)" in text and "Everything binds to loopback" in text
    assert re.findall(r"^\[(softphone[^\]]*)\]", text, flags=re.MULTILINE).count("softphone") == 2
    assert "password=dev-only-softphone" in text  # a placeholder that says what it is, never a real secret


def test_the_test_patient_is_in_the_reserved_range_that_is_never_dialled(tmp_path):
    plan = make_plan(tmp_path)
    assert re.fullmatch(r"\+91999990\d{4}", plan.patient_number)


def test_rtp_http_and_ari_files_change_only_ports_and_the_password(tmp_path):
    plan = make_plan(tmp_path)
    assert "rtpstart=31000" in rig.render_rtp((ASTERISK / "rtp.conf").read_text(), plan)
    http = rig.render_http((ASTERISK / "http.conf").read_text(), plan)
    assert "bindport=18088" in http and "bindaddr=127.0.0.1" in http  # ARI stays on loopback
    ari = rig.render_ari((ASTERISK / "ari.conf").read_text(), plan)
    assert "password=ari-secret" in ari and "CHANGE-ME" not in ari


def test_asterisk_main_conf_gives_the_run_its_own_folders(tmp_path):
    plan = make_plan(tmp_path)
    text = rig.render_asterisk_conf(plan)
    assert "[directories]" in text and "(!)" not in text  # a template would be ignored
    for key in ("astspooldir", "astlogdir", "astrundir", "astetcdir", "astvarlibdir"):
        assert re.search(rf"{key} => {re.escape(str(plan.ast))}", text), key


def test_the_softphone_is_set_up_to_answer_by_itself_and_listen_on_the_machine_address(tmp_path):
    plan = make_plan(tmp_path)
    account = rig.render_baresip_account(plan)
    assert "answermode=auto" in account and "auth_pass=phone-secret" in account
    assert "@192.0.2.7:15060" in account
    cfg = rig.render_baresip_config(plan, Path("/modules"))
    assert "sip_listen 192.0.2.7:15070" in cfg and "ctrl_tcp_listen 127.0.0.1:14444" in cfg
    assert (
        "sip_transports udp" in cfg and f"audio_player aufile,{plan.run_dir / 'baresip' / 'heard.wav'}" in cfg
    )


# --------------------------------------------------------------------------------- the checks' own helpers
def test_a_number_in_a_log_is_found_in_every_written_form_and_a_clean_log_is_not(tmp_path):
    log = tmp_path / "messages.log"
    log.write_text("ok line\ncalled 9999909995\nCLIP 99999 09995 seen\nalso 99999-09995\nlast4 9995 only\n")
    hits = rig.find_number_leaks([log], "+919999909995")
    assert hits == ["messages.log:2", "messages.log:3", "messages.log:4"]
    log.write_text("nothing here\nlast4 9995 only\n")
    assert rig.find_number_leaks([log], "+919999909995") == []
    assert rig.find_number_leaks([tmp_path / "missing.log"], "+919999909995") == []


def _after_stop_report(tmp_path, app_log_text, *, with_messages=True):
    plan = make_plan(tmp_path)
    (plan.ast / "log").mkdir(parents=True)
    (plan.ast / "spool").mkdir()
    (plan.run_dir / "app").mkdir()
    (plan.ast / "log" / "console.log").write_text("started\n")
    if with_messages:
        (plan.ast / "log" / "messages.log").write_text("nothing\n")
    (plan.run_dir / "app" / "app.log").write_text(app_log_text)
    report = rig.Report()
    rig.after_stop(plan, report, rig.Rig(), rig.Procs(plan.run_dir))
    return {name: (ok, detail) for name, ok, detail in report.checks}


def test_the_number_check_names_the_files_it_really_read(tmp_path):
    ok, detail = _after_stop_report(tmp_path, "fine\n")["the patient's number is in no Asterisk or app log"]
    assert (
        ok and detail == "read: console.log, messages.log, app.log"
    )  # no queue_log was written, none claimed


def test_the_number_check_fails_on_a_leak_and_on_a_log_that_is_missing(tmp_path):
    name = "the patient's number is in no Asterisk or app log"
    ok, detail = _after_stop_report(tmp_path / "a", "dialled 9999909995\n")[name]
    assert not ok and detail == "app.log:1"
    ok, detail = _after_stop_report(tmp_path / "b", "fine\n", with_messages=False)[name]
    assert not ok and detail == "not found: messages.log"  # a log that was never written proves nothing


def test_wav_stats_tell_speech_from_silence(tmp_path):
    import io
    import math
    import struct

    def make(samples):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
        return buf.getvalue()

    tone = [int(8000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(8000)]
    seconds, level = rig.wav_stats(make(tone))
    assert seconds == 1.0 and 0.15 < level < 0.2
    assert rig.wav_stats(make([0] * 4000)) == (0.5, 0.0)


def test_the_run_uses_the_real_local_time_when_it_is_inside_the_calling_window():
    window = ("09:00", "20:00")
    noon_ist = datetime(2026, 10, 19, 6, 30, tzinfo=UTC)  # 12:00 in Asia/Kolkata
    assert rig.choose_local_tz(noon_ist, window) == "Asia/Kolkata"


def test_the_run_moves_only_the_time_zone_when_the_window_is_closed_and_never_the_window():
    window = ("09:00", "20:00")
    night_ist = datetime(2026, 10, 19, 16, 7, tzinfo=UTC)  # 21:37 in Asia/Kolkata
    zone = rig.choose_local_tz(night_ist, window)
    assert zone != "Asia/Kolkata" and zone.startswith("Etc/GMT")
    local = night_ist.astimezone(rig.ZoneInfo(zone))
    assert 9 * 60 + 15 <= local.hour * 60 + local.minute <= 20 * 60 - 15
    edge = datetime(2026, 10, 19, 14, 20, tzinfo=UTC)  # 19:50 in Asia/Kolkata: too near the end for a run
    assert rig.choose_local_tz(edge, window) != "Asia/Kolkata"


# ---------------------------------------------------------------------------------------- the patient driver
class FakePhone:
    def __init__(self):
        self.commands: list[tuple[str, str]] = []

    async def command(self, name, params="", timeout=5.0):
        self.commands.append((name, params))
        return {"ok": True}


def seen(kind, **detail):
    return rig.Seen(0.0, kind, "chan", detail)


def test_the_driver_presses_a_key_while_its_question_plays_and_speaks_after_the_beep(monkeypatch, tmp_path):
    monkeypatch.setattr(rig, "LISTEN_BEFORE_KEY_S", 0.01)
    monkeypatch.setattr(rig, "SPEAK_AFTER_BEEP_S", 0.01)

    async def go():
        phone = FakePhone()
        driver = rig.Driver(make_plan(tmp_path), phone, tmp_path / "answer.wav")
        for prompt in ("FC_HELLO", "FC_NOTICE"):  # nothing is pressed over these
            driver.on_event(seen("PlaybackStarted", media=f"sound:chaar/hi/{prompt}"))
        driver.on_event(seen("PlaybackStarted", media="sound:chaar/hi/FC_CONSENT_Q"))
        driver.on_event(
            seen("PlaybackStarted", media="sound:chaar/hi/FC_CONSENT_Q")
        )  # a repeat: not pressed twice
        driver.on_event(seen("RecordingStarted"))  # before the place question: not the place answer
        await asyncio.sleep(0.05)
        assert phone.commands == [("sndcode", "1")]
        driver.on_event(seen("PlaybackStarted", media="sound:chaar/hi/FC_PLACE"))
        driver.on_event(seen("RecordingStarted"))
        driver.on_event(seen("RecordingStarted"))  # a second start does not speak twice
        await asyncio.sleep(0.05)
        await driver.settle()
        assert [c for c in phone.commands if c[0] == "ausrc"] == [
            ("ausrc", f"aufile,{tmp_path / 'answer.wav'}")
        ]
        assert [(p.digit, p.prompt) for p in driver.presses] == [("1", "FC_CONSENT_Q")]
        assert not driver.problems and "FC_CONSENT_Q" not in driver.todo and "FC_SICK" in driver.todo

    asyncio.run(go())


def test_a_key_in_a_later_segment_of_one_playback_is_still_pressed(monkeypatch, tmp_path):
    monkeypatch.setattr(rig, "LISTEN_BEFORE_KEY_S", 0.01)

    async def go():
        phone = FakePhone()
        driver = rig.Driver(make_plan(tmp_path), phone, tmp_path / "a.wav")
        driver.on_event(seen("PlaybackContinuing", media="sound:chaar/hi/FC_NOTCH"))
        await asyncio.sleep(0.05)
        assert phone.commands == [("sndcode", "1")]

    asyncio.run(go())


def test_the_keys_are_the_menu_keys_of_the_first_call():
    """The numbers the patient presses are the ones the flow accepts (ivr/flow.py), question by question."""
    from chaarteeke.ivr import flow as fl

    assert rig.KEYS_BY_PROMPT["FC_CONSENT_Q"] in "12"
    assert rig.KEYS_BY_PROMPT["FC_PLACETYPE"] in fl.PLACE_TYPE_BY_KEY
    assert rig.KEYS_BY_PROMPT["FC_NOTCH"] in fl.NOTCH_BY_KEY
    # 1 = a street dog: it is not a stored animal itself, it leads to the notch question that is
    assert rig.KEYS_BY_PROMPT["FC_ANIMAL"] == "1" and "1" not in fl.ANIMAL_BY_KEY
    assert rig.KEYS_BY_PROMPT["FC_SICK"] in fl.SIGNS_BY_KEY
    assert fl.NOTCH_BY_KEY[rig.KEYS_BY_PROMPT["FC_NOTCH"]] == "notched"
    assert fl.PLACE_TYPE_BY_KEY[rig.KEYS_BY_PROMPT["FC_PLACETYPE"]] == "home_street"
    assert fl.SIGNS_BY_KEY[rig.KEYS_BY_PROMPT["FC_SICK"]] == "no"
    assert rig.EXPECTED_ANSWERS["animal"] == "notched" and rig.EXPECTED_ANSWERS["signs"] == "no"


# ----------------------------------------------------------- what the run found in the shipped Asterisk files
def test_a_dialplan_line_has_balanced_brackets_before_any_semicolon():
    """Asterisk cuts a dialplan line at the first ';' and does not skip the rest of an argument: a ';' inside
    NoOp(...) left 'NoOp(missed call entering Stasis' and a warning at every start (found on a real Asterisk)."""
    bad = []
    for n, line in enumerate((ASTERISK / "extensions.conf").read_text().splitlines(), 1):
        code = line.split(";", 1)[0]
        if code.count("(") != code.count(")"):
            bad.append(f"extensions.conf:{n}: {line.strip()}")
    assert not bad, "\n".join(bad)


def test_modules_conf_leaves_out_what_opens_a_port_on_every_address():
    text = (ASTERISK / "modules.conf").read_text()
    noloaded = set(re.findall(r"^\s*noload\s*=>\s*(\S+)", text, flags=re.MULTILINE))
    # found by listing the open sockets of a running Asterisk: IAX2 4569, UniStim 5000, DUNDi 4520
    assert {"chan_iax2.so", "chan_unistim.so", "pbx_dundi.so"} <= noloaded
    assert "res_cdrel_custom.so" in noloaded  # Asterisk 22's combined call-record writer


def test_the_beep_sound_the_place_question_promises_is_shipped_and_shorter_than_the_driver_waits():
    beep = ROOT / "telephony" / "asterisk" / "sounds" / "beep.wav"
    with wave.open(str(beep)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 8000)
        length = w.getnframes() / w.getframerate()
    assert 0.2 <= length < rig.SPEAK_AFTER_BEEP_S


# --------------------------------------------------------------------- the adapter finding: no length, no clip
def _finished(name: str, **fields) -> dict:
    return {
        "type": "RecordingFinished",
        "timestamp": "2026-10-19T10:00:00.000+0530",
        "recording": {"name": name, "format": "wav", "state": "done", "target_uri": "channel:c1", **fields},
    }


def test_a_recording_asterisk_finishes_without_a_length_is_reported_as_not_recorded():
    """With beep=true and no 'beep' sound installed, a real Asterisk answered the record request with 201 and
    then sent RecordingFinished state=done with no duration at all, and no file. That is not a recording."""

    async def go(r):
        await r.fake._send(_finished("none-1"))
        done = await r.next()
        assert isinstance(done, RecordingDone) and not done.ok and done.duration_s is None
        assert "recording anything" in done.cause and "beep" in done.cause

    asyncio.run(_with_rig(go))


def test_a_silent_recording_has_a_length_and_stays_a_finished_recording():
    async def go(r):
        await r.fake._send(_finished("silent-1", duration=0, talking_duration=0, silence_duration=0))
        done = await r.next()
        assert isinstance(done, RecordingDone) and done.ok and done.duration_s == 0 and done.talking_s == 0

    asyncio.run(_with_rig(go))


async def _with_rig(coro):
    r = await make_rig()
    try:
        await coro(r)
    finally:
        await close_rig(r)


# ------------------------------------------------------------- the opt-in speech-model mode (offline pieces)
def test_the_speech_model_is_off_unless_the_run_asks_for_it():
    off = rig.plan_from_env({})
    assert off.asr is False and off.model_dir == "" and off.expect_ward is None
    on = rig.plan_from_env(
        {"CT_TEST_SIP_ASR": "1", "CT_ASR_MODEL_DIR": "/models/ic", "CT_TEST_SIP_EXPECT_WARD": "6"}
    )
    assert on.asr is True and on.model_dir == "/models/ic" and on.expect_ward == 6
    assert rig.plan_from_env({"CT_TEST_SIP_ASR": "0"}).asr is False  # only "1" opts in


def test_the_app_gets_the_model_only_in_the_speech_mode_and_never_a_default_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("CT_ASR_MODEL_DIR", "/should/not/leak")  # the parent's CT_ variables are not passed on
    plan = make_plan(tmp_path)
    app = rig.App(plan, rig.Procs(tmp_path))
    env = app.env()
    assert env["CT_ASR"] == "off" and "CT_ASR_MODEL_DIR" not in env
    plan.asr, plan.model_dir = True, str(tmp_path / "model")
    env = app.env()
    assert env["CT_ASR"] == "indicconformer" and env["CT_ASR_MODEL_DIR"] == str(tmp_path / "model")


def test_the_speech_mode_refuses_to_start_without_a_complete_model_folder(tmp_path):
    plan = make_plan(tmp_path)
    plan.asr = True
    with pytest.raises(rig.RigError, match="CT_ASR_MODEL_DIR"):
        rig.check_speech_setup(plan)  # no default folder
    plan.model_dir = str(tmp_path / "model")
    (tmp_path / "model" / "assets").mkdir(parents=True)
    with pytest.raises(rig.RigError, match="lacks assets/"):
        rig.check_speech_setup(plan)
    for name in ("preprocessor.ts", "encoder.onnx", "ctc_decoder.onnx", "vocab.json", "language_masks.json"):
        (tmp_path / "model" / "assets" / name).write_bytes(b"x")
    rig.check_speech_setup(plan)  # complete: no error


def test_the_clip_keeper_keeps_a_file_the_app_then_deletes(tmp_path):
    watched, kept = tmp_path / "clips", tmp_path / "kept"
    keeper = rig.ClipKeeper(watched, kept)
    keeper.start()
    try:
        keeper.sweep()  # the folder does not exist yet: nothing to keep, not an error
        assert keeper.kept == [] and keeper.error is None
        watched.mkdir()
        tmp = watched / ".r1.abcd.tmp"
        tmp.write_bytes(b"RIFF-the-clip")
        tmp.rename(watched / "r1.wav")  # the app writes a temp name, then renames
        keeper.sweep()
        (watched / "r1.wav").unlink()  # the app deletes the clip when it places the report by itself
        keeper.sweep()
    finally:
        keeper.stop()
    assert [p.name for p in keeper.kept] == ["1.wav"]  # one file, kept once under both of its names
    assert keeper.kept[0].read_bytes() == b"RIFF-the-clip"
    assert not list(watched.iterdir()) and keeper.error is None


def test_the_clip_keeper_can_keep_the_names_asterisk_gave(tmp_path):
    watched = tmp_path / "rec"
    watched.mkdir()
    (watched / "call_x-r1.wav").write_bytes(b"a")
    (watched / "call_x-r2.wav").write_bytes(b"b")
    keeper = rig.ClipKeeper(watched, tmp_path / "kept", keep_names=True)
    keeper.start()
    keeper.stop()
    assert sorted(p.name for p in keeper.kept) == ["call_x-r1.wav", "call_x-r2.wav"]


def _row(**fields):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    names = list(fields)
    conn.execute(f"CREATE TABLE t ({', '.join(names)})")
    conn.execute(f"INSERT INTO t VALUES ({', '.join('?' for _ in names)})", list(fields.values()))
    return conn.execute("SELECT * FROM t").fetchone()


def test_the_outcome_of_a_placed_report_carries_no_words_and_a_queued_one_carries_its_candidates():
    capture = _row(
        asr_status="done",
        asr_model="m",
        voiced_s=0.8,
        confidence="likely",
        transcript=None,
        candidates_json=None,
        asr_error=None,
    )
    placed = rig.spoken_outcome(capture, _row(location_status="matched", ward_no=6))
    assert placed["state"] == "placed by itself" and placed["ward_no"] == 6
    assert placed["transcript"] is None and placed["candidates"] is None  # the app deleted both on purpose
    assert placed["voiced_s"] == 0.8  # the measured voiced seconds stay for review
    queued_capture = _row(
        asr_status="done",
        asr_model="m",
        voiced_s=0.8,
        confidence="possible",
        transcript="अपर",
        candidates_json='[{"ward_no": 66, "ward_name": "Upper Muthi", "label": "possible", "evidence": "Upper Muthi"}]',
        asr_error=None,
    )
    queued = rig.spoken_outcome(queued_capture, _row(location_status="needs_review", ward_no=None))
    assert queued["state"] == "needs_review" and queued["transcript"] == "अपर"
    assert queued["candidates"] == [
        {"ward_no": 66, "ward_name": "Upper Muthi", "label": "possible", "evidence": "Upper Muthi"}
    ]
    assert rig.spoken_outcome(None, None) == {"state": "no capture row"}


def test_the_heard_script_compares_decisions_and_names_a_difference():
    heard = importlib.util.spec_from_file_location("sip_e2e_heard", ROOT / "scripts" / "sip_e2e_heard.py")
    module = importlib.util.module_from_spec(heard)
    sys.modules["sip_e2e_heard"] = module
    heard.loader.exec_module(module)
    ours = {"words": "w", "combined": {"confidence": "likely", "wards": [6, 7]}}
    assert module.agrees({"state": "placed by itself", "ward_no": 6}, ours) == "same ward"
    assert module.agrees({"state": "placed by itself", "ward_no": 7}, ours).startswith("DIFFERENT ward")
    assert module.agrees({"state": "needs_review"}, ours).startswith("DIFFERENT: app needs_review")
    queued = {"words": "w", "combined": {"confidence": "possible", "wards": [6]}}
    assert module.agrees({"state": "needs_review", "transcript": "w"}, queued) == "same decision, same words"
    assert "DIFFERENT words" in module.agrees({"state": "needs_review", "transcript": "x"}, queued)
    assert module.agrees(None, queued) == "no app decision to compare"


# --------------------------------------------------------------------------------- the run itself (opt-in)
@pytest.mark.real_asterisk
@pytest.mark.skipif(
    os.environ.get("CT_TEST_SIP") != "1", reason="CT_TEST_SIP=1 not set (opt-in SIP end-to-end run)"
)
def test_a_missed_call_and_a_first_call_over_sip_end_to_end():
    plan = rig.plan_from_env()
    plan.asr, plan.expect_ward = (
        False,
        None,
    )  # this run is the speech-model-off one, whatever CT_TEST_SIP_ASR says
    report = asyncio.run(rig.run(plan))
    failed = [f"{name}: {detail}" for name, ok, detail in report.checks if not ok]
    print()
    rig.print_summary(report, plan)
    assert report.ok and report.checks, "failed checks:\n" + "\n".join(failed)


@pytest.mark.real_asterisk
@pytest.mark.asr
@pytest.mark.skipif(
    os.environ.get("CT_TEST_SIP_ASR") != "1",
    reason="CT_TEST_SIP_ASR=1 not set (opt-in: SIP end-to-end run with the speech model on, about 2.2 GB)",
)
def test_a_spoken_place_crosses_the_exchange_and_the_speech_model_places_it():
    """Needs CT_ASR_MODEL_DIR (there is no default folder). The demo clip 1 says "mera ghar Gujjar Nagar mein
    hai" (ward 6); override with CT_TEST_SIP_EXPECT_WARD and the clip's own ward for another one."""
    plan = rig.plan_from_env()
    assert plan.asr
    if plan.expect_ward is None:
        plan.expect_ward = 6
    report = asyncio.run(rig.run(plan))
    failed = [f"{name}: {detail}" for name, ok, detail in report.checks if not ok]
    print()
    rig.print_summary(report, plan)
    assert report.ok and report.checks, "failed checks:\n" + "\n".join(failed)
