"""System status (red only for real faults) and the QR code."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from api_support import Who
from fastapi.testclient import TestClient

from chaarteeke import audit, clock
from chaarteeke.api import auth, routes_system
from chaarteeke.policy import load_policy
from chaarteeke.scheduler import LOCK_NAME


def checks(client, who) -> dict[str, dict]:
    res = client.get("/api/system/status", headers=who.read)
    assert res.status_code == 200, res.text
    return {c["name"]: c for c in res.json()["checks"]}


@pytest.fixture
def mono(monkeypatch):
    """The real-time stopwatch the deletion-job check uses, under the test's control."""
    now = [1000.0]
    monkeypatch.setattr(routes_system, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def purge_after_waiting(client, who, mono) -> dict:
    """Read the deletion check, let ten real minutes pass, read it again: what a page polling would see."""
    checks(client, who)
    mono[0] += 600
    return checks(client, who)["purge"]


def beat(ctx, seconds_ago: float, holder: str | None = None) -> None:
    """The scheduler lock, last beaten `seconds_ago` ago, by this server's own scheduler unless `holder` is given."""
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO locks(name, holder, heartbeat_at) VALUES (?, ?, ?)",
            (
                LOCK_NAME,
                holder or ctx.scheduler.holder,
                clock.iso(clock.now() - timedelta(seconds=seconds_ago)),
            ),
        )


def test_status_has_the_mode_transport_policy_and_missing_prompts(client, people):
    body = client.get("/api/system/status", headers=people["admin"].read).json()
    assert body["mode"] == "demo" and body["telephony"] == "sim"
    assert body["policy_version"] == load_policy().version == "2026.10.9"
    assert body["prompts_missing"] == {"hi": [], "doi": [], "ur": []}
    assert {c["name"] for c in body["checks"]} >= {
        "database",
        "scheduler",
        "telephony",
        "speech",
        "purge",
        "alerts",
    }


def test_every_signed_in_person_may_read_status_and_nobody_else(client, people):
    for name, who in people.items():
        res = client.get("/api/system/status", headers=who.read)
        assert res.status_code == (401 if name == "out" else 200), name


def test_the_database_check(client, people):
    db = checks(client, people["abc"])["database"]
    assert db["ok"] is True and db["alarm"] is False


def test_a_scheduler_that_beats_is_ok_and_a_stopped_one_is_red(client, ctx, people):
    ctx.settings = ctx.settings.model_copy(update={"scheduler": True})
    beat(ctx, 2)
    assert checks(client, people["admin"])["scheduler"]["ok"] is True
    beat(ctx, ctx.policy["scheduler"]["lock_stale_s"] + 5)
    stale = checks(client, people["admin"])["scheduler"]
    assert stale["ok"] is False and stale["alarm"] is True and "stopped" in stale["detail"]


def test_no_scheduler_row_at_all_is_red_when_the_scheduler_should_run(make_app, clock_t0, people_for):
    app = make_app(scheduler=True)
    clock.install(clock_t0)
    client = TestClient(app)
    status = checks(client, people_for(app, "admin"))["scheduler"]
    assert (
        status["ok"] is False and status["alarm"] is True and "No scheduler has started" in status["detail"]
    )


def test_a_scheduler_switched_off_is_said_plainly_and_is_not_red(client, people):
    off = checks(client, people["admin"])["scheduler"]
    assert off["ok"] is False and off["alarm"] is False and "switched off" in off["detail"]


def test_the_speech_model_states(client, ctx, people):
    off = checks(client, people["admin"])["speech"]
    assert off["alarm"] is False and off["detail"].startswith("No transcript: speech model off")
    ctx.settings = ctx.settings.model_copy(update={"asr": "indicconformer"})
    ctx.extras["asr_state"] = "loading"
    loading = checks(client, people["admin"])["speech"]
    assert loading["ok"] is False and loading["alarm"] is False
    ctx.extras["asr_state"] = "ready"
    assert checks(client, people["admin"])["speech"]["ok"] is True
    ctx.extras.update(asr_state="failed", asr_error="FileNotFoundError: no model")
    failed = checks(client, people["admin"])["speech"]
    assert failed["alarm"] is True and "FileNotFoundError: no model" in failed["detail"]


