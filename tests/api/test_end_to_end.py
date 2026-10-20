"""The demo walk-through through the real app, with no server and no network: seed the demo, enter as a
visitor, open a browser phone, give a missed call over the WebSocket, answer the first call over the
simulator protocol (the prompt audio is a tiny test voice in a temporary folder) and see the report appear
in the coverage the team reads."""

from __future__ import annotations

import copy
import threading
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from api_support import Who, wav_bytes
from conftest import T0
from fastapi.testclient import TestClient

from chaarteeke import clock
from chaarteeke.api.auth import COOKIE, CSRF_HEADER
from chaarteeke.policy import Policy, load_policy

KEYS = {
    "SYS_LANG": "1",  # Hindi
    "FC_CONSENT_Q": "1",  # yes to reminders
    "FC_QCONSENT": "1",  # yes to the four questions
    "FC_PLACETYPE": "1",  # home or street
    "FC_ANIMAL": "1",  # a street dog
    "FC_NOTCH": "1",  # the V-cut on the right ear was seen
    "FC_SICK": "1",  # signs of rabies
}


def quick_policy() -> Policy:
    """The real policy with the waits shortened, so a whole call takes a second or two."""
    base = load_policy()
    raw = copy.deepcopy(base.raw)
    raw["calls"].update({"first_call_delay_s": 0, "no_input_wait_s": 2, "ring_timeout_s": 5})
    raw["scheduler"].update({"loop_s": 0.05})
    return Policy(raw, "test-" + base.digest)


