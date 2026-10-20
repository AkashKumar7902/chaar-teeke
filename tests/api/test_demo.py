"""The demo clock and the reset."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from chaarteeke import clock, enrol
from chaarteeke.api.auth import COOKIE, CSRF_HEADER


def visitor_patient(ctx) -> str:
    """What the browser phone makes: a missed call from a sim number, linked at once to the visitor desk."""
    got = enrol.record_missed_call(ctx, "sim:test-phone", "sim", clock.now())
    assert got.status == "linked"
    return got.course_id


def test_the_state_says_now_the_clock_and_the_jump_targets(client, people, moving_clock):
    res = client.get("/api/demo/state", headers=people["visitor-clinic"].read)
    assert res.status_code == 200
    body = res.json()
    assert body["jump_targets"] == ["next_call", "evening_before_next_dose", "morning_after"]
    assert body["next_call_at"] is None or body["next_call_kind"] in ("first", "reminder", "missed_dose", "duplicate_notice", "withdrawn_notice", None)  # fmt: skip


def test_a_visitor_can_jump_the_clock_to_the_next_call(client, ctx, people, moving_clock):
    visitor_patient(ctx)
    before = client.get("/api/demo/state", headers=people["visitor-clinic"].read).json()
    assert before["next_call_kind"] == "first"
    res = client.post("/api/demo/jump", json={"to": "next_call"}, headers=people["visitor-clinic"].write)
    assert res.status_code == 200, res.text
    after = res.json()
    assert after["offset_seconds"] > before["offset_seconds"]
    assert clock.now() >= clock.parse_iso(before["next_call_at"])
    assert ctx.db.one("SELECT value FROM kv WHERE key = 'demo_clock_offset_s'")["value"]
    actor = ctx.db.one("SELECT actor FROM audit WHERE action = 'demo.jump'")["actor"]
    assert actor.startswith("visitor:") and actor != "system:demo"  # the person who pressed it


def test_the_jump_to_the_evening_before_the_next_dose(client, ctx, people, moving_clock):
    visitor_patient(ctx)
    res = client.post(
        "/api/demo/jump", json={"to": "evening_before_next_dose"}, headers=people["visitor-abc"].write
    )
    assert res.status_code == 200, res.text
    local = clock.now().astimezone(clock.local_tz(ctx.settings.local_tz))
    assert local.strftime("%H:%M") == ctx.policy["calls"]["reminder_local_time"]
    assert local.date() == clock.local_today(ctx.settings.local_tz) and local > clock.parse_iso(res.json()["now"]) - timedelta(minutes=1)  # fmt: skip


def test_jumping_a_day_ahead_does_not_sign_anyone_out(client, ctx, people, moving_clock):
    visitor_patient(ctx)
    res = client.post("/api/demo/jump", json={"to": "morning_after"}, headers=people["visitor-abc"].write)
    assert res.status_code == 200
    assert clock.now() - clock.parse_iso(res.json()["now"]) < timedelta(seconds=5)
    assert res.json()["offset_seconds"] > ctx.policy["visitor"]["session_hours"] * 3600 / 100  # it moved
    for name in ("visitor-abc", "admin", "clinic"):
        assert client.get("/api/me", headers=people[name].read).status_code == 200, name


def test_the_clock_never_goes_back(client, ctx, people, moving_clock):
    visitor_patient(ctx)
    client.post("/api/demo/jump", json={"to": "morning_after"}, headers=people["admin"].write)
    now = clock.now()
    again = client.post(
        "/api/demo/jump", json={"to": "evening_before_next_dose"}, headers=people["admin"].write
    )
    assert clock.now() >= now
    assert again.status_code in (200, 409)


def test_the_jump_says_why_it_cannot(client, people, moving_clock):
    none = client.post(
        "/api/demo/jump", json={"to": "evening_before_next_dose"}, headers=people["visitor-clinic"].write
    )
    assert none.status_code == 409 and none.json()["error"] == "no_simulator_course"
    nothing = client.post("/api/demo/jump", json={"to": "next_call"}, headers=people["visitor-clinic"].write)
    assert nothing.status_code == 409 and nothing.json()["error"] == "nothing_scheduled"
    bad = client.post("/api/demo/jump", json={"to": "yesterday"}, headers=people["visitor-clinic"].write)
    assert bad.status_code == 400 and bad.json()["error"] == "bad_request"


def test_the_jump_needs_a_login_and_the_csrf_token(client, people, moving_clock):
    assert client.post("/api/demo/jump", json={"to": "next_call"}).status_code == 401
    res = client.post("/api/demo/jump", json={"to": "next_call"}, headers=people["visitor-clinic"].read)
    assert res.status_code == 403 and res.json()["error"] == "csrf"


def test_a_clock_event_goes_out_on_a_jump(client, ctx, people, moving_clock):
    visitor_patient(ctx)
    q = ctx.bus.subscribe()
    client.post("/api/demo/jump", json={"to": "next_call"}, headers=people["visitor-clinic"].write)
    seen = []
    while not q.empty():
        seen.append(q.get_nowait())
    assert "clock" in [e.type for e in seen]


def test_the_reset_rebuilds_the_demo_and_keeps_the_administrator_signed_in(
    make_app, people_for, moving_clock
):
    app = make_app()
    clock.install(moving_clock)
    ctx = app.state.ctx
    client = TestClient(app)
    admin = people_for(app, "admin")
    with ctx.db.transaction() as conn:
        conn.execute("DELETE FROM sight_counts")  # make it different
    changed = ctx.db.one("SELECT COUNT(*) AS n FROM sight_counts")["n"]
    res = client.post("/api/demo/reset", headers=admin.write)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["csrf"] and body["counts"]["sight_counts"] > changed
    assert COOKIE in res.headers["set-cookie"]
    new_cookie = res.headers["set-cookie"].split(";")[0]
    me = client.get("/api/me", headers={"cookie": new_cookie})
    assert me.status_code == 200 and me.json()["role"] == "admin" and me.json()["csrf"] == body["csrf"]
    again = client.post("/api/demo/reset", headers={"cookie": new_cookie, CSRF_HEADER: body["csrf"]})
    assert again.status_code == 200
    assert client.get("/api/me", headers=admin.read).status_code == 401  # the old login ended


def test_a_reset_makes_new_staff_passwords_and_the_response_never_carries_them(
    make_app, people_for, moving_clock, logins
):
    from api_support import read_logins

    app = make_app()
    clock.install(moving_clock)
    ctx = app.state.ctx
    client = TestClient(app)
    admin = people_for(app, "admin")
    res = client.post("/api/demo/reset", headers=admin.write)
    assert res.status_code == 200
    fresh = read_logins(ctx.settings.var_dir / "demo_logins.txt")
    assert set(fresh) == set(logins) and not set(fresh.values()) & set(logins.values())
    assert "logins" not in res.json() and not any(p in res.text for p in fresh.values())
    now = client.post("/api/auth/signin", json={"username": "vet", "password": fresh["vet"]})
    old = client.post("/api/auth/signin", json={"username": "vet", "password": logins["vet"]})
    assert now.status_code == 200 and old.status_code == 401


def test_a_reset_keeps_a_visitors_login(make_app, people_for, moving_clock):
    from api_support import Who

    from chaarteeke.api import auth

    app = make_app()
    clock.install(moving_clock)
    ctx = app.state.ctx
    client = TestClient(app)
    token, csrf = auth.enter_as_visitor(ctx, "abc")
    admin = people_for(app, "admin")
    assert client.post("/api/demo/reset", headers=admin.write).status_code == 200
    assert client.get("/api/me", headers=Who(token, csrf).read).status_code == 200


def test_the_reset_puts_the_demo_clock_back_on_real_time(make_app, people_for, moving_clock):
    app = make_app()
    clock.install(moving_clock)
    ctx = app.state.ctx
    client = TestClient(app)
    moving_clock.jump_to(clock.now() + timedelta(days=2))
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO kv(key, value) VALUES ('demo_clock_offset_s', '172800')")
    admin = people_for(app, "admin")
    assert client.post("/api/demo/reset", headers=admin.write).status_code == 200
    assert abs(moving_clock.offset.total_seconds()) < 5
    assert ctx.db.one("SELECT value FROM kv WHERE key = 'demo_clock_offset_s'") is None


def test_the_seed_command_prints_each_staff_password_once_and_keeps_them_in_a_private_file(
    monkeypatch, tmp_path, phone_key, capsys, clock_t0
):
    import stat

    from api_support import read_logins

    from chaarteeke import main
    from chaarteeke.config import DATA_DIR, Settings

    settings = Settings(
        _env_file=None, mode="demo", db=tmp_path / "cli.db", var_dir=tmp_path / "var", asr="off",
        secret_key="cli", phone_key=phone_key, data_dir=DATA_DIR,
    )  # fmt: skip
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    assert main.cli(["seed-demo"]) == 0
    out = capsys.readouterr().out
    path = tmp_path / "var" / "demo_logins.txt"
    logins = read_logins(path)
    assert set(logins) == {"clinic", "abc", "vet", "oversight", "reviewer", "admin"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for password in logins.values():
        assert out.count(password) == 1  # printed once
    assert "demo_logins.txt" in out


# ------------------------------------------------------------ who may move the clock
def jump(client, who, to="evening_before_next_dose"):
    return client.post("/api/demo/jump", json={"to": to}, headers=who.write)


def jump_actors(ctx) -> list[str]:
    return [
        r["actor"] for r in ctx.db.query("SELECT actor FROM audit WHERE action = 'demo.jump' ORDER BY seq")
    ]


def test_every_jump_is_audited_under_the_person_who_pressed_it(client, ctx, people, moving_clock):
    visitor_patient(ctx)
    assert jump(client, people["visitor-abc"]).status_code == 200
    assert jump(client, people["clinic"]).status_code == 200
    visitor = ctx.db.one(
        "SELECT visitor_label FROM sessions WHERE role = 'visitor' AND visitor_label LIKE '%:abc'"
    )
    assert jump_actors(ctx) == [f"visitor:{visitor['visitor_label'].split(':')[0]}", "staff:clinic"]
    assert "system:demo" not in jump_actors(ctx)


def test_a_visitor_has_a_limited_number_of_jumps_from_policy(client, ctx, people, moving_clock, monkeypatch):
    monkeypatch.setitem(ctx.policy.raw["visitor"], "clock_jumps_per_session", 2)
    visitor_patient(ctx)
    who = people["visitor-abc"]
    state = client.get("/api/demo/state", headers=who.read).json()
    assert state["jumps_left"] == 2 and state["jump_targets"]
    assert jump(client, who).status_code == 200
    assert jump(client, who).status_code == 200
    third = jump(client, who)
    assert third.status_code == 429 and third.json()["error"] == "jump_limit"
    after = client.get("/api/demo/state", headers=who.read).json()
    assert after["jumps_left"] == 0 and after["jump_targets"] == []  # the page has nothing to offer
    other = jump(client, people["visitor-clinic"])  # another visitor has their own count
    assert other.status_code in (200, 409)
    assert jump(client, people["clinic"]).status_code in (200, 409)  # staff have no cap


def test_all_visitors_together_have_a_cap_per_hour_of_real_time(
    client, ctx, people, moving_clock, monkeypatch
):
    monkeypatch.setitem(ctx.policy.raw["visitor"], "clock_jumps_per_hour", 2)
    visitor_patient(ctx)
    assert jump(client, people["visitor-abc"]).status_code == 200
    assert jump(client, people["visitor-clinic"]).status_code == 200
    refused = jump(client, people["visitor-oversight"])
    assert refused.status_code == 429 and refused.json()["error"] == "jump_limit"
    assert int(refused.headers["retry-after"]) > 0
    assert jump(client, people["admin"]).status_code in (200, 409)  # staff are not counted or held


def test_the_cap_counts_real_time_not_the_jumped_clock(client, ctx, people, moving_clock, monkeypatch):
    """A jump of hours must not make the earlier jumps look old."""
    monkeypatch.setitem(ctx.policy.raw["visitor"], "clock_jumps_per_hour", 1)
    visitor_patient(ctx)
    assert jump(client, people["visitor-abc"], "morning_after").status_code == 200
    assert clock.now() > clock.parse_iso("2026-10-20T00:00:00.000Z")  # the clock is now a day ahead
    assert jump(client, people["visitor-clinic"]).status_code == 429


def test_a_refused_or_failed_jump_costs_nothing(client, ctx, people, moving_clock, monkeypatch):
    monkeypatch.setitem(ctx.policy.raw["visitor"], "clock_jumps_per_session", 1)
    who = people["visitor-abc"]
    assert jump(client, who).status_code == 409  # no demo patient yet
    visitor_patient(ctx)
    assert jump(client, who).status_code == 200  # the one jump is still there


def test_the_administrator_can_lock_the_clock_for_visitors_only(client, ctx, people, moving_clock):
    visitor_patient(ctx)
    admin, visitor = people["admin"], people["visitor-abc"]
    locked = client.post("/api/demo/clock-lock", json={"locked": True}, headers=admin.write)
    assert locked.status_code == 200 and locked.json()["clock_locked"] is True
    refused = jump(client, visitor)
    assert refused.status_code == 403 and refused.json()["error"] == "clock_locked"
    seen = client.get("/api/demo/state", headers=visitor.read).json()
    assert seen["clock_locked"] is True and seen["jump_targets"] == []
    assert jump(client, people["clinic"]).status_code == 200  # the presenter, signed in, still can
    assert client.get("/api/demo/state", headers=people["clinic"].read).json()["jump_targets"]
    unlocked = client.post("/api/demo/clock-lock", json={"locked": False}, headers=admin.write)
    assert unlocked.json()["clock_locked"] is False
    assert jump(client, visitor).status_code == 200
    rows = ctx.db.query("SELECT actor, detail_json FROM audit WHERE action = 'demo.clock_lock' ORDER BY seq")
    assert [(r["actor"], r["detail_json"]) for r in rows] == [
        ("staff:admin", '{"locked": true}'),
        ("staff:admin", '{"locked": false}'),
    ]


def test_the_lock_survives_a_reset_and_is_the_administrators_alone(client, ctx, people, moving_clock):
    for name in ("clinic", "abc", "vet", "oversight", "reviewer", "visitor-admin"):
        res = client.post("/api/demo/clock-lock", json={"locked": True}, headers=people[name].write)
        assert res.status_code == 403, name
    assert client.post("/api/demo/clock-lock", json={"locked": True}).status_code == 401
    client.post("/api/demo/clock-lock", json={"locked": True}, headers=people["admin"].write)
    client.post("/api/demo/reset", headers=people["admin"].write)
    assert ctx.db.one("SELECT 1 FROM kv WHERE key = 'demo_clock_locked'") is not None


def test_back_to_now_puts_the_clock_on_real_time_and_keeps_the_data(client, ctx, people, moving_clock):
    visitor_patient(ctx)
    jump(client, people["clinic"], "morning_after")
    assert ctx.db.one("SELECT value FROM kv WHERE key = 'demo_clock_offset_s'") is not None
    courses = ctx.db.one("SELECT COUNT(*) AS n FROM courses")["n"]
    res = client.post("/api/demo/clock-now", headers=people["clinic"].write)
    assert res.status_code == 200, res.text
    assert abs(res.json()["offset_seconds"]) < 1
    assert ctx.db.one("SELECT value FROM kv WHERE key = 'demo_clock_offset_s'") is None
    assert ctx.db.one("SELECT COUNT(*) AS n FROM courses")["n"] == courses
    row = ctx.db.one("SELECT actor FROM audit WHERE action = 'demo.clock_now'")
    assert row["actor"] == "staff:clinic"


def test_back_to_now_is_for_signed_in_staff_not_visitors(client, people, moving_clock):
    assert client.post("/api/demo/clock-now").status_code == 401
    refused = client.post("/api/demo/clock-now", headers=people["visitor-abc"].write)
    assert refused.status_code == 403 and refused.json()["error"] == "read_only"
    assert client.post("/api/demo/clock-now", headers=people["clinic"].read).json()["error"] == "csrf"


def test_a_pilot_server_has_no_clock_controls(make_app, clock_t0, people_for):
    app = make_app(seeded=False, mode="pilot")
    clock.install(clock_t0)
    client = TestClient(app)
    with app.state.ctx.db.transaction() as conn:
        from chaarteeke.api import auth

        conn.execute(
            "INSERT INTO staff(id, username, display_name, role, password_hash) VALUES ('stf_a','boss','Boss','admin',?)",
            (auth.hash_password("a long password"),),
        )
    me = client.post("/api/auth/signin", json={"username": "boss", "password": "a long password"}).json()
    headers = {CSRF_HEADER: me["csrf"]}
    assert client.post("/api/demo/clock-now", headers=headers).status_code == 404
    assert client.post("/api/demo/clock-lock", json={"locked": True}, headers=headers).status_code == 404
