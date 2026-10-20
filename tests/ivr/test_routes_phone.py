"""POST /api/phone/sessions and WS /api/phone/ws through FastAPI's TestClient."""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from ivr_support import World, fast_policy
from starlette.websockets import WebSocketDisconnect

from chaarteeke.api import auth, routes_phone
from chaarteeke.api.auth import COOKIE, CSRF_HEADER, AuthError
from chaarteeke.config import DATA_DIR
from chaarteeke.telephony.model import CallAnswered, MissedCall
from chaarteeke.telephony.mux import MuxPort
from chaarteeke.telephony.sim import SimPort


@pytest.fixture
def rctx(ctx):
    ctx.policy = fast_policy()
    ctx.telephony = SimPort()
    return ctx


@pytest.fixture
def client(rctx):
    app = FastAPI()
    app.state.ctx = rctx
    app.include_router(routes_phone.router)

    @app.exception_handler(AuthError)
    async def auth_error(_: Request, e: AuthError):
        return JSONResponse({"error": e.code, "message": e.message}, status_code=e.status)

    with TestClient(app) as c:
        yield c


def visitor(rctx, view="clinic") -> dict:
    token, csrf = auth.enter_as_visitor(rctx, view)
    return {
        "cookie": {"cookie": f"{COOKIE}={token}"},
        "post": {"headers": {CSRF_HEADER: csrf, "cookie": f"{COOKIE}={token}"}},
    }


def staff(rctx, role: str, clinic_id: str | None = None) -> dict:
    """A signed-in staff member of `role` (a clinic login belongs to `clinic_id`)."""
    if clinic_id:
        World(rctx).clinic(clinic_id)
    with rctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO staff(id, username, display_name, role, clinic_id, password_hash, active, is_demo)"
            " VALUES (?,?,?,?,?, 'x', 1, 1)",
            (f"stf_{role}", role, role.title(), role, clinic_id),
        )
    token, csrf = auth.create_session(rctx, staff_id=f"stf_{role}", role=role, visitor_label=None)
    return {
        "cookie": {"cookie": f"{COOKIE}={token}"},
        "post": {"headers": {CSRF_HEADER: csrf, "cookie": f"{COOKIE}={token}"}},
    }


def open_session(client, who) -> dict:
    return client.post("/api/phone/sessions", **who["post"])


def test_a_signed_in_visitor_gets_a_session_and_a_socket_address(client, rctx):
    r = open_session(client, visitor(rctx))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"session", "ws_url"}
    assert body["ws_url"] == f"ws://testserver/api/phone/ws?session={body['session']}"
    assert rctx.telephony.session(body["session"]).owner.startswith("visitor:")
    row = rctx.db.one("SELECT actor FROM audit WHERE action='phone.session'")
    assert row["actor"].startswith("visitor:")


def test_the_public_url_decides_the_socket_address_behind_a_tunnel(client, rctx):
    rctx.settings = rctx.settings.model_copy(update={"public_url": "https://demo.example.org/"})
    body = open_session(client, visitor(rctx)).json()
    assert body["ws_url"] == f"wss://demo.example.org/api/phone/ws?session={body['session']}"


def test_signed_out_and_missing_csrf_are_refused(client, rctx):
    r = client.post("/api/phone/sessions")
    assert (r.status_code, r.json()["error"]) == (401, "signed_out")
    who = visitor(rctx)
    r = client.post("/api/phone/sessions", headers=who["cookie"])
    assert (r.status_code, r.json()["error"]) == (403, "csrf")
    assert rctx.telephony.session_count() == 0


def test_each_visitor_has_a_cap_and_the_server_has_a_total(client, rctx):
    rctx.policy.raw["visitor"].update(
        {"phone_sessions_per_visitor": 2, "phone_sessions_total": 3, "phone_sessions_reserved": 0}
    )
    a, b, c = visitor(rctx), visitor(rctx), visitor(rctx)
    assert [open_session(client, a).status_code for _ in range(2)] == [200, 200]
    r = open_session(client, a)
    assert (r.status_code, r.json()["error"]) == (429, "phone_sessions_full")
    assert open_session(client, b).status_code == 200
    r = open_session(client, c)
    assert (r.status_code, r.json()["error"]) == (429, "phone_sessions_busy")
    assert rctx.telephony.session_count() == 3


def test_the_reserved_places_are_kept_free_for_staff(client, rctx):
    rctx.policy.raw["visitor"].update({"phone_sessions_total": 3, "phone_sessions_reserved": 1})
    assert [open_session(client, visitor(rctx)).status_code for _ in range(2)] == [200, 200]
    r = open_session(client, visitor(rctx))
    assert (r.status_code, r.json()["error"]) == (429, "phone_sessions_busy")  # the last place is not theirs
    assert open_session(client, staff(rctx, "admin")).status_code == 200  # the presenter still gets a phone


