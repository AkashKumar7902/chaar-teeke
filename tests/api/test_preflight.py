"""`chaar-teeke preflight`: one line per check, nothing loaded, the real database untouched."""

from __future__ import annotations

import sys

import pytest

from chaarteeke import main, preflight
from chaarteeke.config import Settings


def settings_of(app) -> Settings:
    return app.state.ctx.settings


async def test_a_ready_system_passes_every_check(make_app, capsys):
    app = make_app(seeded=False)
    settings = settings_of(app)
    assert await preflight.run(settings) is True
    lines = capsys.readouterr().out.strip().splitlines()
    names = [ln[6:17].strip() for ln in lines[:-1]]
    assert names == [
        "database",
        "data",
        "voice hi",
        "voice doi",
        "voice ur",
        "speech",
        "telephony",
        "scheduler",
        "sim call",
    ]
    assert all(ln.startswith("ok  ") for ln in lines[:-1]), lines
    assert lines[-1] == "9 of 9 checks passed"
    assert "CT_ASR=off" in lines[5] and "throwaway database" in lines[8]


async def test_the_end_to_end_call_leaves_the_real_database_alone(make_app):
    app = make_app(seeded=False)
    ctx = app.state.ctx
    await preflight.run(settings_of(app))
    for table in ("courses", "calls", "missed_calls", "bite_reports", "contacts"):
        assert ctx.db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0, table


async def test_missing_voice_fails_the_languages_in_use_and_the_call(make_app, capsys):
    app = make_app(voice=False, seeded=False)
    ctx = app.state.ctx
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO clinics(id, name, short_name, default_regimen, default_lang, arv_status, visit_time)"
            " VALUES ('cln_a','A','A','id_trc','hi','confirmed','09:00')"
        )
    assert await preflight.run(settings_of(app)) is False
    out = capsys.readouterr().out
    lines = {ln[6:17].strip(): ln for ln in out.splitlines()}
    assert lines["voice hi"].startswith("FAIL") and "is in use and" in lines["voice hi"]
    assert lines["voice ur"].startswith("ok  ") and "not in use yet" in lines["voice ur"]
    assert lines["sim call"].startswith("FAIL")
    assert out.strip().splitlines()[-1].endswith("failed") and "checks passed" in out


async def test_a_phone_line_that_cannot_be_reached_fails_the_telephony_check(make_app, capsys):
    app = make_app(seeded=False, telephony="ari", ari_url="http://127.0.0.1:9")
    assert await preflight.run(settings_of(app)) is False
    lines = {ln[6:17].strip(): ln for ln in capsys.readouterr().out.splitlines()}
    assert lines["telephony"].startswith("FAIL") and "CT_TELEPHONY=ari" in lines["telephony"]
    assert lines["sim call"].startswith("ok  ")  # the browser phone check does not depend on the line


async def test_the_speech_model_is_reported_never_loaded(make_app, tmp_path, monkeypatch, capsys):
    from chaarteeke.speech import asr

    def never(*_a, **_k):
        raise AssertionError("preflight tried to load the speech model")

    monkeypatch.setattr(asr, "IndicConformerTranscriber", never)
    monkeypatch.setattr(asr, "load_transcriber", never)
    monkeypatch.setenv(asr.MODEL_DIR_ENV, str(tmp_path / "no-model-here"))
    before = {m for m in ("torch", "onnxruntime") if m in sys.modules}
    app = make_app(seeded=False, asr="indicconformer")
    result = preflight.check_asr(settings_of(app))
    assert result.ok is False and "no-model-here" in result.detail and "CT_ASR=off" in result.detail
    monkeypatch.setattr(asr, "check_runtime", lambda *_a, **_k: None)
    found = preflight.check_asr(settings_of(app))
    assert found.ok is True and "not loaded here" in found.detail
    assert {m for m in ("torch", "onnxruntime") if m in sys.modules} == before


async def test_the_scheduler_check_names_a_running_server(make_app):
    from datetime import timedelta

    from chaarteeke import clock
    from chaarteeke.scheduler import LOCK_NAME

    app = make_app(seeded=False)
    ctx = app.state.ctx
    assert "no scheduler holds the lock" in preflight.check_scheduler(ctx).detail
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO locks(name, holder, heartbeat_at) VALUES (?, 'sched-abc', ?)", (LOCK_NAME, clock.iso(clock.now())))  # fmt: skip
    assert "sched-abc holds the lock" in preflight.check_scheduler(ctx).detail
    stale = clock.iso(clock.now() - timedelta(seconds=ctx.policy["scheduler"]["lock_stale_s"] + 5))
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE locks SET heartbeat_at = ?", (stale,))
    assert "stale" in preflight.check_scheduler(ctx).detail


def test_the_command_exits_non_zero_on_a_failed_check(monkeypatch):
    async def fails(_settings, with_asr=False):
        return False

    async def passes(_settings, with_asr=False):
        return True

    monkeypatch.setattr(main, "get_settings", lambda: None)
    monkeypatch.setattr(preflight, "run", fails)
    assert main.cli(["preflight"]) == 1
    monkeypatch.setattr(preflight, "run", passes)
    assert main.cli(["preflight"]) == 0


# ------------------------------------------------------------------ --with-asr
class FakeModel:
    """Stands in for the speech model so no test loads the real one."""

    model_id = "fake"

    def __init__(self, text="बख्शी नगर के पास"):
        self.loads, self.heard, self.text = 0, [], text

    def load(self):
        self.loads += 1

    def transcribe(self, wav, lang):
        from chaarteeke.speech.asr import Transcript

        self.heard.append((wav, lang))
        return Transcript(text=self.text, model="fake", seconds=1.5)


