"""What the server says about its own transport and how much it will take: the security headers that depend
on the public address, compression and caching of the static files, the request body limits and the cap on
live event streams."""

from __future__ import annotations

import asyncio
import gc

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from chaarteeke import clock
from chaarteeke.api import auth, sse
from chaarteeke.api.app import CSP, csp_for
from chaarteeke.api.auth import COOKIE
from chaarteeke.config import Settings
from chaarteeke.policy import get_policy

JSON_MAX = 65536


# ----------------------------------------------------------------- CSP and HSTS
def test_the_default_csp_allows_connections_to_this_server_only(client):
    res = client.get("/api/mode")
    assert res.headers["content-security-policy"] == CSP
    assert "connect-src 'self';" in CSP and "ws:" not in CSP and "wss:" not in CSP


def test_an_https_public_address_adds_its_exact_secure_websocket_origin(make_app, clock_t0):
    app = make_app(public_url="https://demo.example.org/")
    clock.install(clock_t0)
    csp = TestClient(app).get("/api/mode").headers["content-security-policy"]
    assert "connect-src 'self' wss://demo.example.org;" in csp
    assert "ws://" not in csp.replace("wss://", "") and "wss:;" not in csp and "wss: " not in csp


def test_an_http_public_address_adds_its_exact_websocket_origin_with_the_port(make_app, clock_t0):
    app = make_app(public_url="http://192.168.1.20:8080")
    clock.install(clock_t0)
    csp = TestClient(app).get("/api/mode").headers["content-security-policy"]
    assert "connect-src 'self' ws://192.168.1.20:8080;" in csp


def test_csp_for_ignores_an_address_that_is_not_http():
    base = dict(_env_file=None, asr="off")
    assert csp_for(Settings(**base, public_url="javascript:alert(1)")) == CSP
    assert csp_for(Settings(**base, public_url="")) == CSP


def test_hsts_is_sent_on_an_https_address_and_not_on_plain_http(make_app, clock_t0):
    plain = make_app()
    clock.install(clock_t0)
    assert "strict-transport-security" not in TestClient(plain).get("/api/mode").headers
    secure_request = TestClient(plain, base_url="https://testserver").get("/api/mode")
    assert secure_request.headers["strict-transport-security"].startswith("max-age=")
    public = make_app(public_url="https://demo.example.org")
    clock.install(clock_t0)
    assert "strict-transport-security" in TestClient(public).get("/api/mode").headers


def test_a_session_cookie_is_secure_on_an_https_address(make_app, clock_t0):
    app = make_app(public_url="https://demo.example.org")
    clock.install(clock_t0)
    res = TestClient(app).post("/api/auth/visitor", json={"as": "abc"})
    assert "Secure" in res.headers["set-cookie"]


