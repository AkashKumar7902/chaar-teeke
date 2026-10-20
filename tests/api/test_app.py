"""The composition root: the clock, the keys, static files, error statuses and a start and stop."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chaarteeke import clock, main
from chaarteeke.api.app import STATUS_BY_CODE, create_app, error_response
from chaarteeke.config import Settings
from chaarteeke.scheduler import LOCK_NAME


def test_the_demo_clock_offset_survives_a_restart(make_app, clock_t0):
    app = make_app(seeded=False)
    ctx = app.state.ctx
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO kv(key, value) VALUES ('demo_clock_offset_s', '7200')")
    again = create_app(ctx.settings, start_background=False)
    try:
        installed = clock.current()
        assert isinstance(installed, clock.OffsetClock) and installed.offset.total_seconds() == 7200
    finally:
        again.state.ctx.db.close()


def test_a_pilot_server_runs_on_the_system_clock(make_app, clock_t0):
    make_app(seeded=False, mode="pilot")
    assert isinstance(clock.current(), clock.SystemClock)


LONG_KEY = "k" * 32


def pilot_base(tmp_path) -> dict:
    return dict(
        _env_file=None, mode="pilot", db=tmp_path / "p.db", var_dir=tmp_path / "var", asr="off",
        public_url="https://ct.example.org",
    )  # fmt: skip


def test_a_pilot_server_will_not_start_without_its_keys(tmp_path):
    base = pilot_base(tmp_path)
    with pytest.raises(RuntimeError, match="CT_SECRET_KEY is required"):
        create_app(Settings(**base, phone_key="x"), start_background=False)
    with pytest.raises(RuntimeError, match="CT_PHONE_KEY"):
        create_app(Settings(**base, secret_key=LONG_KEY), start_background=False)


def test_a_pilot_server_will_not_start_with_a_short_session_key(tmp_path):
    base = pilot_base(tmp_path)
    for short in ("x", "k" * 31):
        with pytest.raises(RuntimeError, match="at least 32 characters"):
            create_app(Settings(**base, secret_key=short, phone_key="x"), start_background=False)
    # exactly 32 passes the key check (the phone key is checked next)
    with pytest.raises(RuntimeError, match="CT_PHONE_KEY"):
        create_app(Settings(**base, secret_key=LONG_KEY), start_background=False)


def test_a_demo_server_may_keep_a_short_session_key(tmp_path):
    from cryptography.fernet import Fernet

    app = create_app(
        Settings(
            _env_file=None, mode="demo", db=tmp_path / "d.db", var_dir=tmp_path / "var", asr="off",
            secret_key="s", phone_key=Fernet.generate_key().decode(),
        ),
        start_background=False,
    )  # fmt: skip
    app.state.ctx.db.close()


def test_a_pilot_server_will_not_start_without_its_public_address(tmp_path):
    base = pilot_base(tmp_path) | {"public_url": ""}
    with pytest.raises(RuntimeError, match="CT_PUBLIC_URL is required"):
        create_app(Settings(**base, secret_key=LONG_KEY, phone_key="x"), start_background=False)
    with pytest.raises(RuntimeError, match="CT_PUBLIC_URL is required"):
        create_app(
            Settings(**(base | {"public_url": "not a url"}), secret_key=LONG_KEY, phone_key="x"),
            start_background=False,
        )


def test_prompt_audio_is_served_to_anyone_and_a_missing_file_is_a_404(client, ctx):
    path = next(ctx.settings.data_dir.joinpath("prompts", "audio", "hi").glob("FC_NOTICE*.wav")).name
    res = client.get(f"/media/prompts/hi/{path}")
    assert res.status_code == 200 and res.headers["content-type"] in (
        "audio/x-wav",
        "audio/wav",
        "audio/vnd.wave",
    )
    assert client.get("/media/prompts/hi/nothing.wav").status_code == 404


def test_the_ward_outlines_and_areas_are_public_files(client):
    wards = client.get("/data/wards.geojson")
    assert wards.status_code == 200 and wards.headers["content-type"].startswith("application/geo+json")
    assert client.get("/data/clusters.json").json()["clusters"]
    assert client.get("/data/policy.json").status_code == 404
    assert client.get("/data/gazetteer.json").status_code == 404


def test_every_page_has_its_shell_and_the_404_page_is_served_for_the_rest(make_app, clock_t0, tmp_path):
    from chaarteeke.api.app import CSP, NOT_FOUND_PAGE, PAGES

    web = tmp_path / "web"
    (web / "css").mkdir(parents=True)
    (web / "css" / "base.css").write_text("body{}")
    for shell in {*PAGES.values(), NOT_FOUND_PAGE}:
        (web / shell).write_text(f"<!doctype html><title>{shell}</title>")
    app = make_app(seeded=False, web_dir=web)
    clock.install(clock_t0)
    client = TestClient(app)
    for route, shell in PAGES.items():
        res = client.get(route.replace("{course_id}", "crs_x").replace("{cluster_id}", "c1"))
        assert res.status_code == 200 and res.headers["content-type"].startswith("text/html"), route
        assert shell in res.text and res.headers["content-security-policy"] == CSP, route
    gone = client.get("/no/such/page")
    assert gone.status_code == 404 and NOT_FOUND_PAGE in gone.text
    assert client.get("/404").status_code == 404
    assert client.get("/css/base.css").status_code == 200
    assert client.get("/api/no/such").json()["error"] == "not_found"


def test_starting_and_stopping_runs_the_phone_the_runner_and_the_scheduler(make_app, clock_t0):
    app = make_app(scheduler=True, background=True)
    clock.install(clock_t0)
    ctx = app.state.ctx
    assert ctx.extras.get("telephony_up") is None
    with TestClient(app):
        assert ctx.extras["telephony_up"] is True and ctx.extras["asr_state"] == "off"
        import time

        end = time.monotonic() + 5
        while (
            ctx.db.one("SELECT 1 FROM locks WHERE name = ?", (LOCK_NAME,)) is None and time.monotonic() < end
        ):
            time.sleep(0.05)
        assert ctx.db.one("SELECT holder FROM locks WHERE name = ?", (LOCK_NAME,)) is not None
    assert ctx.extras["telephony_up"] is False
    assert ctx.db.one("SELECT 1 FROM locks WHERE name = ?", (LOCK_NAME,)) is None  # the lock is given up


def test_with_the_scheduler_switched_off_no_lock_is_taken(make_app, clock_t0):
    app = make_app(scheduler=False, background=True)
    clock.install(clock_t0)
    with TestClient(app):
        assert app.state.ctx.db.one("SELECT 1 FROM locks") is None


# ------------------------------------------------------------- error statuses
class Err(Exception):
    def __init__(self, code, message="plain words", **extra):
        super().__init__(message)
        self.code, self.message = code, message
        for k, v in extra.items():
            setattr(self, k, v)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        # enrol and doses
        ("not_found", 404), ("unknown_clinic", 404), ("unknown_regimen", 400), ("regimen_disabled", 409),
        ("bad_date", 400), ("date_in_future", 400), ("not_awaiting", 409), ("already_linked", 409),
        ("course_closed", 409), ("missed_call_closed", 409), ("wrong_clinic", 403), ("phone_purged", 410),
        ("course_not_active", 409), ("dose_in_future", 409), ("dose_not_needed", 409),
        # alerts
        ("forbidden", 403), ("read_only", 403), ("already_claimed", 409), ("already_closed", 409),
        ("not_claimed", 409), ("not_yours", 403), ("note_needed", 400), ("no_course", 409), ("bad_status", 400),
        # demo, stats, review, prompts
        ("not_demo", 404), ("clock_fixed", 409), ("bad_target", 400), ("nothing_scheduled", 409),
        ("no_simulator_course", 409), ("no_dose_ahead", 409), ("not_empty", 409), ("unknown_ward", 404),
        ("unknown_cluster", 404), ("not_in_queue", 409), ("already_decided", 409), ("bad_decision", 400),
        ("bad_ward", 404), ("no_audio", 410), ("no_name", 400), ("unknown_prompt", 404), ("missing_slot", 400),
    ],
)  # fmt: skip
def test_each_service_error_code_has_a_sensible_status(code, status):
    res = error_response(Err(code))
    assert res.status_code == status, code
    assert res.body == f'{{"error":"{code}","message":"plain words"}}'.encode()


def test_an_error_may_carry_its_own_status_and_an_unlisted_code_is_a_conflict():
    assert error_response(Err("prompt_error", status=418)).status_code == 418
    assert error_response(Err("some_new_refusal")).status_code == 409
    assert error_response(Err("unknown_prompt", status=409)).status_code == 404  # the table wins


def test_an_exception_with_no_code_is_a_plain_500():
    res = error_response(RuntimeError("password=hunter2"))
    assert res.status_code == 500 and b"hunter2" not in res.body
    assert res.body == b'{"error":"server_error","message":"Something went wrong on the server"}'


def test_every_status_in_the_table_is_an_error_status():
    assert all(400 <= s < 600 for s in STATUS_BY_CODE.values())


# --------------------------------------------------------------------- the CLI
def settings_in(tmp_path, **kw) -> Settings:
    from cryptography.fernet import Fernet

    return Settings(
        _env_file=None, mode="demo", db=tmp_path / "cli.db", var_dir=tmp_path / "var", asr="off",
        secret_key="s", phone_key=Fernet.generate_key().decode(), **kw,
    )  # fmt: skip


def test_seed_demo_writes_the_data_once_and_says_so_the_second_time(tmp_path, monkeypatch, capsys, clock_t0):
    s = settings_in(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    assert main.cli(["seed-demo"]) == 0
    assert "demo data written" in capsys.readouterr().out
    assert main.cli(["seed-demo"]) == 2
    assert "Reset the demo" in capsys.readouterr().err


def test_reset_demo_rebuilds_and_clears_the_stored_clock_jump(tmp_path, monkeypatch, capsys, clock_t0):
    from chaarteeke.db import Database

    s = settings_in(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    assert main.cli(["seed-demo"]) == 0
    db = Database(s.db)
    with db.transaction() as conn:
        conn.execute("INSERT INTO kv(key, value) VALUES ('demo_clock_offset_s', '86400')")
    db.close()
    assert main.cli(["reset-demo"]) == 0
    db = Database(s.db)
    assert db.one("SELECT 1 FROM kv WHERE key = 'demo_clock_offset_s'") is None
    db.close()


def test_seed_demo_refuses_a_pilot_server(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: settings_in(
            tmp_path,
        ).model_copy(update={"mode": "pilot"}),
    )
    assert main.cli(["seed-demo"]) == 2
    assert "only in CT_MODE=demo" in capsys.readouterr().err


def test_purge_now_runs_the_deletion_jobs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main, "get_settings", lambda: settings_in(tmp_path))
    assert main.cli(["purge-now"]) == 0
    assert "phones=0" in capsys.readouterr().out


# ------------------------------------------- a server already running on the database
@pytest.fixture
def real_clock():
    """The system clock, as a command-line process has it (`serve` installs its own, which is real time too)."""
    with clock.use_clock(clock.SystemClock()) as c:
        yield c


def hold_lock(db_path, *, seconds_ago: float = 2, holder: str = "sched-running") -> None:
    """Another process's scheduler lock on this database, last beaten `seconds_ago` ago (in the clock now)."""
    from datetime import timedelta

    from chaarteeke.db import Database
    from chaarteeke.scheduler import LOCK_NAME

    db = Database(db_path)
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO locks(name, holder, heartbeat_at) VALUES (?,?,?)",
            (LOCK_NAME, holder, clock.iso(clock.now() - timedelta(seconds=seconds_ago))),
        )
    db.close()


