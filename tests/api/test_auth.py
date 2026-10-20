"""Sign in and out, the visitor entry, the public mode, CSRF, sessions, headers and error shapes."""

from __future__ import annotations

from datetime import timedelta

import pytest

from chaarteeke import clock
from chaarteeke.api import auth
from chaarteeke.api.app import CSP
from chaarteeke.api.auth import COOKIE, CSRF_HEADER


def sign_in(client, logins, username="clinic", password=None):
    """Sign in with the password the seed made for `username`, unless one is given."""
    password = logins.get(username, "unknown") if password is None else password
    return client.post("/api/auth/signin", json={"username": username, "password": password})


def test_mode_is_public_and_says_the_basemap(client):
    res = client.get("/api/mode")
    assert res.status_code == 200
    assert res.json() == {
        "mode": "demo", "basemap": "none", "public_url": None, "signed_in": False, "view": None
    }  # fmt: skip


def test_mode_in_a_pilot_server(make_app, clock_t0):
    from fastapi.testclient import TestClient

    app = make_app(seeded=False, mode="pilot", basemap="openfreemap")
    clock.install(clock_t0)
    assert TestClient(app).get("/api/mode").json() == {
        "mode": "pilot", "basemap": "openfreemap", "public_url": "http://ct.example.org", "signed_in": False, "view": None
    }  # fmt: skip


def test_signing_in_sets_an_http_only_cookie_and_describes_the_person(client, logins):
    res = sign_in(client, logins)
    assert res.status_code == 200
    me = res.json()
    assert me["role"] == "clinic" and me["clinic_id"] == "cln_gmc" and me["can_write"] is True
    assert me["mode"] == "demo" and me["csrf"]
    cookie = res.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=") and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert client.get("/api/me").json()["csrf"] == me["csrf"]


def test_a_wrong_password_and_an_unknown_name_get_the_same_answer(client, logins):
    wrong = sign_in(client, logins, "clinic", "not the password")
    unknown = sign_in(client, logins, "nobody-at-all", "demo")
    assert wrong.status_code == unknown.status_code == 401
    assert (
        wrong.json()
        == unknown.json()
        == {"error": "bad_credentials", "message": "Wrong username or password"}
    )
    assert COOKIE not in wrong.headers.get("set-cookie", "")


def test_a_person_switched_off_cannot_sign_in(client, ctx, logins):
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE staff SET active = 0 WHERE username = 'abc'")
    assert sign_in(client, logins, "abc").status_code == 401


def test_a_blank_form_is_a_plain_400_that_names_the_field_not_the_value(client):
    res = client.post("/api/auth/signin", json={"username": "", "password": "secret-value"})
    assert res.status_code == 400 and res.json()["error"] == "bad_request"
    assert "username" in res.json()["message"] and "secret-value" not in res.text


def test_signing_out_ends_the_login(client, logins):
    me = sign_in(client, logins).json()
    assert client.post("/api/auth/signout", headers={CSRF_HEADER: me["csrf"]}).status_code == 200
    assert client.get("/api/me").status_code == 401


def test_signing_out_needs_the_csrf_token(client, logins):
    sign_in(client, logins)
    res = client.post("/api/auth/signout")
    assert res.status_code == 403 and res.json()["error"] == "csrf"
    assert client.get("/api/me").status_code == 200


def test_signing_out_with_no_login_just_clears_the_cookie(client):
    assert client.post("/api/auth/signout").json() == {"ok": True}


def test_signing_in_again_replaces_the_earlier_login(client, ctx, logins):
    sign_in(client, logins, "clinic")
    sign_in(client, logins, "abc")
    assert client.get("/api/me").json()["role"] == "abc"
    assert ctx.db.one("SELECT COUNT(*) AS n FROM sessions")["n"] == 1


def test_the_visitor_entry_gives_a_read_only_view(client):
    res = client.post("/api/auth/visitor", json={"as": "clinic"})
    assert res.status_code == 200
    me = res.json()
    assert (me["role"], me["acting_as"], me["can_write"]) == ("visitor", "clinic", False)
    assert me["clinic_id"] == "cln_gmc"