# ------------------------------------------------------------- static files
def test_static_text_is_compressed_for_a_browser_that_accepts_it(client):
    res = client.get("/css/base.css", headers={"accept-encoding": "gzip"})
    assert res.status_code == 200 and res.headers["content-encoding"] == "gzip"
    assert res.headers["vary"].lower().find("accept-encoding") >= 0
    page = client.get("/clinic", headers={"accept-encoding": "gzip"})
    assert page.headers.get("content-encoding") == "gzip"
    plain = client.get("/css/base.css", headers={"accept-encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.content == res.content  # the same bytes decoded


def test_the_api_the_audio_and_the_fonts_are_never_compressed(client, ctx):
    assert "content-encoding" not in client.get("/api/mode", headers={"accept-encoding": "gzip"}).headers
    audio = next(ctx.settings.data_dir.joinpath("prompts", "audio", "hi").glob("FC_NOTICE*.wav")).name
    assert "content-encoding" not in client.get(f"/media/prompts/hi/{audio}", headers={"accept-encoding": "gzip"}).headers  # fmt: skip


def test_a_versioned_library_and_the_fonts_are_kept_for_a_year_and_the_rest_is_revalidated(client):
    lib = client.get("/vendor/maplibre-gl-4.7.1/maplibre-gl.js")
    assert lib.headers["cache-control"] == "public, max-age=31536000, immutable"
    font = client.get("/fonts/atkinson-latin.woff2")
    assert font.headers["cache-control"] == "public, max-age=31536000, immutable"
    for path in (
        "/css/base.css",
        "/js/core/api.js",
        "/i18n/common.en.json",
        "/data/wards.geojson",
        "/clinic",
    ):
        assert client.get(path).headers["cache-control"] == "no-cache", path
    assert client.get("/api/mode").headers["cache-control"] == "no-store"


def test_a_revalidating_browser_gets_304_and_no_body(client):
    first = client.get("/css/base.css")
    again = client.get("/css/base.css", headers={"if-none-match": first.headers["etag"]})
    assert again.status_code == 304 and again.content == b""


# ---------------------------------------------------------------- body limits
def test_a_json_body_over_the_limit_is_refused_by_its_announced_length(client, people):
    who = people["clinic"]
    big = {"reason": "x" * (JSON_MAX + 10)}
    res = client.post("/api/courses/crs_x/stop", json=big, headers=who.write)
    assert res.status_code == 413
    assert res.json() == {"error": "too_large", "message": "What was sent is too large"}
    assert res.headers["content-security-policy"] == CSP  # an answer in the API's own shape, with its headers


def test_a_body_under_the_limit_reaches_the_route(client, people):
    res = client.post("/api/courses/crs_x/stop", json={"reason": "y" * 150}, headers=people["clinic"].write)
    assert res.status_code != 413


def test_a_body_that_lies_about_its_length_is_stopped_as_it_streams(client, people):
    def chunks():
        for _ in range(40):
            yield b"x" * 4096  # 160 KB, sent with no Content-Length

    res = client.post(
        "/api/courses/crs_x/stop",
        content=chunks(),
        headers={**people["clinic"].write, "content-type": "application/json"},  # fmt: skip
    )
    assert res.status_code == 413 and res.json()["error"] == "too_large"


def test_a_get_is_not_limited_and_a_pilot_login_body_is_small_enough(client, people):
    assert client.get("/api/me", headers=people["clinic"].read).status_code == 200
    login = client.post("/api/auth/signin", json={"username": "a", "password": "b" * 256})
    assert login.status_code == 401


def test_the_recording_upload_has_its_own_larger_limit(make_app, clock_t0, people_for, monkeypatch):
    monkeypatch.setitem(get_policy().raw["limits"], "upload_body_max_bytes", 200_000)
    app = make_app()
    clock.install(clock_t0)
    client = TestClient(app)
    admin = people_for(app, "admin")
    path = "/api/admin/prompts/hi/FC_HELLO.0"
    over = client.post(path, content=b"R" * 200_001, headers={**admin.write, "content-type": "audio/wav"})
    assert over.status_code == 413 and over.json()["error"] == "too_large"
    under = client.post(path, content=b"R" * 100_000, headers={**admin.write, "content-type": "audio/wav"})
    assert (
        under.status_code == 400 and under.json()["error"] == "bad_wav"
    )  # past the limit, refused as a file


def test_the_limits_come_from_policy(ctx):
    assert ctx.policy["limits"] == {
        "json_body_max_bytes": 65536,
        "upload_body_max_bytes": 5 * 1024 * 1024,
        "event_streams_per_principal": 5,
    }


# --------------------------------------------------------------- event streams
def request_for(app, who) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/events",
        "query_string": b"",
        "headers": [(b"cookie", f"{COOKIE}={who.token}".encode())],
        "app": app,
        "state": {},
    }
    return Request(scope)


async def open_stream(app, who):
    response = await sse.events(request_for(app, who))
    await anext(response.body_iterator)  # the stream has started: the first thing it sends is `retry: 5000`
    return response


def test_a_login_may_hold_only_so_many_event_streams_at_once(app, ctx, people):
    async def go():
        who = people["clinic"]
        held = [
            await open_stream(app, who) for _ in range(ctx.policy["limits"]["event_streams_per_principal"])
        ]
        with pytest.raises(auth.AuthError) as refused:
            await sse.events(request_for(app, who))
        assert refused.value.code == "too_many_streams" and refused.value.status == 429
        # another person is not held by it, and a closed stream frees a place
        other = await open_stream(app, people["abc"])
        await held[0].body_iterator.aclose()
        again = await open_stream(app, who)
        for r in (*held[1:], other, again):
            await r.body_iterator.aclose()
        assert not ctx.extras["open_event_streams"]

    asyncio.run(go())


def test_a_stream_that_never_started_gives_its_place_back(app, ctx, people):
    async def go():
        who = people["clinic"]
        for _ in range(ctx.policy["limits"]["event_streams_per_principal"]):
            await sse.events(request_for(app, who))  # answered, never read: a browser that went away at once
        gc.collect()
        await sse.events(request_for(app, who))  # a place is free again

    asyncio.run(go())


def test_the_refusal_reaches_the_client_as_429_with_a_retry_after(client, ctx, people, monkeypatch):
    monkeypatch.setitem(ctx.policy.raw["limits"], "event_streams_per_principal", 0)
    res = client.get("/api/events", headers=people["clinic"].read)
    assert res.status_code == 429 and res.json()["error"] == "too_many_streams"
    assert int(res.headers["retry-after"]) >= 1
