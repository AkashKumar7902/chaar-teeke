"""The 19 Oct contract additions: the public mode, the kept demo session key, call-back events and outcomes,
who an alert was taken by, what the team stream may carry, the worked-wards count, a missing page and the
content security policy with no outside host."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import re
import stat
from pathlib import Path

import pytest
from api_support import Who
from fastapi.testclient import TestClient
from test_sse import open_stream

from chaarteeke import alerts, clock, enrol
from chaarteeke.api import auth
from chaarteeke.api.app import CSP, NOT_FOUND_PAGE, PAGES, create_app
from chaarteeke.config import WEB_DIR


# ------------------------------------------------------------------- /api/mode
def test_mode_for_nobody_says_not_signed_in_and_is_never_a_401(client):
    res = client.get("/api/mode")
    assert res.status_code == 200
    assert res.json()["signed_in"] is False and res.json()["view"] is None
    forged = client.get("/api/mode", headers={"cookie": f"{auth.COOKIE}=not-a-real-token"})
    assert forged.status_code == 200 and forged.json()["signed_in"] is False


def test_mode_says_the_view_of_whoever_is_signed_in(client, people):
    for role in ("clinic", "abc", "vet", "oversight", "reviewer", "admin"):
        body = client.get("/api/mode", headers=people[role].read).json()
        assert body["signed_in"] is True and body["view"] == role, role
    for view in auth.VISITOR_VIEWS:
        body = client.get("/api/mode", headers=people[f"visitor-{view}"].read).json()
        assert body["signed_in"] is True and body["view"] == view, view


def test_mode_gives_the_public_url_or_null(make_app, clock_t0):
    some = make_app(seeded=False, public_url="https://ct.example.org")
    clock.install(clock_t0)
    assert TestClient(some).get("/api/mode").json()["public_url"] == "https://ct.example.org"
    none = make_app(seeded=False, public_url="")
    clock.install(clock_t0)
    assert TestClient(none).get("/api/mode").json()["public_url"] is None


def test_mode_after_the_session_ends_is_signed_out(client, people):
    who = people["clinic"]
    assert client.get("/api/mode", headers=who.read).json()["signed_in"] is True
    client.post("/api/auth/signout", headers=who.write)
    assert client.get("/api/mode", headers=who.read).json()["signed_in"] is False


# ---------------------------------------------------------- the kept demo secret
def test_a_demo_session_survives_a_restart(make_app, clock_t0):
    first = make_app(secret_key="")
    clock.install(clock_t0)
    ctx = first.state.ctx
    token, _csrf = auth.enter_as_visitor(ctx, "abc")
    key = Path(ctx.settings.var_dir) / "demo_secret.key"
    assert key.is_file() and key.read_text().strip() == ctx.settings.secret_key
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert ctx.extras["generated_secret"] is True
    again = create_app(ctx.settings.model_copy(update={"secret_key": ""}), start_background=False)
    try:
        clock.install(clock_t0)
        assert again.state.ctx.settings.secret_key == ctx.settings.secret_key
        res = TestClient(again).get("/api/me", headers={"cookie": f"{auth.COOKIE}={token}"})
        assert res.status_code == 200 and res.json()["role"] == "visitor"
    finally:
        again.state.ctx.db.close()


def test_a_secret_given_in_the_environment_is_used_and_nothing_is_written(make_app, clock_t0):
    app = make_app(secret_key="from-the-environment")
    assert app.state.ctx.settings.secret_key == "from-the-environment"
    assert not (Path(app.state.ctx.settings.var_dir) / "demo_secret.key").exists()
    assert app.state.ctx.extras["generated_secret"] is False


def test_an_empty_secret_file_stops_the_start_instead_of_signing_with_nothing(tmp_path):
    from chaarteeke.crypto import demo_secret

    (tmp_path / "demo_secret.key").write_text("")
    with pytest.raises(RuntimeError, match="empty"):
        demo_secret(tmp_path)


def test_the_secret_file_is_private_from_the_first_byte_and_left_no_temporary_file(tmp_path):
    from chaarteeke.crypto import demo_secret

    key = demo_secret(tmp_path / "var")
    assert key and demo_secret(tmp_path / "var") == key
    assert [p.name for p in (tmp_path / "var").iterdir()] == ["demo_secret.key"]
    assert stat.S_IMODE((tmp_path / "var" / "demo_secret.key").stat().st_mode) == 0o600


# ------------------------------------------------------------ call-backs and alerts
def claimable_alert(ctx):
    row = ctx.db.one(
        "SELECT a.id, c.id AS course_id FROM alerts a JOIN bite_reports r ON r.id = a.report_id"
        " JOIN courses c ON c.id = r.course_id WHERE a.status = 'new' AND c.clinic_id = 'cln_gmc' AND r.is_demo = 1"
        " AND NOT EXISTS (SELECT 1 FROM callback_requests cb WHERE cb.alert_id = a.id) LIMIT 1"
    )
    assert row is not None
    return row


def test_a_callback_can_be_closed_as_could_not_reach(client, ctx, people):
    alert = claimable_alert(ctx)
    vet, clinic = people["vet"], people["clinic"]
    client.post(f"/api/alerts/{alert['id']}/claim", json={}, headers=vet.write)
    cb = client.post(f"/api/alerts/{alert['id']}/callback", headers=vet.write).json()
    done = client.post(
        f"/api/callbacks/{cb['id']}/done",
        json={"outcome": "closed_no_contact", "note": "Phone off twice"},
        headers=clinic.write,
    )
    assert done.status_code == 200 and done.json()["status"] == "closed_no_contact"
    listed = client.get("/api/alerts", headers=vet.read).json()
    assert next(a for a in listed if a["id"] == alert["id"])["callback_status"] == "closed_no_contact"
    again = client.post(f"/api/callbacks/{cb['id']}/done", json={}, headers=clinic.write)
    assert again.status_code == 409 and again.json()["error"] == "already_closed"


def test_an_alert_says_whether_the_person_asking_took_it(client, ctx, people):
    alert = claimable_alert(ctx)["id"]
    client.post(f"/api/alerts/{alert}/claim", json={}, headers=people["vet"].write)
    for role, mine in (("vet", True), ("abc", False), ("admin", False)):
        row = next(a for a in client.get("/api/alerts", headers=people[role].read).json() if a["id"] == alert)
        assert row["claimed_by_me"] is mine, role
        assert row["claimed_by"] == "staff:vet"
    every = client.get("/api/alerts", headers=people["vet"].read).json()
    assert [a["id"] for a in every if a["claimed_by_me"]] and all(
        a["claimed_by_me"] is (a["claimed_by"] == "staff:vet") for a in every
    )  # true exactly where this vet took it; false for new alerts and other people's


def test_the_reply_to_a_claim_says_it_is_mine(client, ctx, people):
    alert = claimable_alert(ctx)["id"]
    res = client.post(f"/api/alerts/{alert}/claim", json={}, headers=people["vet"].write)
    assert res.json()["claimed_by_me"] is True
    closed = client.post(
        f"/api/alerts/{alert}/acted", json={"note": "Dog caught"}, headers=people["vet"].write
    )
    assert closed.json()["claimed_by_me"] is True and closed.json()["status"] == "acted"


# --------------------------------------------------------------------- the stream
async def test_a_callback_request_and_its_closing_reach_the_clinic_and_the_team(app, ctx, people):
    alert = claimable_alert(ctx)
    mine = await open_stream(app, people["clinic"])
    team = await open_stream(app, people["abc"])
    viewer = await open_stream(app, people["visitor-clinic"])
    oversight = await open_stream(app, people["oversight"])
    alerts.claim(ctx, alert["id"], "staff:vet")
    cb = alerts.request_callback(ctx, alert["id"], "staff:vet")
    alerts.callback_done(ctx, cb.id, "staff:clinic", outcome="closed_no_contact")
    await asyncio.sleep(0.2)
    for s in (mine, team, viewer):
        got = [e["data"] for e in s.events if e["type"] == "callback"]
        assert got == [
            {"callback_id": cb.id, "alert_id": alert["id"], "status": "open"},
            {"callback_id": cb.id, "alert_id": alert["id"], "status": "closed_no_contact"},
        ]
        assert alert["course_id"] not in s.text  # no stream names the patient's course
    assert not [e for e in oversight.events if e["type"] == "callback"]
    for s in (mine, team, viewer, oversight):
        await s.close()


async def test_a_callback_for_another_clinic_stays_with_that_clinic(app, ctx, people):
    alert = claimable_alert(ctx)
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO staff(id, username, display_name, role, clinic_id, password_hash, active, is_demo)"
            " VALUES ('stf_other','other','Other desk','clinic','cln_demo2',?,1,1)",
            (auth.hash_password("a long password"),),
        )
    token, csrf = auth.sign_in(ctx, "other", "a long password")
    other = await open_stream(app, Who(token, csrf))
    mine = await open_stream(app, people["clinic"])
    alerts.claim(ctx, alert["id"], "staff:vet")
    alerts.request_callback(ctx, alert["id"], "staff:vet")
    await asyncio.sleep(0.2)
    assert [e["type"] for e in mine.events] == ["callback"]
    assert other.events == []
    await other.close()
    await mine.close()


async def test_the_team_gets_simulated_calls_and_missed_calls_only(app, ctx, people):
    abc, vet = await open_stream(app, people["abc"]), await open_stream(app, people["vet"])
    clinic = await open_stream(app, people["clinic"])
    sim = enrol.record_missed_call(ctx, "sim:session-1", "sim", clock.now())
    real = enrol.record_missed_call(ctx, "9876543210", "ari", clock.now())
    course = ctx.db.one("SELECT id FROM courses WHERE clinic_id = 'cln_gmc' LIMIT 1")["id"]
    ctx.bus.publish("call", {"call_id": "c1", "course_id": course, "kind": "reminder", "status": "ringing", "simulated": True})  # fmt: skip
    ctx.bus.publish("call", {"call_id": "c2", "course_id": course, "kind": "reminder", "status": "ringing", "simulated": False})  # fmt: skip
    await asyncio.sleep(0.2)
    for s in (abc, vet):
        types = [(e["type"], e["data"]) for e in s.events]
        assert (
            "call",
            {"call_id": "c1", "kind": "reminder", "status": "ringing", "simulated": True},
        ) in types
        calls = [e["data"] for e in s.events if e["type"] == "call"]
        assert all(
            c["simulated"] is True and set(c) == {"call_id", "kind", "status", "simulated"} for c in calls
        )
        assert "c2" not in {c["call_id"] for c in calls}  # the real call is not carried
        missed = [e["data"] for e in s.events if e["type"] == "missed_call"]
        assert [m["id"] for m in missed] == [sim.missed_call_id]  # the real missed call is not carried
        assert set(missed[0]) == {"id", "status"}
        assert course not in s.text and "phone_last4" not in s.text and "3210" not in s.text
    assert (
        real.status == "unlinked" and "3210" in clinic.text
    )  # the clinic still gets its own, last four only
    for s in (abc, vet, clinic):
        await s.close()


async def test_oversight_reviewer_and_admin_get_no_call_events(app, ctx, people):
    streams = {r: await open_stream(app, people[r]) for r in ("oversight", "reviewer", "admin")}
    enrol.record_missed_call(ctx, "sim:session-2", "sim", clock.now())
    ctx.bus.publish("call", {"call_id": "c1", "course_id": None, "kind": "first", "status": "ringing", "simulated": True})  # fmt: skip
    await asyncio.sleep(0.2)
    for s in streams.values():
        assert not [e for e in s.events if e["type"] in ("call", "missed_call", "callback")]
        await s.close()


# ----------------------------------------------------------- worked-wards count
_made = itertools.count(1)


def add_patient(ctx, n, ward, *, status="active", source="clinic", dose_status="due", due="2026-10-22",
                location="matched"):  # fmt: skip
    with ctx.db.transaction() as conn:
        for i in range(n):
            k = next(_made)
            cid, rid = f"crs_wa_{k}", f"rep_wa_{k}"
            conn.execute(
                "INSERT INTO courses(id, clinic_id, register_no, regimen_id, day0, lang, status, eligibility, source, created_at, is_demo)"
                " VALUES (?,'cln_gmc',?,'id_trc','2026-10-16','hi',?,'adult',?,'2026-10-16T05:00:00.000Z',1)",
                (cid, f"WA{i}", status, source),
            )
            conn.execute(
                "INSERT INTO doses(course_id, dose_no, offset_days, due_date, status) VALUES (?,2,3,?,?)",
                (cid, due, dose_status),
            )
            conn.execute(
                "INSERT INTO bite_reports(id, course_id, clinic_id, bite_week, location_status, ward_no, source, created_at, is_demo)"
                " VALUES (?,?,'cln_gmc','2026-10-12',?,?,?,'2026-10-16T05:00:00.000Z',1)",
                (rid, cid, location, ward, source),
            )


def affected(client, who, wards):
    return client.get(f"/api/worked/affected?ward_nos={wards}", headers=who.read)


def test_the_count_of_patients_with_doses_ahead_in_worked_wards(client, ctx, people, wards):
    a, b, c = wards[0], wards[1], wards[2]
    add_patient(ctx, 7, a)  # counted
    add_patient(ctx, 6, b)  # counted
    add_patient(ctx, 9, a, status="completed")  # not active
    add_patient(ctx, 9, a, dose_status="given")  # nothing ahead
    add_patient(ctx, 9, a, due="2026-10-18")  # the dose date has passed
    add_patient(ctx, 9, a, location="needs_review")  # not placed in a ward yet
    add_patient(ctx, 9, a, source="demo")  # demo-seeded courses are never called
    add_patient(ctx, 3, c)
    abc = people["abc"]
    assert affected(client, abc, a).json() == {"courses_with_doses_ahead": 7}
    assert affected(client, abc, f"{a},{b}").json() == {"courses_with_doses_ahead": 13}
    assert affected(client, abc, f"{a}, {b} ,{a}").json() == {
        "courses_with_doses_ahead": 13
    }  # spaces, repeats
    assert affected(client, abc, c).json() == {"courses_with_doses_ahead": "<5"}  # 3 patients: small cell
    assert affected(client, abc, wards[3]).json() == {
        "courses_with_doses_ahead": "<5"
    }  # none: not a bare zero


def test_a_dose_due_today_or_a_line_already_heard_does_not_count(client, ctx, people, wards):
    a = wards[0]
    add_patient(ctx, 6, a)  # counted
    add_patient(ctx, 9, a, due=clock.local_today().isoformat())  # its reminder went out yesterday
    before = {r["id"] for r in ctx.db.query("SELECT id FROM courses")}
    add_patient(ctx, 9, a)  # would count, but has heard the line (it is said once)
    heard = {r["id"] for r in ctx.db.query("SELECT id FROM courses")} - before
    with ctx.db.transaction() as conn:
        for course_id in heard:
            conn.execute("INSERT INTO kv(key, value) VALUES (?, 'x')", (f"team_line:{course_id}",))
    assert affected(client, people["abc"], a).json() == {"courses_with_doses_ahead": 6}


def test_the_count_refuses_bad_input_and_unknown_wards(client, people, wards):
    abc = people["abc"]
    assert affected(client, abc, "").status_code == 400
    assert affected(client, abc, "x,2").status_code == 400
    assert client.get("/api/worked/affected", headers=abc.read).status_code == 400
    nowhere = affected(client, abc, "99999")
    assert nowhere.status_code == 404 and nowhere.json()["error"] == "unknown_ward"
    too_many = affected(client, abc, ",".join(str(i) for i in range(1, 102)))
    assert too_many.status_code == 400


def test_the_count_carries_no_patient(client, ctx, people, wards):
    add_patient(ctx, 8, wards[0])
    body = affected(client, people["vet"], wards[0])
    assert body.status_code == 200 and set(body.json()) == {"courses_with_doses_ahead"}
    assert "crs_" not in body.text and "rep_" not in body.text


# ------------------------------------------------------------ a page with no shell
def test_a_page_whose_shell_is_missing_serves_the_404_page_not_a_500(make_app, clock_t0, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / NOT_FOUND_PAGE).write_text("<!doctype html><title>Not found page</title>")
    (web / PAGES["/clinic"]).write_text("<!doctype html><title>Clinic page</title>")
    app = make_app(seeded=False, web_dir=web)
    clock.install(clock_t0)
    client = TestClient(app)
    assert client.get("/clinic").status_code == 200
    for route in ("/oversight", "/review", "/admin", "/method", "/team"):
        res = client.get(route)
        assert res.status_code == 404 and "Not found page" in res.text, route
        assert res.headers["content-security-policy"] == CSP


def test_with_no_404_page_either_a_missing_shell_is_a_json_404(make_app, clock_t0, tmp_path):
    web = tmp_path / "bare"
    web.mkdir()
    app = make_app(seeded=False, web_dir=web)
    clock.install(clock_t0)
    res = TestClient(app).get("/team")
    assert res.status_code == 404 and res.json()["error"] == "not_found"


# ------------------------------------------------------- the policy and vendored map
def test_the_csp_names_no_outside_host():
    directives = [d.strip() for d in CSP.split(";") if d.strip()]
    allowed = {"'self'", "'none'", "data:", "blob:"}  # no bare ws: or wss: (any host): see csp_for
    for directive in directives:
        name, *sources = directive.split()
        assert set(sources) <= allowed, f"{name} lists a source other than this server: {sources}"
    assert "connect-src 'self';" in CSP
    assert not re.search(r"https?://|\.[a-z]{2,}\b", CSP)
    assert "unpkg" not in CSP and "unsafe-inline" not in CSP and "unsafe-eval" not in CSP
    assert "script-src 'self';" in CSP and "style-src 'self';" in CSP


def test_the_map_library_is_served_from_this_server(client):
    base = "/vendor/maplibre-gl-4.7.1"
    js = client.get(f"{base}/maplibre-gl.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    css = client.get(f"{base}/maplibre-gl.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert client.get(f"{base}/LICENSE.txt").status_code == 200
    assert js.headers["content-security-policy"] == CSP


def test_the_vendored_map_library_matches_its_checksums_and_version():
    folder = WEB_DIR / "vendor" / "maplibre-gl-4.7.1"
    lines = (folder / "SHA256SUMS").read_text().splitlines()
    listed = dict(reversed(ln.split("  ", 1)) for ln in lines if ln.strip())
    assert set(listed) == {"maplibre-gl.js", "maplibre-gl.css", "LICENSE.txt"}
    for name, digest in listed.items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest, name
    assert "v4.7.1" in (folder / "maplibre-gl.js").read_text(encoding="utf-8")[:400]


def test_no_page_or_script_loads_anything_from_an_outside_host():
    from_outside = re.compile(
        r"""(?:src|href)=["']https?://|import\s[^;]*from\s+["']https?://|\bunpkg\.com"""
    )
    offenders = []
    for path in [*WEB_DIR.glob("*.html"), *WEB_DIR.glob("js/**/*.js"), *WEB_DIR.glob("js/**/*.mjs")]:
        if from_outside.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(WEB_DIR)))
    assert offenders == []