def wait_for(find, what, seconds=15.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        found = find()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def play_the_call(ws, keys: dict[str, str]) -> list[str]:
    """Be the person on the phone: give the missed call, answer, press the keys, speak the place."""
    heard: list[str] = []
    keys = dict(keys)
    while True:
        msg = ws.receive_json()
        kind = msg["type"]
        if kind == "ready":
            ws.send_json({"type": "missed_call"})
        elif kind == "ring":
            ws.send_json({"type": "answer"})
        elif kind == "play":
            ids = [u.rsplit("/", 1)[-1].removesuffix(".wav").split(".")[0] for u in msg["urls"]]
            heard += ids
            ws.send_json({"type": "played", "playback_id": msg["playback_id"], "ok": True})
            for pid in reversed(ids):
                if pid in keys:
                    ws.send_json({"type": "dtmf", "digit": keys.pop(pid)})
                    break
        elif kind == "record":
            ws.send_bytes(wav_bytes(1.5, rate=16000))
        elif kind == "hangup":
            return heard


def run_phone(ws, keys, seconds: float = 30.0) -> list[str]:
    """play_the_call on a side thread, so a call that never ends fails the test instead of hanging it."""
    out: dict[str, object] = {}

    def target() -> None:
        try:
            out["heard"] = play_the_call(ws, keys)
        except BaseException as e:  # reported below, on the test's own thread
            out["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"the call did not finish in {seconds} s"
    if "error" in out:
        raise out["error"]
    return out["heard"]


def test_a_visitor_gives_a_missed_call_and_the_answer_reaches_the_team(make_app, monkeypatch):
    monkeypatch.setattr("chaarteeke.api.app.get_policy", quick_policy)
    app = make_app(scheduler=True, background=True)
    clock.install(clock.OffsetClock(T0 - datetime.now(UTC)))  # inside the calling window, and moving
    ctx = app.state.ctx
    with TestClient(app) as client:
        client.post("/api/auth/visitor", json={"as": "abc"})
        csrf = client.get("/api/me").json()["csrf"]
        before = client.get("/api/coverage").json()

        session = client.post("/api/phone/sessions", headers={CSRF_HEADER: csrf})
        assert session.status_code == 200, session.text
        target = urlsplit(session.json()["ws_url"])
        cookie = f"{COOKIE}={client.cookies.get(COOKIE)}"
        with client.websocket_connect(f"{target.path}?{target.query}", headers={"cookie": cookie}) as ws:
            heard = run_phone(ws, KEYS)
            assert heard[0] == "SYS_LANG" and heard.index("FC_HELLO") > 0
            assert heard.index("FC_NOTICE") < heard.index("FC_CONSENT_Q") < heard.index("FC_PLACE")
            assert heard.index("FC_SICK") < heard.index("FC_SICK_YES") < heard.index("FC_CLOSE")
            assert {"FC_NOTCH", "FC_PLACETYPE", "FC_ANIMAL"} <= set(heard)
            assert "FC_DOSE" not in heard  # the first call no longer asks which injection was taken

            report = wait_for(
                lambda: ctx.db.one("SELECT * FROM bite_reports WHERE source = 'simulator'"), "the bite report"
            )
            assert (report["animal"], report["signs"], report["place_type"]) == (
                "notched",
                "yes",
                "home_street",
            )
            assert report["is_demo"] == 1 and report["clinic_id"] == "cln_demo_visitor"
            call = ctx.db.one(
                "SELECT status, kind, transport FROM calls WHERE kind = 'first' AND transport = 'sim'"
            )
            assert (call["status"], call["kind"]) == ("completed", "first")

            after = client.get("/api/coverage").json()
            assert after["total_bites"] == before["total_bites"] + 1  # the report is in what the team reads
            assert after["is_demo"] is True

            # the answer 'signs of rabies' made an alert for the vets, with no patient in it
            alerts = client.get("/api/alerts?status=new").json()
            mine = [a for a in alerts if a["simulated"]]
            assert mine and mine[0]["place_type"] == "home_street"

            # the place waits for the reviewer, because the speech model is off
            assert report["location_status"] in ("pending", "needs_review")
            waiting = wait_for(
                lambda: ctx.db.one(
                    "SELECT r.location_status, pc.asr_status FROM bite_reports r JOIN place_captures pc ON pc.report_id = r.id"
                    " WHERE r.id = ? AND r.location_status = 'needs_review'",
                    (report["id"],),
                ),
                "the place in the review queue",
            )
            assert waiting["asr_status"] == "off"  # no transcript: speech model off
            item = client.get(f"/api/review/{report['id']}/audio")  # an abc visitor is not the reviewer
            assert item.status_code == 403

            # the presenter jumps the demo clock to the evening before the next dose: the same phone rings
            jump = client.post(
                "/api/demo/jump", json={"to": "evening_before_next_dose"}, headers={CSRF_HEADER: csrf}
            )
            assert jump.status_code == 200, jump.text
            reminder_heard = run_phone(ws, {"RM_GATE": "1"})  # "that is me"

        assert reminder_heard[:2] == ["RM_GATE", "RM_HELLO"] and "RM_EVE_BODY" in reminder_heard

        # the visitor desk course is readable by a visitor viewing as a clinic
        course_id = ctx.db.one("SELECT id FROM courses WHERE source = 'simulator'")["id"]
        client.post("/api/auth/visitor", json={"as": "clinic"})
        course = client.get(f"/api/courses/{course_id}").json()
        assert course["status"] == "active" and course["source"] == "simulator"
        assert "sim:" not in str(course)
        kinds = {(c["kind"], c["status"]) for c in course["calls"]}
        assert kinds == {("first", "completed"), ("reminder", "completed")}
        desk = client.get("/api/clinic/today?clinic_id=cln_demo_visitor").json()
        assert desk["clinic_id"] == "cln_demo_visitor"


def give_a_missed_call_from_the_browser_phone(client, ctx, who):
    """Open a browser phone as `who` and give one missed call, as the page does. Returns the missed call
    the server recorded for it (the seeded demo already holds some of its own)."""
    known = {r["id"] for r in ctx.db.query("SELECT id FROM missed_calls")}
    session = client.post("/api/phone/sessions", headers=who.write)
    assert session.status_code == 200, session.text
    target = urlsplit(session.json()["ws_url"])
    with client.websocket_connect(f"{target.path}?{target.query}", headers=who.read) as ws:
        assert ws.receive_json() == {"type": "ready"}
        ws.send_json({"type": "missed_call"})
        assert ws.receive_json() == {"type": "missed_call_received"}
    return wait_for(
        lambda: next((r for r in ctx.db.query("SELECT * FROM missed_calls") if r["id"] not in known), None),
        "the missed call",
    )


def test_a_clinic_workers_browser_phone_missed_call_waits_in_their_clinics_today(
    make_app, clock_t0, people_for
):
    """The clinic-worker path: enrol a patient, then ring from the browser phone as that patient. The call
    must reach this clinic's Today to link, not link itself to the visitor desk."""
    app = make_app(background=True)
    clock.install(clock_t0)
    ctx = app.state.ctx
    worker = people_for(app, "clinic")
    with TestClient(app) as client:
        day0 = clock.local_today(ctx.settings.local_tz).isoformat()
        made = client.post(
            "/api/courses",
            json={"clinic_id": "cln_gmc", "register_no": "R-501", "regimen_id": "id_trc", "lang": "hi",
                  "day0": day0, "eligibility": "adult"},
            headers=worker.write,
        )  # fmt: skip
        assert made.status_code == 200, made.text
        course_id = made.json()["id"]

        row = give_a_missed_call_from_the_browser_phone(client, ctx, worker)
        assert (row["status"], row["clinic_hint"], row["course_id"]) == ("unlinked", "cln_gmc", None)
        today = client.get("/api/clinic/today", headers=worker.read).json()
        waiting = [m for m in today["unlinked"] if m["id"] == row["id"]]
        assert waiting and today["counts"]["unlinked"] >= 1  # in the clinic's list, to be linked by a person
        assert ctx.db.one("SELECT COUNT(*) AS n FROM courses WHERE clinic_id = 'cln_demo_visitor'")["n"] == 0
        assert (
            ctx.db.one("SELECT status FROM courses WHERE id = ?", (course_id,))["status"] == "awaiting_call"
        )

        # the worker links it to the patient they enrolled, and the first call is then planned for that patient
        linked = client.post(
            f"/api/missed-calls/{row['id']}/link", json={"course_id": course_id}, headers=worker.write
        )
        assert linked.status_code == 200, linked.text
        assert ctx.db.one("SELECT status FROM courses WHERE id = ?", (course_id,))["status"] == "active"


def test_a_visitors_browser_phone_missed_call_links_at_once_to_the_visitor_desk(
    make_app, clock_t0, people_for
):
    app = make_app(background=True)
    clock.install(clock_t0)
    ctx = app.state.ctx
    with TestClient(app) as client:
        client.post("/api/auth/visitor", json={"as": "clinic"})
        csrf = client.get("/api/me").json()["csrf"]
        viewer = Who(client.cookies.get(COOKIE), csrf)
        row = give_a_missed_call_from_the_browser_phone(client, ctx, viewer)
        assert (row["status"], row["clinic_hint"]) == ("linked", "cln_demo_visitor")
        assert ctx.db.one("SELECT COUNT(*) AS n FROM courses WHERE clinic_id = 'cln_demo_visitor'")["n"] == 1