def test_telephony_is_unstarted_up_or_red(client, ctx, people):
    from chaarteeke.telephony.model import TelephonyConnectionError

    assert checks(client, people["admin"])["telephony"]["detail"].endswith("is not started")

    class Runner:
        _down = None

    ctx.runner = Runner()
    ctx.extras["telephony_up"] = True
    up = checks(client, people["admin"])["telephony"]
    assert up["ok"] is True and "SIMULATOR" in up["detail"]
    ctx.runner._down = TelephonyConnectionError("ARI event socket closed")
    down = checks(client, people["admin"])["telephony"]
    assert down["ok"] is False and down["alarm"] is True and "ARI event socket closed" in down["detail"]


def test_missing_voice_is_red_only_for_a_language_in_use(make_app, clock_t0, people_for):
    app = make_app(voice=False, seeded=False)
    ctx = app.state.ctx
    client = TestClient(app)
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO staff(id, username, display_name, role, password_hash) VALUES ('stf_a','boss','Boss','admin','x')"
        )
        conn.execute(
            "INSERT INTO clinics(id, name, short_name, default_regimen, default_lang, arv_status, visit_time)"
            " VALUES ('cln_a','A','A','id_trc','hi','confirmed','09:00')"
        )
    from chaarteeke.api import auth

    token, _ = auth.create_session(ctx, staff_id="stf_a", role="admin", visitor_label=None)
    from api_support import Who

    who = Who(token, None)
    status = checks(client, who)
    assert status["voice_hi"]["ok"] is False and status["voice_hi"]["alarm"] is True
    assert "patients use this language" in status["voice_hi"]["detail"]
    assert status["voice_ur"]["ok"] is False and status["voice_ur"]["alarm"] is False
    body = client.get("/api/system/status", headers=who.read).json()
    assert body["prompts_missing"]["hi"] and body["prompts_missing"]["ur"]


def test_the_deletion_job_is_ok_after_its_daily_time_and_red_before_it(client, ctx, people, clock_t0, mono):
    with ctx.db.transaction() as conn:
        conn.execute("DELETE FROM kv WHERE key = 'last_purge_run'")
    ctx.settings = ctx.settings.model_copy(update={"scheduler": True})
    assert purge_after_waiting(client, people["admin"], mono)["alarm"] is True
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES ('last_purge_run', ?)",
            (clock.iso(clock.now() - timedelta(minutes=1)),),
        )
    assert checks(client, people["admin"])["purge"]["ok"] is True
    with ctx.db.transaction() as conn:
        conn.execute(
            "UPDATE kv SET value = ? WHERE key = 'last_purge_run'",
            (clock.iso(clock.now() - timedelta(days=3)),),
        )
    stale = purge_after_waiting(client, people["admin"], mono)
    assert stale["ok"] is False and stale["alarm"] is True and "before its daily time" in stale["detail"]


def test_a_sick_dog_alert_nobody_has_seen_is_the_alarm(client, ctx, people):
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE alerts SET first_seen_by_vet_at = created_at WHERE first_seen_by_vet_at IS NULL")
    assert checks(client, people["abc"])["alerts"]["alarm"] is False
    minutes = ctx.policy["alerts"]["unseen_by_vet_minutes"]
    with ctx.db.transaction() as conn:
        conn.execute(
            "UPDATE alerts SET status='new', first_seen_by_vet_at = NULL, created_at = ? WHERE id = (SELECT id FROM alerts LIMIT 1)",
            (clock.iso(clock.now() - timedelta(minutes=minutes + 1)),),
        )
    alarm = checks(client, people["abc"])["alerts"]
    assert alarm["ok"] is False and alarm["alarm"] is True and f"over {minutes} minutes" in alarm["detail"]
    assert "ward" not in alarm["detail"].lower()
    client.get("/api/alerts", headers=people["vet"].read)  # a vet opens the list
    assert checks(client, people["abc"])["alerts"]["alarm"] is False