def test_one_network_address_has_a_cap_of_its_own(client, rctx):
    rctx.policy.raw["visitor"].update({"phone_sessions_per_address": 2})
    assert [open_session(client, visitor(rctx)).status_code for _ in range(2)] == [200, 200]
    r = open_session(client, visitor(rctx))  # a third visitor on the same wifi address
    assert (r.status_code, r.json()["error"]) == (429, "phone_sessions_address")
    assert rctx.telephony.session_count() == 2


def test_a_session_carries_the_socket_time_limits_of_the_policy(client, rctx):
    sid = open_session(client, visitor(rctx)).json()["session"]
    s, caps = rctx.telephony.session(sid), rctx.policy["visitor"]
    assert (s.idle_s, s.max_age_s) == (caps["phone_socket_idle_s"], caps["phone_socket_max_s"])
    assert s.address == "testclient"


def test_a_clinic_logins_phone_is_that_clinics_desk_and_nobody_elses_is(client, rctx):
    """The server decides it from the login: the page cannot ask for a desk, and a visitor cannot be given one."""
    clinic = open_session(client, staff(rctx, "clinic", "cln_a")).json()["session"]
    assert rctx.telephony.session(clinic).clinic_id == "cln_a"
    viewer = open_session(client, visitor(rctx, "clinic")).json()["session"]  # a visitor viewing as a clinic
    assert rctx.telephony.session(viewer).clinic_id is None
    presenter = open_session(client, staff(rctx, "admin")).json()["session"]
    assert rctx.telephony.session(presenter).clinic_id is None


def test_a_clinic_login_with_no_clinic_gets_no_phone(client, rctx):
    r = open_session(client, staff(rctx, "clinic"))
    assert (r.status_code, r.json()["error"]) == (409, "no_clinic")
    assert rctx.telephony.session_count() == 0


def test_idle_sessions_stop_holding_a_cap_after_the_attach_minutes(client, rctx, fixed_clock):
    from datetime import timedelta

    rctx.policy.raw["visitor"].update({"phone_sessions_per_visitor": 1, "phone_session_attach_minutes": 10})
    who = visitor(rctx)
    assert open_session(client, who).status_code == 200
    assert open_session(client, who).status_code == 429
    fixed_clock.advance(timedelta(minutes=11))
    assert open_session(client, who).status_code == 200


def test_a_session_not_attached_within_the_attach_minutes_is_refused_and_gone(client, rctx, fixed_clock):
    from datetime import timedelta

    rctx.policy.raw["visitor"].update({"phone_session_attach_minutes": 10})
    who = visitor(rctx)
    sid = open_session(client, who).json()["session"]
    fixed_clock.advance(timedelta(minutes=11))
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/api/phone/ws?session={sid}", headers=who["cookie"]):
            pass
    assert e.value.code == routes_phone.WS_REFUSED
    assert rctx.telephony.session(sid) is None


def test_missed_calls_over_the_socket_are_capped_by_policy(client, rctx):
    rctx.policy.raw["visitor"].update({"phone_missed_calls_per_minute": 2})
    who = visitor(rctx)
    sid = open_session(client, who).json()["session"]
    with client.websocket_connect(f"/api/phone/ws?session={sid}", headers=who["cookie"]) as ws:
        assert ws.receive_json() == {"type": "ready"}
        for _ in range(2):
            ws.send_json({"type": "missed_call"})
            assert ws.receive_json() == {"type": "missed_call_received"}
        ws.send_json({"type": "missed_call"})
        refused = ws.receive_json()
        assert refused["type"] == "error" and "too many missed calls" in refused["message"]


def test_a_server_without_the_browser_phone_says_so(client, rctx):
    rctx.telephony = None
    r = open_session(client, visitor(rctx))
    assert (r.status_code, r.json()["error"]) == (409, "phone_off")


def test_the_browser_phone_is_found_beside_the_pbx(client, rctx):
    sim = rctx.telephony
    rctx.telephony = MuxPort(object(), sim)
    assert routes_phone.sim_port(rctx) is sim
    assert open_session(client, visitor(rctx)).status_code == 200
    rctx.telephony = object()
    assert routes_phone.sim_port(rctx) is None


def test_the_socket_says_ready_and_a_missed_call_reaches_the_port(client, rctx):
    who = visitor(rctx)
    sid = open_session(client, who).json()["session"]
    with client.websocket_connect(f"/api/phone/ws?session={sid}", headers=who["cookie"]) as ws:
        assert ws.receive_json() == {"type": "ready"}
        ws.send_json({"type": "missed_call"})
        assert ws.receive_json() == {"type": "missed_call_received"}
        ev = rctx.telephony._queue.get_nowait()
        assert isinstance(ev, MissedCall) and ev.caller_number == f"sim:{sid}"


