"""The event stream: delivery, the per-role filter, the clinic scope, pings, and no phone numbers.

TestClient buffers a whole response, so a stream that never ends would hang it. These tests drive the ASGI
app by hand instead: they send the request, read the chunks as they come, then hang up.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest

from chaarteeke import clock, enrol
from chaarteeke.api import sse


class Stream:
    def __init__(self, app, headers: dict[str, str]) -> None:
        self.app = app
        self.headers = headers
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}
        self.chunks: list[bytes] = []
        self._disconnect = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def open(self) -> Stream:
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
            "path": "/api/events", "raw_path": b"/api/events", "query_string": b"", "root_path": "",
            "scheme": "http", "server": ("testserver", 80), "client": ("test", 1),
            "headers": [(k.lower().encode(), v.encode()) for k, v in {"host": "testserver", **self.headers}.items()],
        }  # fmt: skip
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await self._disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                self.status = message["status"]
                self.response_headers = {k.decode(): v.decode() for k, v in message["headers"]}
            elif message["type"] == "http.response.body" and message.get("body"):
                self.chunks.append(message["body"])

        self._task = asyncio.create_task(self.app(scope, receive, send))
        await self.until(lambda: self.status is not None)
        return self

    async def until(self, predicate: Callable[[], bool], timeout: float = 3.0) -> None:
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"timed out; chunks so far: {self.chunks}")

    @property
    def text(self) -> str:
        return b"".join(self.chunks).decode()

    @property
    def events(self) -> list[dict]:
        out = []
        for block in self.text.split("\n\n"):
            fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))  # fmt: skip
            if "event" in fields:
                out.append({**json.loads(fields["data"]), "event": fields["event"], "sse_id": fields["id"]})
        return out

    async def close(self) -> None:
        self._disconnect.set()
        await asyncio.wait_for(self._task, 3)


async def open_stream(app, who) -> Stream:
    s = await Stream(app, who.read).open()
    if s.status == 200:
        await s.until(lambda: "retry: 5000" in s.text)
    return s


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(sse, "POLL_S", 0.02)


async def test_signed_out_gets_401_and_no_stream(app, people):
    s = await Stream(app, {}).open()
    assert s.status == 401
    await s.close()


async def test_the_stream_opens_with_retry_and_delivers_an_event(app, ctx, people):
    s = await open_stream(app, people["abc"])
    assert s.status == 200
    assert s.response_headers["content-type"].startswith("text/event-stream")
    assert s.response_headers["cache-control"] == "no-store"
    assert s.response_headers["content-security-policy"]  # the middleware covers streams too
    ctx.bus.publish("alert", {"alert_id": "alr_1", "status": "new", "cluster_id": "c1", "ward_no": 7})
    await s.until(lambda: s.events)
    ev = s.events[0]
    assert (ev["event"], ev["type"]) == ("alert", "alert")
    assert ev["data"] == {"alert_id": "alr_1", "status": "new", "cluster_id": "c1", "ward_no": 7}
    assert ev["sse_id"] == str(ev["id"]) and ev["at"].endswith("Z")
    await s.close()


async def test_an_idle_stream_sends_a_ping_comment(app, ctx, people, monkeypatch):
    monkeypatch.setattr(sse, "PING_S", 0.05)
    s = await open_stream(app, people["abc"])
    await s.until(lambda: ": ping" in s.text)
    await s.close()


async def test_each_view_gets_only_its_own_event_types(app, ctx, people):
    streams = {name: await open_stream(app, people[name]) for name in ("abc", "reviewer", "clinic", "admin")}
    ctx.bus.publish("alert", {"alert_id": "a", "status": "new", "cluster_id": None, "ward_no": None})
    ctx.bus.publish("review", {"remaining": 3})
    ctx.bus.publish("clock", {"now": "x", "local_now": "y"})
    ctx.bus.publish("report", {"report_id": "r", "cluster_id": None, "animal": "notched", "location_status": "pending", "simulated": True})  # fmt: skip
    await asyncio.sleep(0.2)
    got = {name: [e["type"] for e in s.events] for name, s in streams.items()}
    assert got["abc"] == ["alert", "clock", "report"]
    assert got["reviewer"] == ["review", "clock"]
    assert got["clinic"] == ["clock"]
    assert got["admin"] == ["clock"]
    for s in streams.values():
        await s.close()


async def test_a_clinic_gets_its_own_patients_events_only(app, ctx, people):
    ids = {}
    with ctx.db.transaction() as conn:
        for clinic in ("cln_gmc", "cln_demo2"):
            row = conn.execute("SELECT id FROM courses WHERE clinic_id = ? LIMIT 1", (clinic,)).fetchone()
            ids[clinic] = row["id"]
    mine = await open_stream(app, people["clinic"])
    viewer = await open_stream(app, people["visitor-clinic"])
    for course in ids.values():
        ctx.bus.publish("dose", {"course_id": course, "dose_no": 2, "status": "given"})
        ctx.bus.publish("call", {"call_id": "c", "course_id": course, "kind": "reminder", "status": "completed", "simulated": False})  # fmt: skip
    ctx.bus.publish("call", {"call_id": "c2", "course_id": None, "kind": "duplicate_notice", "status": "scheduled", "simulated": True})  # fmt: skip
    await asyncio.sleep(0.2)
    assert {e["data"]["course_id"] for e in mine.events} == {ids["cln_gmc"]}
    assert len(mine.events) == 2
    assert {e["data"]["course_id"] for e in viewer.events} == {ids["cln_gmc"], ids["cln_demo2"], None}
    await mine.close()
    await viewer.close()


async def test_a_clinic_login_with_no_clinic_gets_no_patient_events(app, ctx, people):
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE staff SET clinic_id = NULL WHERE username = 'clinic'")
    s = await open_stream(app, people["clinic"])
    course = ctx.db.one("SELECT id FROM courses LIMIT 1")["id"]
    ctx.bus.publish("dose", {"course_id": course, "dose_no": 2, "status": "given"})
    ctx.bus.publish("clock", {"now": "x", "local_now": "y"})
    await asyncio.sleep(0.2)
    assert [e["type"] for e in s.events] == ["clock"]
    await s.close()


async def test_missed_calls_reach_the_clinic_they_were_made_to_and_never_carry_the_number(app, ctx, people):
    mine = await open_stream(app, people["clinic"])
    theirs = await open_stream(app, people["visitor-clinic"])
    enrol.record_missed_call(ctx, "9876543210", "ari", clock.now())
    enrol.record_missed_call(ctx, "9123456780", "ari", clock.now(), clinic_hint="cln_demo2")
    await asyncio.sleep(0.2)
    assert [e["data"]["phone_last4"] for e in mine.events] == ["3210"]
    assert [e["data"]["phone_last4"] for e in theirs.events] == ["3210", "6780"]
    for s in (mine, theirs):
        assert "9876543210" not in s.text and "9123456780" not in s.text
        assert "+91" not in s.text
        await s.close()


async def test_hanging_up_unsubscribes(app, ctx, people):
    s = await open_stream(app, people["abc"])
    assert len(ctx.bus._subs) == 1
    await s.close()
    assert len(ctx.bus._subs) == 0


def test_every_event_type_the_bus_knows_is_given_to_some_view():
    from chaarteeke.events import EVENT_TYPES

    given = set().union(*sse.TYPES_BY_VIEW.values())
    assert set(EVENT_TYPES) - given == set()


async def test_oversight_reads_aggregates_so_it_gets_no_alert_events(app, ctx, people):
    s = await open_stream(app, people["oversight"])
    ctx.bus.publish("alert", {"alert_id": "a", "status": "new", "cluster_id": "c1", "ward_no": 4})
    ctx.bus.publish("clock", {"now": "x", "local_now": "y"})
    await asyncio.sleep(0.2)
    assert [e["type"] for e in s.events] == ["clock"]
    await s.close()


async def test_a_real_report_is_announced_without_its_answer_but_a_simulated_one_carries_it(app, ctx, people):
    s = await open_stream(app, people["abc"])
    real = {"report_id": "rep_1", "cluster_id": "c1", "animal": "notched", "location_status": "matched", "simulated": False}  # fmt: skip
    ctx.bus.publish("report", real)
    ctx.bus.publish("report", {**real, "report_id": "rep_2", "simulated": True})
    await s.until(lambda: len(s.events) == 2)
    first, second = s.events
    assert first["data"] == {"report_id": "rep_1", "cluster_id": "c1", "simulated": False}
    assert second["data"]["animal"] == "notched" and second["data"]["location_status"] == "matched"
    await s.close()


async def test_a_stream_ends_when_its_login_ends(app, ctx, people, monkeypatch):
    monkeypatch.setattr(sse, "PING_S", 0.05)
    s = await open_stream(app, people["abc"])
    with ctx.db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE staff_id = (SELECT id FROM staff WHERE username = 'abc')")
    await s.until(lambda: s._task.done())
    assert len(ctx.bus._subs) == 0
    await s.close()
