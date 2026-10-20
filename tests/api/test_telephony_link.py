"""A phone line that cannot connect leaves the app up with the telephony check red, and the administrator can
try again."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from chaarteeke import clock
from chaarteeke.api import app as app_module
from chaarteeke.enrol import new_call
from chaarteeke.telephony.sim import SimPort

DEAD_LINE = "http://127.0.0.1:9"  # nothing listens on the discard port: the connection is refused at once
RECONNECT = "/api/system/telephony/reconnect"


@pytest.fixture
def dead_line(make_app, clock_t0):
    """An app with CT_TELEPHONY=ari whose PBX is not there, started the way uvicorn starts it."""

    def build(**overrides):
        app = make_app(
            background=True, telephony="ari", ari_url=DEAD_LINE, ari_user="u", ari_password="hunter2",
            **overrides,
        )  # fmt: skip
        clock.install(clock_t0)
        return app

    return build


def telephony(client, who) -> dict:
    res = client.get("/api/system/status", headers=who.read)
    assert res.status_code == 200, res.text
    return next(c for c in res.json()["checks"] if c["name"] == "telephony")


def test_the_app_starts_when_the_phone_line_cannot_connect_and_says_why(dead_line, people_for):
    app = dead_line()
    admin = people_for(app, "admin")
    with TestClient(app) as client:
        assert client.get("/api/mode").status_code == 200  # the pages are served
        check = telephony(client, admin)
    assert check["ok"] is False and check["alarm"] is True
    assert "127.0.0.1:9" in check["detail"] and "phone line" in check["detail"]
    assert "hunter2" not in check["detail"] and "api_key" not in check["detail"]  # nothing secret on screen


def test_a_call_is_refused_with_the_reason_while_the_line_is_down(dead_line, people_for):
    app = dead_line()
    ctx = app.state.ctx
    with TestClient(app):
        with ctx.db.transaction() as conn:
            course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()["id"]
            call_id = new_call(
                conn, course_id=course, kind="reminder", job_key="line-down", scheduled_for=clock.now(), dose_no=2
            )  # fmt: skip
        status = asyncio.run(ctx.runner.place(call_id))
        row = ctx.db.one("SELECT status, error FROM calls WHERE id = ?", (call_id,))
    assert status == "failed" and row["status"] == "failed"
    assert "telephone link is down" in row["error"] and "127.0.0.1:9" in row["error"]


def test_reconnect_is_for_the_administrator_only_and_needs_the_csrf_token(dead_line, people_for):
    app = dead_line()
    with TestClient(app) as client:
        assert client.post(RECONNECT).status_code == 401
        for role in ("clinic", "abc", "oversight", "reviewer", "vet"):
            who = people_for(app, role)
            assert client.post(RECONNECT, headers=who.write).status_code == 403, role
        admin = people_for(app, "admin")
        refused = client.post(RECONNECT, headers=admin.read)  # no CSRF header
        assert refused.status_code == 403 and refused.json()["error"] == "csrf"


def test_a_visitor_cannot_reconnect(dead_line):
    from chaarteeke.api import auth

    app = dead_line()
    token, csrf = auth.enter_as_visitor(app.state.ctx, "admin")
    with TestClient(app) as client:
        res = client.post(RECONNECT, headers={"cookie": f"ct_session={token}", "x-ct-csrf": csrf})
    assert res.status_code == 403 and res.json()["error"] == "read_only"


def test_reconnecting_to_a_line_that_is_still_down_says_so_and_is_audited(dead_line, people_for):
    app = dead_line()
    admin = people_for(app, "admin")
    ctx = app.state.ctx
    with TestClient(app) as client:
        res = client.post(RECONNECT, headers=admin.write)
        assert res.status_code == 200
        body = res.json()
        assert body["name"] == "telephony" and body["ok"] is False and body["alarm"] is True
        assert "127.0.0.1:9" in body["detail"]
        assert telephony(client, admin)["ok"] is False
    row = ctx.db.one("SELECT actor, detail_json FROM audit WHERE action = 'telephony.reconnect'")
    assert row["actor"] == "staff:admin" and '"ok": false' in row["detail_json"]


def test_reconnecting_once_the_line_is_back_brings_the_link_up(dead_line, people_for, monkeypatch):
    app = dead_line()
    admin = people_for(app, "admin")
    ctx = app.state.ctx
    heard = ctx.bus.subscribe()
    with TestClient(app) as client:
        assert telephony(client, admin)["ok"] is False
        broken_runner = ctx.runner
        monkeypatch.setattr(app_module, "_telephony", lambda settings: SimPort())  # the PBX answers now
        res = client.post(RECONNECT, headers=admin.write)
        assert res.status_code == 200 and res.json()["ok"] is True
        assert telephony(client, admin)["ok"] is True
        assert ctx.runner is not broken_runner and ctx.runner._down is None
        assert broken_runner._pump is None  # the old runner's event reader was ended, not left running
        assert isinstance(ctx.telephony, SimPort)
        again = client.post(RECONNECT, headers=admin.write)  # a link that is up is left alone
        assert again.status_code == 200 and again.json()["ok"] is True
        assert ctx.extras.get("telephony_error") is None
    system = []
    while not heard.empty():
        ev = heard.get_nowait()
        if ev.type == "system":
            system.append(ev.data)
    assert system == [{"check": "telephony", "ok": False}, {"check": "telephony", "ok": True}]


def test_the_link_comes_up_again_after_it_drops_while_running(make_app, clock_t0, people_for, monkeypatch):
    from chaarteeke.telephony.model import TelephonyConnectionError

    app = make_app(background=True)  # the browser phone
    clock.install(clock_t0)
    admin = people_for(app, "admin")
    ctx = app.state.ctx
    with TestClient(app) as client:
        assert telephony(client, admin)["ok"] is True
        ctx.runner._down = TelephonyConnectionError(
            "the event socket closed"
        )  # what the runner does on a drop
        assert telephony(client, admin)["ok"] is False
        dropped = ctx.runner
        res = client.post(RECONNECT, headers=admin.write)
        assert res.json()["ok"] is True and ctx.runner is not dropped and ctx.runner._down is None


def test_reconnect_on_a_server_with_no_phone_link_is_refused(make_app, clock_t0, people_for):
    app = make_app(background=False)
    clock.install(clock_t0)
    ctx = app.state.ctx
    ctx.telephony = None
    admin = people_for(app, "admin")
    res = TestClient(app).post(RECONNECT, headers=admin.write)
    assert res.status_code == 404 and res.json()["error"] == "not_available"


@pytest.mark.parametrize("url", ["http://u:hunter2@:notaport", "u:hunter2@", "http://u:hunter2@[::1"])
def test_an_address_that_cannot_be_read_never_puts_the_password_in_the_reason(make_app, url):
    from chaarteeke.api.app import _port_reason

    settings = make_app().state.ctx.settings.model_copy(update={"ari_url": url})
    reason = _port_reason(settings, ConnectionRefusedError("refused " + url))
    assert "hunter2" not in reason and "the configured address" in reason
    assert "ConnectionRefusedError" in reason