def test_the_server_can_ring_the_socket_and_hear_the_answer(client, rctx):
    who = visitor(rctx)
    sid = open_session(client, who).json()["session"]
    with client.websocket_connect(f"/api/phone/ws?session={sid}", headers=who["cookie"]) as ws:
        ws.receive_json()
        call_id = client.portal.call(rctx.telephony.originate, f"sim:{sid}", "corr-1")
        assert ws.receive_json() == {"type": "ring", "call_id": call_id}
        ws.send_json({"type": "answer"})
        ws.send_json({"type": "bogus"})  # the reply to this proves the answer above was handled first
        assert ws.receive_json()["type"] == "error"
        ev = rctx.telephony._queue.get_nowait()
        assert isinstance(ev, CallAnswered) and ev.correlation_id == "corr-1"
        client.portal.call(rctx.telephony.play, call_id, ["chaar/hi/SYS_BYE"], "pb1")
        assert ws.receive_json() == {
            "type": "play",
            "playback_id": "pb1",
            "urls": ["/media/prompts/hi/SYS_BYE.wav"],
        }
        ws.send_bytes(b"x")  # audio nobody asked for is ignored, not an error


def test_closing_the_socket_frees_the_session_and_its_cap(client, rctx):
    rctx.policy.raw["visitor"]["phone_sessions_per_visitor"] = 1
    who = visitor(rctx)
    sid = open_session(client, who).json()["session"]
    with client.websocket_connect(f"/api/phone/ws?session={sid}", headers=who["cookie"]) as ws:
        ws.receive_json()
        assert open_session(client, who).status_code == 429
    for _ in range(100):
        if rctx.telephony.session(sid) is None:
            break
        client.portal.call(asyncio.sleep, 0.01)
    assert rctx.telephony.session(sid) is None
    assert open_session(client, who).status_code == 200


@pytest.mark.parametrize("case", ["no_cookie", "other_visitor", "unknown_session", "second_socket"])
def test_a_socket_that_is_not_the_owners_is_refused(client, rctx, case):
    who = visitor(rctx)
    sid = open_session(client, who).json()["session"]
    headers, session = who["cookie"], sid
    if case == "no_cookie":
        headers = {}
    elif case == "other_visitor":
        headers = visitor(rctx)["cookie"]
    elif case == "unknown_session":
        session = "nope"
    with client.websocket_connect(f"/api/phone/ws?session={sid}", headers=who["cookie"]) as first:
        first.receive_json()
        if case != "second_socket":
            first.close()
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect(f"/api/phone/ws?session={session}", headers=headers):
                pass
        assert e.value.code == routes_phone.WS_REFUSED


# ------------------------------------------------------------------ captions
def test_captions_are_public_and_name_the_words_of_each_clip(client, rctx):
    r = client.get(
        "/api/phone/captions", params={"lang": "hi"}
    )  # no cookie: the page asks before anyone signs in
    assert r.status_code == 200
    body = r.json()
    assert body["lang"] == "hi" and "needs" in body["review_status"]
    texts = body["texts"]
    assert texts["SYS_BYE"] == "धन्यवाद। नमस्ते।"  # a prompt with no slots: <id>
    assert texts["FC_CLOSE.2"] == "को"  # the third literal of a prompt with slots: <id>.<k>
    assert texts["slot/day_22"] == "बाईस"
    assert texts["slot/weekday_0"] == "सोमवार"
    assert texts["slot/month_10"] == "अक्टूबर"
    assert texts["slot/num_9"] == "नौ"
    assert texts["slot/time_0900"] == "सुबह नौ बजे"
    assert (
        texts["slot/clinic_cln_gmc"] == "सरकारी मेडिकल कॉलेज और अस्पताल जम्मू"
    )  # the city is part of the spoken name
    assert texts["slot/ward_12"] == "कृष्णा नगर"


def test_every_clip_of_the_voice_has_its_words_in_each_call_language(client):
    manifest = json.loads((DATA_DIR / "prompts" / "audio" / "MANIFEST.json").read_text(encoding="utf-8"))
    for lang in ("hi", "doi", "ur"):
        texts = client.get("/api/phone/captions", params={"lang": lang}).json()["texts"]
        files = {
            f["file"].split("/", 1)[1].removesuffix(".wav") for f in manifest["files"] if f["lang"] == lang
        }
        assert files <= set(texts), (lang, sorted(files - set(texts))[:5])
        assert all(words.strip() for words in texts.values())