RUNNING = (
    "A server is running on this database: use the admin Reset demo data button, or stop the server first."
)


def test_reset_demo_refuses_while_a_server_holds_the_scheduler(tmp_path, monkeypatch, capsys, clock_t0):
    from chaarteeke.db import Database

    s = settings_in(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    assert main.cli(["seed-demo"]) == 0
    capsys.readouterr()
    db = Database(s.db)
    before = db.one("SELECT value FROM kv WHERE key = 'demo_seed'")["value"]
    db.close()
    hold_lock(s.db)
    assert main.cli(["reset-demo"]) == 2
    out = capsys.readouterr()
    assert RUNNING in out.err and out.out == ""
    db = Database(s.db)
    assert db.one("SELECT value FROM kv WHERE key = 'demo_seed'")["value"] == before  # nothing was wiped
    db.close()


def test_seed_demo_refuses_too_even_on_an_empty_database(tmp_path, monkeypatch, capsys, clock_t0):
    from chaarteeke.api.app import build_context

    s = settings_in(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    ctx = build_context(s)  # the schema, no data
    ctx.db.close()
    hold_lock(s.db)
    assert main.cli(["seed-demo"]) == 2
    assert RUNNING in capsys.readouterr().err
    db = build_context(s).db
    assert db.one("SELECT COUNT(*) AS n FROM clinics")["n"] == 0
    db.close()


def test_a_stale_lock_does_not_stop_a_reset(tmp_path, monkeypatch, capsys, clock_t0):
    s = settings_in(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    assert main.cli(["seed-demo"]) == 0
    hold_lock(s.db, seconds_ago=3600)  # a server that stopped an hour ago
    assert main.cli(["reset-demo"]) == 0
    assert "demo data written" in capsys.readouterr().out


def test_a_server_with_a_jumped_demo_clock_is_still_seen_as_running(
    tmp_path, monkeypatch, capsys, real_clock
):
    """The server stamps its beat in demo time. The command line reads the stored jump, so a beat a day ahead
    of real time is a beat from now, not a stale one a day away."""
    from datetime import timedelta

    from chaarteeke.db import Database
    from chaarteeke.scheduler import LOCK_NAME

    s = settings_in(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    assert main.cli(["seed-demo"]) == 0
    capsys.readouterr()
    day = 86400
    db = Database(s.db)
    with db.transaction() as conn:
        conn.execute("INSERT INTO kv(key, value) VALUES ('demo_clock_offset_s', ?)", (str(day),))
        conn.execute(
            "INSERT OR REPLACE INTO locks(name, holder, heartbeat_at) VALUES (?,?,?)",
            (LOCK_NAME, "sched-running", clock.iso(clock.SystemClock().now() + timedelta(seconds=day - 3))),
        )
    db.close()
    assert main.cli(["reset-demo"]) == 2
    assert RUNNING in capsys.readouterr().err


def test_serve_refuses_in_demo_when_another_server_holds_the_scheduler(
    tmp_path, monkeypatch, capsys, real_clock
):
    import uvicorn

    s = settings_in(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    assert main.cli(["seed-demo"]) == 0
    capsys.readouterr()
    hold_lock(s.db)
    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: ran.append(k))
    assert main.cli(["serve"]) == 2
    err = capsys.readouterr().err
    assert "Another server is already running on this database" in err and "Stop it first" in err
    assert ran == []  # nothing was served


def test_serve_starts_when_nobody_holds_the_scheduler_and_passes_the_real_address(
    tmp_path, monkeypatch, capsys, real_clock
):
    import uvicorn

    s = settings_in(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    assert main.cli(["seed-demo"]) == 0
    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **k: ran.append((app, k)))
    assert main.cli(["serve", "--host", "10.1.2.3", "--port", "9191"]) == 0
    app, kw = ran[0]
    assert (kw["host"], kw["port"]) == ("10.1.2.3", 9191)
    assert (
        app.state.ctx.settings.port == 9191 and app.state.ctx.settings.host == "10.1.2.3"
    )  # the QR list reads it
    app.state.ctx.db.close()


def test_serve_does_not_refuse_a_pilot_server_that_has_a_standby(tmp_path, monkeypatch, capsys, real_clock):
    """Only the demo refuses: a pilot may keep a second server waiting to take over."""
    import uvicorn

    s = settings_in(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    assert main.cli(["seed-demo"]) == 0
    hold_lock(s.db)
    pilot = s.model_copy(
        update={"mode": "pilot", "secret_key": "k" * 40, "public_url": "https://ct.example.org"}
    )
    monkeypatch.setattr(main, "get_settings", lambda: pilot)
    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **k: ran.append(app))
    assert main.cli(["serve"]) == 0 and ran
    ran[0].state.ctx.db.close()


# ------------------------------------------------------------------- create-admin
def pilot_settings(tmp_path, **kw) -> Settings:
    from cryptography.fernet import Fernet

    return Settings(
        _env_file=None, mode="pilot", db=tmp_path / "pilot.db", var_dir=tmp_path / "var", asr="off",
        secret_key="s" * 40, phone_key=Fernet.generate_key().decode(), public_url="https://ct.example.org", **kw,
    )  # fmt: skip


def answers(monkeypatch, username: str, *passwords: str):
    """The terminal: the username typed, then each password asked for. Returns the list of prompts shown."""
    shown: list[str] = []
    typed = iter(passwords)
    monkeypatch.setattr("builtins.input", lambda prompt="": shown.append(prompt) or username)
    monkeypatch.setattr(main.getpass, "getpass", lambda prompt="": shown.append(prompt) or next(typed))
    return shown


def test_create_admin_makes_the_first_administrator_and_audits_it(tmp_path, monkeypatch, capsys):
    from chaarteeke.api import auth
    from chaarteeke.api.app import build_context

    s = pilot_settings(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    password = "a-long-secret-phrase-9"
    answers(monkeypatch, "firstadmin", password, password)
    assert main.cli(["create-admin"]) == 0
    out = capsys.readouterr()
    assert password not in out.out and password not in out.err  # never printed
    ctx = build_context(s)
    row = ctx.db.one("SELECT * FROM staff WHERE username = 'firstadmin'")
    assert row["role"] == "admin" and row["active"] == 1 and row["is_demo"] == 0
    assert password not in row["password_hash"] and auth.verify_password(password, row["password_hash"])
    audit_row = ctx.db.one("SELECT * FROM audit WHERE action = 'staff.create'")
    assert audit_row["actor"] == "system:cli" and audit_row["object_id"] == row["id"]
    assert password not in (audit_row["detail_json"] or "") and "firstadmin" in audit_row["detail_json"]
    token, _ = auth.sign_in(ctx, "firstadmin", password)  # and the login works
    assert auth.principal_for(ctx, token).role == "admin"
    ctx.db.close()


def test_create_admin_refuses_once_an_administrator_exists_and_does_not_ask_for_a_password(
    tmp_path, monkeypatch, capsys
):
    s = pilot_settings(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    pw = "a-long-secret-phrase-9"
    answers(monkeypatch, "firstadmin", pw, pw)
    assert main.cli(["create-admin"]) == 0
    capsys.readouterr()
    shown = answers(monkeypatch, "second", "another-long-phrase-1", "another-long-phrase-1")
    assert main.cli(["create-admin"]) == 2
    assert "An administrator already exists" in capsys.readouterr().err
    assert shown == []  # refused before asking for anything
    from chaarteeke.api.app import build_context

    ctx = build_context(s)
    assert ctx.db.one("SELECT COUNT(*) AS n FROM staff")["n"] == 1
    ctx.db.close()


def test_create_admin_refuses_in_a_demo_database_that_has_its_administrator(
    tmp_path, monkeypatch, capsys, clock_t0
):
    s = settings_in(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    assert main.cli(["seed-demo"]) == 0
    capsys.readouterr()
    answers(monkeypatch, "extra", "a-long-secret-phrase-9", "a-long-secret-phrase-9")
    assert main.cli(["create-admin"]) == 2
    assert "An administrator already exists" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("username", "first", "second", "complaint"),
    [
        ("firstadmin", "short", "short", "at least 10 characters"),
        ("firstadmin", "a-long-secret-phrase-9", "a-different-phrase-9", "not the same"),
        ("Bad Name!", "a-long-secret-phrase-9", "a-long-secret-phrase-9", "3 to 32 lower-case"),
    ],
)
def test_create_admin_refuses_a_weak_password_a_mismatch_and_a_bad_username(
    tmp_path, monkeypatch, capsys, username, first, second, complaint
):
    from chaarteeke.api.app import build_context

    s = pilot_settings(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    answers(monkeypatch, username, first, second)
    assert main.cli(["create-admin"]) == 2
    out = capsys.readouterr()
    assert complaint in out.err and first not in out.err + out.out
    ctx = build_context(s)
    assert ctx.db.one("SELECT COUNT(*) AS n FROM staff")["n"] == 0
    ctx.db.close()


def test_the_speech_model_is_ready_only_once_its_weights_are_loaded(monkeypatch):
    """A SIP end-to-end run with the model on found the status saying 'loaded' while the weights were still
    unread: they used to load on the first recording. 'ready' now means the weights are in memory."""
    import threading
    from types import SimpleNamespace

    from chaarteeke.api import app as app_module
    from chaarteeke.speech import asr

    release, started = threading.Event(), threading.Event()

    class Slow:
        loads = 0

        def load(self):
            started.set()
            release.wait(5)
            Slow.loads += 1

    fake = Slow()
    monkeypatch.setattr(asr, "load_transcriber", lambda settings: fake)
    ctx = SimpleNamespace(settings=SimpleNamespace(asr="indicconformer"), extras={}, transcriber=None)
    app_module._load_asr(ctx)
    assert started.wait(5)
    assert ctx.extras["asr_state"] == "loading"  # the folder is checked, the weights are not in yet
    assert ctx.transcriber is fake  # a recording that arrives now waits for the weights, it is not refused
    release.set()
    for _ in range(100):
        if ctx.extras["asr_state"] != "loading":
            break
        threading.Event().wait(0.05)
    assert ctx.extras["asr_state"] == "ready" and Slow.loads == 1


def test_a_speech_model_that_cannot_load_its_weights_is_failed_not_ready(monkeypatch):
    import threading
    from types import SimpleNamespace

    from chaarteeke.api import app as app_module
    from chaarteeke.speech import asr

    class Broken:
        def load(self):
            raise asr.SpeechError("the encoder will not open")

    monkeypatch.setattr(asr, "load_transcriber", lambda settings: Broken())
    ctx = SimpleNamespace(settings=SimpleNamespace(asr="indicconformer"), extras={}, transcriber=None)
    app_module._load_asr(ctx)
    for _ in range(100):
        if ctx.extras["asr_state"] != "loading":
            break
        threading.Event().wait(0.05)
    assert ctx.extras["asr_state"] == "failed" and "will not open" in ctx.extras["asr_error"]
    assert ctx.transcriber is None  # recordings are refused with the reason (places/review.py), not retried