def test_a_kept_session_key_is_said_to_the_administrator(make_app, clock_t0, people_for):
    app = make_app(seeded=True, secret_key="")
    status = checks(TestClient(app), people_for(app, "admin"))
    assert "session_key" in status and "stay signed in across a restart" in status["session_key"]["detail"]


def test_a_given_session_key_is_not_mentioned(client, people):
    assert "session_key" not in checks(client, people["admin"])


# ------------------------------------------------------------------------- QR
def test_the_qr_is_an_svg_for_a_link_on_this_server(client):
    res = client.get("/api/phone/qr.svg", params={"url": "http://127.0.0.1:8080/phone"})
    assert res.status_code == 200 and res.headers["content-type"] == "image/svg+xml"
    assert res.text.lstrip().startswith("<svg") and "</svg>" in res.text


def test_the_qr_refuses_links_that_are_not_on_this_server(client):
    for url in (
        "http://evil.example/phone",
        "https://127.0.0.1.evil.example/phone",
        "http://127.0.0.1:9999/phone",  # not the port this server listens on
        "javascript:alert(1)",
        "ftp://127.0.0.1:8080/phone",
        "http://user:pass@127.0.0.1:8080/phone",
        "//127.0.0.1:8080/phone",
        "127.0.0.1:8080/phone",
        "",
    ):
        res = client.get("/api/phone/qr.svg", params={"url": url})
        assert res.status_code == 400 and res.json()["error"] == "bad_request", url


def test_the_host_header_of_the_request_cannot_widen_the_qr_allow_list(client):
    """The allow-list comes from the settings. A request that says it is for evil.example gets no QR for it."""
    res = client.get(
        "/api/phone/qr.svg", params={"url": "http://evil.example/phone"}, headers={"host": "evil.example"}
    )
    assert res.status_code == 400
    ok = client.get(
        "/api/phone/qr.svg", params={"url": "http://localhost:8080/phone"}, headers={"host": "evil.example"}
    )
    assert ok.status_code == 200  # this computer, by either name, as configured


def test_the_qr_accepts_the_public_url_host(make_app, clock_t0):
    app = make_app(public_url="https://demo.example.org/")
    client = TestClient(app)
    assert (
        client.get("/api/phone/qr.svg", params={"url": "https://demo.example.org/phone"}).status_code == 200
    )
    assert (
        client.get("/api/phone/qr.svg", params={"url": "https://other.example.org/phone"}).status_code == 400
    )


def test_the_qr_allow_list_follows_the_configured_host_and_port(make_app, clock_t0):
    app = make_app(host="192.168.1.20", port=9090)
    client = TestClient(app)
    assert (
        client.get("/api/phone/qr.svg", params={"url": "http://192.168.1.20:9090/phone"}).status_code == 200
    )
    assert (
        client.get("/api/phone/qr.svg", params={"url": "http://192.168.1.20:8080/phone"}).status_code == 400
    )
    assert client.get("/api/phone/qr.svg", params={"url": "http://127.0.0.1:9090/phone"}).status_code == 400
    everywhere = TestClient(make_app(host="0.0.0.0", port=9090))  # noqa: S104
    assert (
        everywhere.get("/api/phone/qr.svg", params={"url": "http://127.0.0.1:9090/phone"}).status_code == 200
    )


def test_the_qr_needs_a_short_url(client):
    assert client.get("/api/phone/qr.svg").status_code == 400
    long = "http://127.0.0.1:8080/phone?" + "a" * 400
    assert client.get("/api/phone/qr.svg", params={"url": long}).status_code == 400