def test_the_visitor_entry_refuses_a_view_that_does_not_exist(client):
    res = client.post("/api/auth/visitor", json={"as": "vet"})
    assert res.status_code == 400 and res.json()["error"] == "bad_request"


def test_there_is_no_visitor_entry_and_no_demo_clock_in_a_pilot_server(make_app, clock_t0, phone_key):
    from fastapi.testclient import TestClient

    app = make_app(seeded=False, mode="pilot")
    clock.install(clock_t0)
    client = TestClient(app)
    res = client.post("/api/auth/visitor", json={"as": "clinic"})
    assert res.status_code == 404 and res.json()["error"] == "not_available"
    ctx = app.state.ctx
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO staff(id, username, display_name, role, password_hash) VALUES ('stf_a','boss','Boss','admin',?)",
            (auth.hash_password("a long password"),),
        )
    me = client.post("/api/auth/signin", json={"username": "boss", "password": "a long password"}).json()
    headers = {CSRF_HEADER: me["csrf"]}
    assert client.get("/api/demo/state").status_code == 404
    assert client.post("/api/demo/jump", json={"to": "next_call"}, headers=headers).status_code == 404
    assert client.post("/api/demo/reset", headers=headers).status_code == 404


def test_every_write_needs_the_csrf_token(client, people):
    who = people["clinic"]
    body = {"out": True}
    no_header = client.post("/api/clinics/cln_gmc/vaccine", json=body, headers=who.read)
    wrong = client.post("/api/clinics/cln_gmc/vaccine", json=body, headers={**who.read, CSRF_HEADER: "wrong"})
    right = client.post("/api/clinics/cln_gmc/vaccine", json=body, headers=who.write)
    assert (no_header.status_code, wrong.status_code, right.status_code) == (403, 403, 200)
    assert no_header.json()["error"] == wrong.json()["error"] == "csrf"


def test_a_visitor_without_the_token_meets_the_role_rule_first(client, people):
    res = client.post(
        "/api/clinics/cln_gmc/vaccine", json={"out": True}, headers=people["visitor-clinic"].read
    )
    assert res.status_code == 403 and res.json()["error"] == "read_only"


def test_a_session_ends_after_the_policy_hours(client, ctx, people, clock_t0):
    hours = ctx.policy["visitor"]["session_hours"]
    who = people["abc"]
    assert client.get("/api/me", headers=who.read).status_code == 200
    clock_t0.advance(timedelta(hours=hours, seconds=1))
    assert client.get("/api/me", headers=who.read).status_code == 401


def test_a_forged_cookie_is_signed_out(client):
    assert client.get("/api/me", headers={"cookie": f"{COOKIE}=not-a-real-token"}).status_code == 401


def test_security_headers_are_on_pages_json_and_errors(client):
    for path in ("/api/mode", "/api/me", "/no-such-page", "/api/no-such-route", "/404"):
        res = client.get(path)
        assert res.headers["content-security-policy"] == CSP, path
        assert res.headers["x-content-type-options"] == "nosniff", path
        assert res.headers["x-frame-options"] == "DENY", path
        assert res.headers["referrer-policy"] == "no-referrer", path
    assert "script-src 'self';" in CSP and "style-src 'self';" in CSP and "unsafe-inline" not in CSP
    assert client.get("/api/mode").headers["cache-control"] == "no-store"


def test_unknown_routes_answer_in_the_api_shape_and_pages_in_html(client, ctx):
    api = client.get("/api/no-such-route")
    assert api.status_code == 404 and api.json() == {"error": "not_found", "message": "Not found"}
    page = client.get("/no-such-page")
    assert page.status_code == 404
    assert client.get("/404").status_code == 404
    wrong_method = client.delete("/api/mode")
    assert wrong_method.status_code == 405 and wrong_method.json()["error"] == "method_not_allowed"


def test_a_fault_on_our_side_is_a_plain_500_with_no_detail(make_app, clock_t0, monkeypatch):
    from fastapi.testclient import TestClient

    app = make_app()
    clock.install(clock_t0)

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("secret detail /etc/passwd")

    res = TestClient(app).get("/api/boom")
    assert res.status_code == 500 and res.json() == {
        "error": "server_error",
        "message": "Something went wrong on the server",
    }
    assert res.headers["content-security-policy"] == CSP


