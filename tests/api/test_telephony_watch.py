"""A phone line that drops comes back by itself, and the calls it cost are given back."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from datetime import timedelta

import pytest
from aiohttp import web
from fastapi.testclient import TestClient

from chaarteeke import clock
from chaarteeke.api import telephony_watch
from chaarteeke.enrol import new_call
from chaarteeke.telephony.fake_ari import PASSWORD, USER, FakeAri

FAST = {"reconnect_min_s": 0.05, "reconnect_max_s": 0.4}


class FixedPortAri(FakeAri):
    """The fake PBX on a port of our choosing, so it can be stopped and started again at the same address."""

    async def start_on(self, port: int) -> str:
        app = web.Application(middlewares=[self._auth_mw])
        r = app.router
        r.add_get("/ari/events", self._events)
        r.add_post("/ari/channels", self._originate)
        r.add_delete("/ari/channels/{id}", self._delete_channel)
        r.add_post("/ari/channels/{id}/play", self._play)
        r.add_post("/ari/channels/{id}/record", self._record)
        r.add_post("/ari/applications/{app}/subscription", self._subscribe)
        r.add_get("/ari/recordings/stored/{name}/file", self._rec_file)
        r.add_delete("/ari/recordings/stored/{name}", self._rec_delete)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", port).start()
        self.port = port
        return f"http://127.0.0.1:{port}"


class Pbx:
    """A FakeAri running on its own event loop in a thread, which the test can stop and start."""

    def __init__(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.fake: FixedPortAri | None = None

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.fake = FixedPortAri()
        self._run(self.fake.start_on(self.port))

    def stop(self) -> None:
        self._run(self.fake.stop())
        self.fake = None

    def close(self) -> None:
        if self.fake is not None:
            self.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def pbx():
    box = Pbx()
    box.start()
    yield box
    box.close()


def until(check, seconds: float = 10.0, what: str = "the condition"):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if found := check():
            return found
        time.sleep(0.02)
    raise AssertionError(f"{what} did not happen within {seconds} s")


def telephony(client, who) -> dict:
    res = client.get("/api/system/status", headers=who.read)
    return next(c for c in res.json()["checks"] if c["name"] == "telephony")


@pytest.fixture
def line(make_app, clock_t0, people_for, pbx, monkeypatch):
    """An app on CT_TELEPHONY=ari against the fake PBX, started the way uvicorn starts it, retrying quickly."""
    from chaarteeke.policy import get_policy

    for key, value in FAST.items():
        monkeypatch.setitem(get_policy().raw["telephony"], key, value)
    app = make_app(
        background=True, telephony="ari", ari_url=pbx.url, ari_user=USER, ari_password=PASSWORD
    )  # fmt: skip
    clock.install(clock_t0)
    return app, people_for(app, "admin")


def test_the_line_is_taken_up_again_by_itself_when_the_pbx_comes_back(line, pbx):
    app, admin = line
    ctx = app.state.ctx
    heard = ctx.bus.subscribe()
    with TestClient(app) as client:
        until(lambda: telephony(client, admin)["ok"], what="the line to come up")
        first_runner = ctx.runner
        pbx.stop()
        down = until(lambda: (c := telephony(client, admin))["alarm"] and c, what="the drop to show")
        assert down["code"] == "telephony.down"
        assert ctx.runner is not first_runner or ctx.runner._down is not None
        pbx.start()
        up = until(lambda: (c := telephony(client, admin))["ok"] and c, what="the line to reconnect")
        assert up["alarm"] is False and ctx.runner._down is None
        assert ctx.runner is not first_runner  # a runner reading the new link
    system = []
    while not heard.empty():
        ev = heard.get_nowait()
        if ev.type == "system" and ev.data["check"] == "telephony":
            system.append(ev.data["ok"])
    assert system[0] is True and False in system and system[-1] is True  # up, down, up again


def test_the_wait_between_tries_grows_from_the_policy_minimum_to_its_maximum(ctx):
    seen, wait = [], None
    for _ in range(6):
        wait = telephony_watch.next_wait(ctx, wait)
        seen.append(wait)
    assert seen == [5, 10, 20, 40, 60, 60]  # 5 s to 60 s, from policy telephony.reconnect_min_s / max_s
    assert ctx.policy["telephony"] == {"reconnect_min_s": 5, "reconnect_max_s": 60}


def make_call(ctx, key: str, *, attempt: int = 1, status: str = "scheduled", course: str | None = None):
    course = course or ctx.db.one("SELECT id FROM courses LIMIT 1")["id"]
    with ctx.db.transaction() as conn:
        call_id = new_call(
            conn, course_id=course, kind="reminder", job_key=key, scheduled_for=clock.now(), dose_no=2,
            attempt=attempt,
        )  # fmt: skip
        if status != "scheduled":
            conn.execute("UPDATE calls SET status = ? WHERE id = ?", (status, call_id))
    return call_id


def test_calls_that_failed_because_the_line_was_down_are_given_back_without_using_an_attempt(line, pbx):
    app, admin = line
    ctx = app.state.ctx
    with TestClient(app) as client:
        until(lambda: telephony(client, admin)["ok"], what="the line to come up")
        # a try that ended for a real reason before the outage: it stays what it was
        earlier = make_call(ctx, "earlier", status="no_answer")
        with ctx.db.transaction() as conn:
            conn.execute(
                "UPDATE calls SET ended_at = ?, error = 'The phone rang but nobody answered.' WHERE id = ?",
                (clock.iso(clock.now() - timedelta(hours=1)), earlier),
            )
        pbx.stop()
        until(lambda: telephony(client, admin)["alarm"], what="the drop to show")
        lost = make_call(ctx, "outage-a")
        asyncio.run(ctx.runner.place(lost))  # the scheduler hands it over; the runner refuses while down
        assert ctx.db.one("SELECT status, error FROM calls WHERE id = ?", (lost,))["status"] == "failed"
        assert (
            "telephone link is down" in ctx.db.one("SELECT error FROM calls WHERE id = ?", (lost,))["error"]
        )
        # what the scheduler did meanwhile: a retry row for it, and a second try that also met the outage
        retry = make_call(ctx, "outage-a:a2", attempt=2)
        second = make_call(ctx, "outage-b")
        asyncio.run(ctx.runner.place(second))
        later_try = make_call(ctx, "outage-b:a2", attempt=2)
        asyncio.run(ctx.runner.place(later_try))  # the retry of b also met the outage
        # a try that failed after the line was down, for a reason of its own: it stays failed
        declined = make_call(ctx, "outage-c")
        with ctx.db.transaction() as conn:
            conn.execute(
                "UPDATE calls SET status = 'failed', ended_at = ?, error = 'The call was declined.' WHERE id = ?",
                (clock.iso(clock.now()), declined),
            )
        pbx.start()
        until(lambda: telephony(client, admin)["ok"], what="the line to reconnect")
        until(
            lambda: ctx.db.one("SELECT status FROM calls WHERE id = ?", (lost,))["status"] == "scheduled",
            what="the call to be given back",
        )

        def row(call_id):
            return ctx.db.one("SELECT * FROM calls WHERE id = ?", (call_id,))

        for call_id in (lost, second):
            back = row(call_id)
            assert back["status"] == "scheduled" and back["error"] is None and back["attempt"] == 1
            assert back["ended_at"] is None and clock.parse_iso(back["scheduled_for"]) <= clock.now()
        assert row(retry) is None  # the scheduler's retry for it would have called twice
        assert row(later_try) is None  # a later try that met the same outage is not a second call
        assert row(earlier)["status"] == "no_answer"  # a real result stays
        assert row(declined)["status"] == "failed"
        audit = ctx.db.query("SELECT object_id FROM audit WHERE action = 'call.replanned'")
        assert sorted(r["object_id"] for r in audit) == sorted([lost, second])


def test_nothing_is_given_back_when_there_was_no_outage(ctx):
    make_call(ctx, "plain", status="failed")
    assert telephony_watch.give_back_outage_calls(ctx) == 0


def test_a_link_that_is_up_is_left_alone_by_the_loop(make_app, clock_t0, monkeypatch):
    """The browser phone never drops; the loop only looks and sleeps."""
    from chaarteeke.policy import get_policy

    monkeypatch.setitem(get_policy().raw["telephony"], "reconnect_min_s", 0.05)
    app = make_app(background=True)
    clock.install(clock_t0)
    ctx = app.state.ctx
    with TestClient(app):
        port, runner = ctx.telephony, ctx.runner
        time.sleep(0.4)
        assert ctx.telephony is port and ctx.runner is runner and telephony_watch.link_is_up(ctx)