def test_urdu_words_are_urdu_script_and_dogri_days_are_dogri(client):
    ur = client.get("/api/phone/captions", params={"lang": "ur"}).json()["texts"]
    assert ur["SYS_BYE"] == "شکریہ۔ خدا حافظ۔" and ur["slot/ward_1"] == "پنج تیرتھی"
    doi = client.get("/api/phone/captions", params={"lang": "doi"}).json()
    assert doi["texts"]["slot/day_22"] == "बाईं" and "MACHINE" in doi["review_status"]


@pytest.mark.parametrize("lang", ["", "en", "fr", "HI", "../hi"])
def test_captions_refuse_a_language_that_is_not_a_call_language(client, lang):
    r = client.get("/api/phone/captions", params={"lang": lang})
    assert (r.status_code, r.json()["error"]) == (400, "bad_request")


# --------------------------------------------------- the English beside the captions
def test_the_english_words_come_with_the_call_languages_captions_on_request(client):
    plain = client.get("/api/phone/captions", params={"lang": "hi"}).json()
    assert "en" not in plain  # asked for nothing, sent nothing
    body = client.get("/api/phone/captions", params={"lang": "hi", "with": "en"}).json()
    assert body["texts"] == plain["texts"]  # the call's own words are the same either way
    en = body["en"]
    assert en["texts"]["SYS_BYE"] == "Thank you. Goodbye."  # a line with no slots: its own clip key
    # a line with slots is on the key of its first file, and names each slot by where the call's language
    # puts it: Hindi says the total, then the dose number, then the date
    assert en["texts"]["FC_CLOSE.0"].startswith(
        "Thank you. Your next injection is injection {1.0} of {0.0}, on {1.1},"
    )
    assert "FC_CLOSE.1" not in en["texts"] and "FC_CLOSE.2" not in en["texts"]
    assert en["slots"]["slot/weekday_3"] == "Thursday" and en["slots"]["slot/month_10"] == "October"
    assert en["slots"]["slot/day_22"] == "22" and en["slots"]["slot/num_9"] == "9"
    assert en["slots"]["slot/time_0900"] == "9:00 am"
    assert en["slots"]["slot/clinic_cln_gmc"] == "Government Medical College and Hospital, Jammu"
    assert en["slots"]["slot/ward_12"] == "Krishna Nagar"


def test_every_line_and_slot_clip_of_each_call_language_has_its_english(client):
    prompts = json.loads((DATA_DIR / "prompts" / "prompts.json").read_text(encoding="utf-8"))["prompts"]
    for lang in ("hi", "doi", "ur"):
        body = client.get("/api/phone/captions", params={"lang": lang, "with": "en"}).json()
        en, texts = body["en"], body["texts"]
        assert len(en["texts"]) == len(prompts), lang  # every line has its English, on a clip that has words
        assert set(en["texts"]) <= set(texts), lang
        assert {k for k in texts if k.startswith("slot/")} == set(en["slots"]), lang
        slotted = {p["id"] for p in prompts if "{" in p["text"]["en"]}
        for key, line in en["texts"].items():
            places = re.findall(r"\{\d+\.\d+\}", line)
            assert bool(places) == (key.split(".")[0] in slotted), (lang, key)  # slots become places, no more
            assert "{" not in re.sub(r"\{\d+\.\d+\}", "", line), (lang, key)  # no slot name is left bare


def test_both_notices_have_their_words_and_their_english_in_every_call_language(client):
    prompts = json.loads((DATA_DIR / "prompts" / "prompts.json").read_text(encoding="utf-8"))["prompts"]
    english = {p["id"]: p["text"]["en"] for p in prompts if p["id"] in ("FC_NOTICE", "FC_NOTICE_FULL")}
    assert set(english) == {"FC_NOTICE", "FC_NOTICE_FULL"}
    for lang in ("hi", "doi", "ur"):
        body = client.get("/api/phone/captions", params={"lang": lang, "with": "en"}).json()
        for pid in ("FC_NOTICE", "FC_NOTICE_FULL"):
            assert body["texts"][pid].strip(), (lang, pid)  # the call language's words, on the clip's own key
            assert body["en"]["texts"][pid] == english[pid], (
                lang,
                pid,
            )  # and the English line, nothing placed in it
        assert "Data Protection Board of India" in body["en"]["texts"]["FC_NOTICE_FULL"]
        assert "press 0" in body["en"]["texts"]["FC_NOTICE"]
        assert len(body["texts"]["FC_NOTICE_FULL"]) > 2 * len(body["texts"]["FC_NOTICE"]), lang


def test_the_english_is_asked_for_in_en_and_nothing_else(client):
    for other in ("hi", "fr", "EN", ""):
        r = client.get("/api/phone/captions", params={"lang": "ur", "with": other})
        assert (r.status_code, r.json()["error"]) == (400, "bad_request"), other