def test_a_csrf_header_with_a_non_ascii_character_is_a_403_not_a_fault(client, people):
    # the wire carries bytes, so the test sends them
    headers = {**people["clinic"].read, CSRF_HEADER: "tok\u00e9n".encode("latin-1")}
    res = client.post("/api/clinics/cln_gmc/vaccine", json={"out": True}, headers=headers)
    assert res.status_code == 403 and res.json()["error"] == "csrf"


def test_a_visitor_login_left_in_a_demo_database_is_no_login_in_pilot_mode(make_app, clock_t0):
    from fastapi.testclient import TestClient

    demo = make_app()
    clock.install(clock_t0)
    token, _ = auth.enter_as_visitor(demo.state.ctx, "clinic")
    assert TestClient(demo).get("/api/me", headers={"cookie": f"{COOKIE}={token}"}).status_code == 200
    pilot_ctx = demo.state.ctx
    pilot_ctx.settings = pilot_ctx.settings.model_copy(update={"mode": "pilot"})
    res = TestClient(demo).get("/api/me", headers={"cookie": f"{COOKIE}={token}"})
    assert res.status_code == 401


def test_the_visitor_clinic_is_a_demo_clinic(client, ctx):
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE clinics SET is_demo = 0 WHERE id = 'cln_gmc'")
    me = client.post("/api/auth/visitor", json={"as": "clinic"}).json()
    assert me["clinic_id"] != "cln_gmc"


# ---------------------------------------------------------------- rate limits
def from_address(app, address):
    """A browser at another address: the client the server sees."""
    from fastapi.testclient import TestClient

    return TestClient(app, client=(address, 50000))


def wrong(client, username="clinic"):
    return client.post("/api/auth/signin", json={"username": username, "password": "not the password"})


def test_the_policy_sets_the_sign_in_and_visitor_limits(ctx):
    assert ctx.policy["auth"] == {
        "max_failed_signins": 5,
        "window_min": 10,
        "visitor_sessions_per_min": 30,
        "max_failed_signins_per_address": 30,
        "signin_delay_base_s": 30,
        "signin_delay_max_s": 600,
    }


def test_five_wrong_passwords_make_that_name_at_that_address_wait_even_for_the_right_password(
    app, clock_t0, logins
):
    c = from_address(app, "10.0.0.1")
    assert [wrong(c).status_code for _ in range(5)] == [401] * 5
    waiting = sign_in(c, logins)  # the right password
    assert waiting.status_code == 429 and waiting.json()["error"] == "rate_limited"
    assert "Too many wrong sign-ins" in waiting.json()["message"]
    assert "Wait 30 seconds" in waiting.json()["message"]
    assert int(waiting.headers["retry-after"]) == 31
    assert COOKIE not in waiting.headers.get("set-cookie", "")
    assert (
        sign_in(from_address(app, "10.0.0.3"), logins, "abc").status_code == 200
    )  # other people are not held


def test_a_stranger_cannot_make_somebody_elses_name_wait_from_another_address(app, logins):
    stranger = from_address(app, "10.0.0.66")
    for _ in range(5):
        wrong(stranger)
    assert wrong(stranger).status_code == 429  # the stranger waits ...
    owner = from_address(app, "10.0.0.2")
    assert sign_in(owner, logins).status_code == 200  # ... the real clinic desk, elsewhere, does not


def test_the_wait_grows_with_each_wrong_password_and_ends_with_a_right_one(app, clock_t0, logins):
    c = from_address(app, "10.0.0.1")
    for _ in range(5):
        wrong(c)
    waits = []
    for _ in range(4):
        refused = sign_in(c, logins)
        assert refused.status_code == 429
        waits.append(int(refused.headers["retry-after"]) - 1)
        clock_t0.advance(timedelta(seconds=waits[-1]))
        assert wrong(c).status_code == 401  # allowed once the wait is over; wrong again
    assert waits == [30, 60, 120, 240]
    refused = sign_in(c, logins)
    assert int(refused.headers["retry-after"]) - 1 == 480
    clock_t0.advance(timedelta(seconds=480))
    assert sign_in(c, logins).status_code == 200
    assert [wrong(c).status_code for _ in range(5)] == [401] * 5  # the count started again