@pytest.mark.parametrize("sub", ["css", "js", "fonts", "img", "i18n", "vendor"])
def test_every_static_folder_the_pages_use_is_mounted(client, sub):
    assert any(getattr(r, "path", None) == f"/{sub}" for r in client.app.routes)


# ----------------------------------------- the keys the server names exist in the page bundles
def bundle(lang: str) -> dict:
    return json.loads((WEB_DIR / "i18n" / f"common.{lang}.json").read_text(encoding="utf-8"))


def test_the_band_label_key_is_in_both_bundles_and_says_what_the_server_says(client, people):
    body = client.get("/api/coverage", headers=people["abc"].read).json()
    key_group, key = body["claim_label_key"].split(".")
    lo, hi = (round(x * 100) for x in body["claim_band"])
    assert bundle("en")[key_group][key].format(lo=lo, hi=hi) == body["claim_label"]
    hindi = bundle("hi")[key_group][key]
    assert "{lo}" in hindi and "{hi}" in hindi


def test_the_look_here_reason_data_fills_the_english_bundle_to_the_servers_sentence():
    from chaarteeke.policy import load_policy
    from chaarteeke.stats.lookhere import Counts, evaluate

    policy = load_policy()
    # the city counts include the cluster being assessed; the rest of the city is 160 notched, 40 not
    strong = evaluate(policy, Counts(220, 180), {"c1": Counts(60, 140)})[0]
    en = bundle("en")["lookHere"]
    assert en["strong"].format(n=strong.data["n"]) == strong.reason
    hi = bundle("hi")["lookHere"]
    assert set(hi) == set(en) == {"strong"}  # one rule, so one reason


def test_the_alert_card_words_exist_in_english_and_hindi():
    for lang in ("en", "hi"):
        alert = bundle(lang)["alert"]
        for key in ("takenByMe", "takenBy", "you", "title", "callback_closed_no_contact"):
            assert alert[key], (lang, key)
        assert "{name}" in alert["takenBy"]