def test_a_demo_clock_jump_over_the_deletion_time_is_not_red_until_the_job_stays_overdue(
    client, ctx, clock_t0, mono, logins
):
    ctx.settings = ctx.settings.model_copy(update={"scheduler": True})
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES ('last_purge_run', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (clock.iso(clock.now()),),
        )
    clock_t0.advance(timedelta(days=1))  # the presenter jumps a day; the job has not had its round yet
    admin = Who(*auth.sign_in(ctx, "admin", logins["admin"]))  # a day on, the earlier logins have run out
    jumped = checks(client, admin)["purge"]
    assert jumped["ok"] is False and jumped["alarm"] is False
    mono[0] += ctx.policy["scheduler"]["purge_every_s"]  # one round later: still inside the allowance
    assert checks(client, admin)["purge"]["alarm"] is False
    with ctx.db.transaction() as conn:  # the round runs
        conn.execute("UPDATE kv SET value = ? WHERE key = 'last_purge_run'", (clock.iso(clock.now()),))
    assert checks(client, admin)["purge"]["ok"] is True
    clock_t0.advance(timedelta(days=1))  # the next jump, and this time the job never runs
    admin = Who(*auth.sign_in(ctx, "admin", logins["admin"]))
    checks(client, admin)
    mono[0] += 3 * ctx.policy["scheduler"]["purge_every_s"]
    assert checks(client, admin)["purge"]["alarm"] is True


def test_every_check_carries_a_stable_code_and_its_params(client, ctx, people):
    ctx.settings = ctx.settings.model_copy(update={"scheduler": True})
    beat(ctx, ctx.policy["scheduler"]["lock_stale_s"] + 5)
    by = checks(client, people["admin"])
    assert all(c["code"] and isinstance(c["params"], dict) for c in by.values())
    stale = by["scheduler"]
    assert stale["code"] == "scheduler.stopped" and stale["params"]["seconds"] >= ctx.policy["scheduler"]["lock_stale_s"]  # fmt: skip
    assert by["database"]["code"] == "database.ok" and by["speech"]["code"] == "speech.off"
    assert by["telephony"]["code"] == "telephony.not_started" and by["telephony"]["params"] == {"transport": "sim"}  # fmt: skip
    assert by["voice_hi"]["code"] == "voice.complete" and by["voice_hi"]["params"] == {"lang": "hi"}
    ctx.extras.update(asr_state="failed", asr_error="no model")
    ctx.settings = ctx.settings.model_copy(update={"asr": "indicconformer"})
    failed = checks(client, people["admin"])["speech"]
    assert failed["code"] == "speech.failed" and failed["params"] == {"reason": "no model"}
    assert "no model" in failed["detail"]  # the English message stays


@pytest.mark.parametrize(
    ("ago_s", "text", "unit", "n"),
    [(1, "1 second ago", "second", 1), (30, "30 seconds ago", "second", 30), (120, "2 minutes ago", "minute", 2),
     (3600, "60 minutes ago", "minute", 60), (5400, "1 hour ago", "hour", 1), (7300, "2 hours ago", "hour", 2)],
)  # fmt: skip
def test_the_deletion_job_detail_pluralises_its_unit(client, ctx, people, ago_s, text, unit, n):
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES ('last_purge_run', ?)",
            (clock.iso(clock.now() - timedelta(seconds=ago_s)),),
        )
    purge = checks(client, people["admin"])["purge"]
    assert f"last ran {text}" in purge["detail"]
    assert purge["params"] == {"n": n, "unit": unit}


def test_one_of_a_unit_is_singular_and_the_rest_plural():
    assert routes_system._count(1, "second") == "1 second"
    assert routes_system._count(0, "minute") == "0 minutes" and routes_system._count(2, "hour") == "2 hours"


# ------------------------------------------------------- a second server on the same database
def test_a_scheduler_held_by_another_server_is_a_red_alarm_that_says_so(client, ctx, people):
    ctx.settings = ctx.settings.model_copy(update={"scheduler": True})
    beat(ctx, 2, holder="sched-another-server")
    other = checks(client, people["admin"])["scheduler"]
    assert other["ok"] is False and other["alarm"] is True and other["code"] == "scheduler.foreign"
    assert other["detail"] == (
        "Another server on this database holds the scheduler; calls from this server will not be placed"
    )
    beat(ctx, 2)  # this server's own scheduler holds it again
    assert checks(client, people["admin"])["scheduler"]["ok"] is True