def test_the_wait_has_a_ceiling_from_policy(app, ctx, clock_t0, monkeypatch):
    monkeypatch.setitem(ctx.policy.raw["auth"], "signin_delay_max_s", 100)
    c = from_address(app, "10.0.0.1")
    for _ in range(5):
        wrong(c)
    waits = []
    for _ in range(4):
        refused = wrong(c)
        assert refused.status_code == 429
        waits.append(int(refused.headers["retry-after"]) - 1)
        clock_t0.advance(timedelta(seconds=waits[-1]))
        assert wrong(c).status_code == 401
    assert waits == [30, 60, 100, 100]


def test_the_wait_is_gone_when_the_window_has_passed(app, clock_t0, logins):
    c = from_address(app, "10.0.0.1")
    for _ in range(5):
        wrong(c)
    clock_t0.advance(timedelta(minutes=10, seconds=1))
    assert sign_in(c, logins).status_code == 200


def test_one_address_trying_many_names_is_stopped_at_the_larger_address_cap(app, clock_t0, logins):
    bad = from_address(app, "10.0.0.9")
    assert [wrong(bad, f"guess{i}").status_code for i in range(30)] == [401] * 30
    again = wrong(bad, "guess-thirty-one")
    assert again.status_code == 429 and again.json()["error"] == "rate_limited"
    assert sign_in(bad, logins).status_code == 429  # even a real person at that address waits
    assert sign_in(from_address(app, "10.0.0.10"), logins).status_code == 200
    clock_t0.advance(timedelta(minutes=10, seconds=1))
    assert sign_in(bad, logins).status_code == 200


def test_an_administrator_can_see_and_clear_a_wait(app, logins, people_for):
    c = from_address(app, "10.0.0.1")
    for _ in range(5):
        wrong(c)
    assert sign_in(c, logins).status_code == 429
    admin = people_for(app, "admin")
    seen = from_address(app, "10.0.0.2").get("/api/admin/signin-locks", headers=admin.read).json()
    assert [(r["username"], r["address"], r["failures"]) for r in seen] == [("clinic", "10.0.0.1", 5)]
    assert 0 < seen[0]["wait_s"] <= 31
    from fastapi.testclient import TestClient

    cleared = TestClient(app).post(
        "/api/admin/signin-locks/clear", json={"username": "clinic"}, headers=admin.write
    )
    assert cleared.status_code == 200 and cleared.json() == {"cleared": 1}
    assert sign_in(c, logins).status_code == 200
    row = app.state.ctx.db.one("SELECT actor, detail_json FROM audit WHERE action = 'signin.lock_clear'")
    assert row["actor"] == "staff:admin" and '"username": "clinic"' in row["detail_json"]


def test_clearing_a_wait_needs_an_administrator_and_a_name_or_an_address(app, people_for):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    for role in ("clinic", "abc", "vet", "oversight", "reviewer"):
        who = people_for(app, role)
        assert (
            client.post(
                "/api/admin/signin-locks/clear", json={"username": "x"}, headers=who.write
            ).status_code
            == 403
        )
        assert client.get("/api/admin/signin-locks", headers=who.read).status_code == 403
    token, csrf = auth.enter_as_visitor(app.state.ctx, "admin")
    visitor = {"cookie": f"{COOKIE}={token}", CSRF_HEADER: csrf}
    assert (
        client.post("/api/admin/signin-locks/clear", json={"username": "x"}, headers=visitor).status_code
        == 403
    )
    assert client.get("/api/admin/signin-locks", headers=visitor).status_code == 403  # it lists addresses
    admin = people_for(app, "admin")
    nothing = client.post("/api/admin/signin-locks/clear", json={}, headers=admin.write)
    assert nothing.status_code == 400 and nothing.json()["error"] == "bad_request"