@pytest.fixture
def model(monkeypatch):
    from chaarteeke.speech import asr

    fake = FakeModel()
    monkeypatch.setattr(asr, "load_transcriber", lambda _settings: fake)
    return fake


def clip_dir(app):
    return settings_of(app).data_dir / "demo" / "clips"


def test_with_asr_loads_the_model_once_and_hears_the_first_clip(make_app, model):
    app = make_app(seeded=False, asr="indicconformer")
    clip_dir(app).mkdir(parents=True)
    (clip_dir(app) / "2.wav").write_bytes(b"second")
    (clip_dir(app) / "1.wav").write_bytes(b"first")
    result = preflight.check_asr_loads(settings_of(app))
    assert result.ok is True and result.name == "speech load"
    assert "1.wav" in result.detail and "बख्शी नगर के पास" in result.detail
    assert model.loads == 1 and model.heard == [(b"first", "hi")]


def test_with_asr_and_no_clip_says_what_is_missing(make_app, model):
    app = make_app(seeded=False, asr="indicconformer")
    result = preflight.check_asr_loads(settings_of(app))
    assert result.ok is False and "no .wav clip" in result.detail and "demo/clips" in result.detail
    assert "clips-plan" in result.detail  # how to make them
    assert model.loads == 1 and model.heard == []  # the model did load; nothing stood in for the clip


def test_with_asr_and_a_missing_model_says_so_and_names_the_folder(make_app, tmp_path, monkeypatch):
    from chaarteeke.speech import asr

    monkeypatch.setenv(asr.MODEL_DIR_ENV, str(tmp_path / "no-model-here"))
    app = make_app(seeded=False, asr="indicconformer")
    result = preflight.check_asr_loads(settings_of(app))
    assert result.ok is False and "no-model-here" in result.detail and "CT_ASR=off" in result.detail


def test_with_asr_fails_when_the_model_hears_nothing(make_app, model):
    app = make_app(seeded=False, asr="indicconformer")
    clip_dir(app).mkdir(parents=True)
    (clip_dir(app) / "1.wav").write_bytes(b"x")
    model.text = "  "
    result = preflight.check_asr_loads(settings_of(app))
    assert result.ok is False and "heard nothing" in result.detail


def test_with_asr_fails_when_the_model_breaks_on_load(make_app, monkeypatch):
    from chaarteeke.speech import asr

    def broken(_settings):
        raise OSError("weights corrupt")

    monkeypatch.setattr(asr, "load_transcriber", broken)
    app = make_app(seeded=False, asr="indicconformer")
    result = preflight.check_asr_loads(settings_of(app))
    assert result.ok is False and "OSError" in result.detail and "weights corrupt" in result.detail


def test_with_asr_and_ct_asr_off_is_a_failed_check_not_a_silent_skip(make_app, model):
    app = make_app(seeded=False)  # asr="off"
    result = preflight.check_asr_loads(settings_of(app))
    assert result.ok is False and "CT_ASR=off" in result.detail and model.loads == 0


async def test_the_flag_adds_one_line_after_the_speech_check_and_without_it_nothing_is_loaded(
    make_app, model, capsys
):
    app = make_app(seeded=False, asr="indicconformer")
    clip_dir(app).mkdir(parents=True)
    (clip_dir(app) / "1.wav").write_bytes(b"x")
    await preflight.run(settings_of(app))
    plain = capsys.readouterr().out.splitlines()
    assert model.loads == 0 and not any("speech load" in ln for ln in plain)
    await preflight.run(settings_of(app), with_asr=True)
    lines = capsys.readouterr().out.splitlines()
    names = [ln[6:17].strip() for ln in lines[:-1]]
    assert names.index("speech load") == names.index("speech") + 1
    assert model.loads == 1


def test_the_command_passes_the_flag_on(monkeypatch):
    seen = []

    async def run(_settings, with_asr=False):
        seen.append(with_asr)
        return True

    monkeypatch.setattr(main, "get_settings", lambda: None)
    monkeypatch.setattr(preflight, "run", run)
    assert main.cli(["preflight"]) == 0 and main.cli(["preflight", "--with-asr"]) == 0
    assert seen == [False, True]


async def test_preflight_leaves_the_running_servers_call_link_alone(make_app, capsys):
    """Found on Ubuntu 24.04 with Asterisk 20: the telephony check connected an event socket under the app's own
    name, and closing it unregistered the application of the server that was running, so missed calls were no
    longer taken until the server restarted. The check now uses a name of its own."""
    from chaarteeke.telephony import AriAdapter
    from chaarteeke.telephony.fake_ari import PASSWORD, USER, FakeAri

    pbx = FakeAri()
    url = await pbx.start()
    live = AriAdapter(url, USER, PASSWORD, app="chaar")
    await live.start()  # the running server's link
    try:
        assert pbx.apps == {"chaar"}
        app = make_app(seeded=False, telephony="ari", ari_url=url, ari_user=USER, ari_password=PASSWORD)
        assert await preflight.run(settings_of(app)) is True
        lines = {ln[6:17].strip(): ln for ln in capsys.readouterr().out.splitlines()}
        assert lines["telephony"].startswith("ok  ")
        assert pbx.event_sockets_seen[0] == "chaar"
        assert len(pbx.event_sockets_seen) == 2 and pbx.event_sockets_seen[1] != "chaar"
        assert pbx.apps == {"chaar"}, "the running server's application must still be registered"
    finally:
        await live.stop()
        await pbx.stop()