def test_a_stale_lock_of_another_server_is_the_stopped_alarm_not_the_foreign_one(client, ctx, people):
    ctx.settings = ctx.settings.model_copy(update={"scheduler": True})
    beat(ctx, ctx.policy["scheduler"]["lock_stale_s"] + 5, holder="sched-another-server")
    stale = checks(client, people["admin"])["scheduler"]
    assert stale["code"] == "scheduler.stopped" and stale["alarm"] is True


def test_the_foreign_lock_is_seen_by_two_contexts_on_one_database(make_app, clock_t0):
    """Two servers on one file: the one that took the lock first holds it, the other is told so."""
    import asyncio

    from chaarteeke.api.app import build_context

    first = make_app(scheduler=True)
    clock.install(clock_t0)
    ctx1 = first.state.ctx
    ctx2 = build_context(ctx1.settings)  # a second server: the same database file, its own scheduler
    assert ctx2.scheduler.holder != ctx1.scheduler.holder
    assert asyncio.run(ctx1.scheduler.tick()).ran is True  # server one takes the lock
    assert asyncio.run(ctx2.scheduler.tick()).ran is False  # server two waits
    mine = {c.name: c for c in routes_system.build_status(ctx1).checks}["scheduler"]
    theirs = {c.name: c for c in routes_system.build_status(ctx2).checks}["scheduler"]
    assert mine.code == "scheduler.running" and mine.ok is True
    assert theirs.code == "scheduler.foreign" and theirs.alarm is True
    ctx2.db.close()


# ------------------------------------------------------------ what the status tells whom
def test_a_visitor_and_staff_read_the_kind_of_failure_and_only_the_administrator_the_detail(
    client, ctx, people
):
    ctx.settings = ctx.settings.model_copy(update={"asr": "indicconformer"})
    ctx.extras.update(
        asr_state="failed",
        asr_error="FileNotFoundError: [Errno 2] No such file or directory: '/Users/someone/models/asr/encoder.onnx'",
        telephony_up=False,
        telephony_error="nothing at 10.20.30.40:8088 answered in time",
        telephony_error_class="TimeoutError",
    )
    for who in ("visitor-admin", "visitor-clinic", "clinic", "vet"):
        body = client.get("/api/system/status", headers=people[who].read)
        text = body.text
        assert "/Users" not in text and "encoder.onnx" not in text and "10.20.30.40" not in text, who
        by = {c["name"]: c for c in body.json()["checks"]}
        assert by["speech"]["detail"] == "The speech model failed to load (FileNotFoundError)"
        assert by["speech"]["params"] == {"failure": "FileNotFoundError"}
        assert by["telephony"]["detail"] == "The browser phone (SIMULATOR) is down (TimeoutError)"
        assert by["telephony"]["alarm"] is True and "reason" not in by["telephony"]["params"]
        assert "phone_reveals" not in by
    admin = checks(client, people["admin"])
    assert "/Users/someone/models/asr/encoder.onnx" in admin["speech"]["detail"]
    assert "10.20.30.40:8088" in admin["telephony"]["detail"]
    assert admin["telephony"]["params"]["reason"] == "nothing at 10.20.30.40:8088 answered in time"


# ----------------------------------------------------------- the phone-number burst alarm
def test_the_administrator_sees_an_alarm_when_a_login_hits_the_reveal_limit(client, ctx, people, clock_t0):
    ok = checks(client, people["admin"])["phone_reveals"]
    assert ok["ok"] is True and ok["alarm"] is False
    with ctx.db.transaction() as conn:
        audit.record(conn, "staff:clinic", "phone.reveal_limited", "course", "crs_x", {"limit": 30})
    burst = checks(client, people["admin"])["phone_reveals"]
    assert burst["ok"] is False and burst["alarm"] is True and burst["code"] == "reveals.burst"
    assert burst["params"] == {"count": 1, "logins": 1}
    clock_t0.advance(timedelta(hours=1, minutes=1))  # an hour on it is history
    assert checks(client, people["admin"])["phone_reveals"]["ok"] is True