def test_unknown_names_are_counted_like_known_ones(app):
    c = from_address(app, "10.0.0.1")
    for _ in range(5):
        wrong(c, "nobody-at-all")
    assert wrong(c, "nobody-at-all").status_code == 429


def test_a_good_sign_in_clears_the_count_for_that_name(app, logins):
    c = from_address(app, "10.0.0.1")
    for _ in range(4):
        wrong(c)
    assert sign_in(c, logins).status_code == 200
    other = from_address(app, "10.0.0.2")  # a different address, so only the name's count is in play
    assert [wrong(other).status_code for _ in range(5)] == [401] * 5


def test_the_refusal_is_plain_and_echoes_nothing_that_was_typed(app):
    c = from_address(app, "10.0.0.1")
    for _ in range(5):
        wrong(c)
    text = wrong(c).text
    assert "not the password" not in text and "clinic" not in text.replace("Too many wrong sign-ins", "")


def test_a_restart_forgets_the_count(make_app, clock_t0, logins):
    first = make_app()
    clock.install(clock_t0)
    for _ in range(5):
        wrong(from_address(first, "10.0.0.1"))
    assert wrong(from_address(first, "10.0.0.1")).status_code == 429
    second = make_app()  # a new server: the count lives in memory
    clock.install(clock_t0)
    assert wrong(from_address(second, "10.0.0.1")).status_code == 401


def test_thirty_visitors_a_minute_from_one_address_then_a_plain_429(app, ctx, clock_t0):
    c = from_address(app, "10.0.0.1")
    limit = ctx.policy["auth"]["visitor_sessions_per_min"]
    assert limit == 30  # shared venue wifi: a room of visitors enters from one address
    assert [c.post("/api/auth/visitor", json={"as": "abc"}).status_code for _ in range(limit)] == [
        200
    ] * limit
    res = c.post("/api/auth/visitor", json={"as": "abc"})
    assert res.status_code == 429 and res.json()["error"] == "rate_limited"
    assert "Too many visitors" in res.json()["message"] and int(res.headers["retry-after"]) >= 1
    assert from_address(app, "10.0.0.2").post("/api/auth/visitor", json={"as": "abc"}).status_code == 200
    clock_t0.advance(timedelta(seconds=61))
    assert c.post("/api/auth/visitor", json={"as": "abc"}).status_code == 200


def test_a_refused_visitor_view_does_not_use_up_the_allowance(app):
    c = from_address(app, "10.0.0.1")
    for _ in range(10):
        assert c.post("/api/auth/visitor", json={"as": "vet"}).status_code == 400
    assert c.post("/api/auth/visitor", json={"as": "abc"}).status_code == 200


def test_the_two_limits_are_separate(app, ctx, logins):
    c = from_address(app, "10.0.0.1")
    for _ in range(ctx.policy["auth"]["visitor_sessions_per_min"]):
        c.post("/api/auth/visitor", json={"as": "abc"})
    assert c.post("/api/auth/visitor", json={"as": "abc"}).status_code == 429
    assert sign_in(c, logins).status_code == 200  # visitors being counted does not lock staff out


def test_a_burst_of_parallel_wrong_guesses_cannot_all_pass_the_limit(ctx):
    """The tries are counted before the slow password check, so threads that start together are still held to
    the limit: five get a 401 and the rest a 429."""
    import threading

    outcomes: list[str] = []
    start = threading.Barrier(20)

    def guess() -> None:
        start.wait()
        try:
            auth.sign_in(ctx, "clinic", "not the password", "10.0.0.9")
        except auth.AuthError as e:
            outcomes.append(e.code)

    threads = [threading.Thread(target=guess) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("bad_credentials") == 5 and outcomes.count("rate_limited") == 15


def test_a_right_password_does_not_spend_the_address_allowance(ctx, logins):
    for _ in range(10):
        auth.sign_in(ctx, "clinic", logins["clinic"], "10.0.0.8")  # ten good sign-ins, none counted
    for _ in range(5):
        with pytest.raises(auth.AuthError) as e:
            auth.sign_in(ctx, "abc", "not the password", "10.0.0.8")
        assert e.value.code == "bad_credentials"
