"""The role matrix: every route against every kind of person.

For a route a person may use, the answer is anything but 401 or 403 (the request is built to fail later,
with a 404 or 400, or to succeed). For a route they may not, it is exactly 401 when signed out, 403 when
signed in. Every write carries a good CSRF token, so a refusal here is the role and nothing else.
"""

from __future__ import annotations

from api_support import wav_bytes

CLINIC = {"clinic"}
TEAM = {"abc", "vet", "oversight", "admin"}
ALERT_READ = {"abc", "vet", "admin"}
RECORDS = {"abc", "vet", "admin"}
ANY = {"clinic", "abc", "vet", "oversight", "reviewer", "admin"}

# (method, path, json body or None, views that may use it). A visitor counts by the view it chose.
READS = [
    ("GET", "/api/me", None, ANY),
    # Patient level: the clinic's own people only. The administrator reads a clinic's settings and no patient.
    ("GET", "/api/clinic/today?clinic_id=cln_gmc", None, {"clinic"}),
    ("GET", "/api/clinic/config?clinic_id=cln_gmc", None, {"clinic", "admin"}),
    ("GET", "/api/courses/crs_nobody", None, {"clinic"}),
    ("GET", "/api/clinic/callbacks?clinic_id=cln_gmc", None, {"clinic"}),
    ("GET", "/api/coverage", None, TEAM),
    ("GET", "/api/clusters/{cluster}", None, TEAM),
    ("GET", "/api/wards", None, TEAM),
    ("GET", "/api/alerts", None, ALERT_READ),
    ("GET", "/api/worked", None, RECORDS),
    ("GET", "/api/worked/affected?ward_nos=1", None, RECORDS),
    ("GET", "/api/sight-counts", None, RECORDS),
    ("GET", "/api/oversight/summary", None, {"oversight", "reviewer", "admin"}),
    ("GET", "/api/exports/weekly.csv", None, {"oversight", "reviewer", "admin"}),
    ("GET", "/api/exports/register.csv", None, {"reviewer"}),
    ("GET", "/api/review/next", None, {"reviewer"}),
    ("GET", "/api/review/rep_nobody/audio", None, {"reviewer"}),
    ("GET", "/api/aliases", None, {"reviewer"}),
    ("GET", "/api/admin/clinics", None, {"admin"}),
    ("GET", "/api/admin/regimens", None, {"admin"}),
    ("GET", "/api/admin/people", None, {"admin"}),
    ("GET", "/api/admin/prompts", None, {"admin"}),
    ("GET", "/api/admin/retention", None, {"admin"}),
    ("GET", "/api/admin/audit", None, {"admin"}),
    ("GET", "/api/demo/state", None, ANY),
    ("GET", "/api/system/status", None, ANY),
]
PUBLIC = [
    ("GET", "/api/mode"),
    ("GET", "/api/phone/qr.svg?url=http://127.0.0.1:8080/phone"),
]
DAY = "2026-10-19"
WRITES = [
    (
        "POST",
        "/api/courses",
        {
            "clinic_id": "cln_gmc",
            "register_no": "R-1",
            "regimen_id": "id_trc",
            "lang": "hi",
            "day0": DAY,
            "eligibility": "adult",
        },
        CLINIC,
    ),  # fmt: skip
    ("POST", "/api/missed-calls/mc_nobody/link", {"course_id": "crs_nobody"}, CLINIC),
    ("POST", "/api/courses/crs_nobody/doses/2/given", {}, CLINIC),
    ("POST", "/api/courses/crs_nobody/stop", {"reason": "moved"}, CLINIC),
    ("POST", "/api/courses/crs_nobody/reveal-phone", None, CLINIC),
    ("POST", "/api/clinics/cln_gmc/vaccine", {"out": True}, CLINIC),
    ("POST", "/api/callbacks/cb_nobody/done", {}, CLINIC),
    ("POST", "/api/courses/crs_nobody/doses/2/unmark", None, CLINIC),
    ("POST", "/api/missed-calls/mc_nobody/unlink", None, CLINIC),
    ("POST", "/api/callbacks/cb_nobody/reopen", None, CLINIC),
    ("POST", "/api/alerts/alr_nobody/claim", {}, {"vet"}),
    ("POST", "/api/alerts/alr_nobody/acted", {}, {"vet"}),
    ("POST", "/api/alerts/alr_nobody/not-acted", {"note": "x"}, {"vet"}),
    ("POST", "/api/alerts/alr_nobody/callback", None, {"vet"}),
    ("POST", "/api/worked", {"ward_nos": [1], "worked_on": DAY}, {"abc", "vet"}),
    (
        "POST",
        "/api/sight-counts",
        {"ward_no": 1, "counted_on": DAY, "notched": 1, "unnotched": 1, "unsure": 0},
        {"abc", "vet"},
    ),  # fmt: skip
    ("POST", "/api/review/rep_nobody/decide", {"cant_tell": True}, {"reviewer"}),
    ("POST", "/api/aliases", {"text": "rani bagh", "ward_no": 1}, {"reviewer"}),
    (
        "POST",
        "/api/admin/clinics",
        {"name": "New clinic", "short_name": "New", "default_regimen": "id_trc", "default_lang": "hi"},
        {"admin"},
    ),  # fmt: skip
    ("PUT", "/api/admin/clinics", {"id": "cln_gmc", "visit_time": "10:00"}, {"admin"}),
    (
        "POST",
        "/api/admin/people",
        {"username": "newperson", "display_name": "New", "role": "abc", "password": "a long password"},
        {"admin"},
    ),  # fmt: skip
    ("PUT", "/api/admin/people", {"id": "stf_nobody", "active": True}, {"admin"}),
    ("POST", "/api/admin/signin-locks/clear", {"username": "nobody"}, {"admin"}),
    ("POST", "/api/demo/clock-lock", {"locked": False}, {"admin"}),
    ("POST", "/api/demo/clock-now", None, ANY),  # any signed-in staff; a visitor is read-only
]
WRITE_ROLES = ("clinic", "abc", "vet", "oversight", "reviewer", "admin")
VISITORS = ("clinic", "abc", "oversight", "reviewer", "admin")


def principals(people):
    """(name, who, view, is_visitor)"""
    out = [("out", people["out"], None, False)]
    out += [(r, people[r], r, False) for r in WRITE_ROLES]
    out += [(f"visitor-{v}", people[f"visitor-{v}"], v, True) for v in VISITORS]
    return out


def call(client, method, path, body, who, *, write):
    headers = who.write if write else who.read
    if method == "GET":
        return client.get(path, headers=headers)
    return client.request(method, path, json=body, headers=headers)


def check(client, method, path, body, allowed, people, *, write):
    for name, who, view, visitor in principals(people):
        res = call(client, method, path, body, who, write=write)
        permitted = view in allowed and not (write and visitor)
        if name == "out":
            assert res.status_code == 401, (method, path, name, res.status_code, res.text)
        elif permitted:
            assert res.status_code not in (401, 403), (method, path, name, res.status_code, res.text)
        else:
            assert res.status_code == 403, (method, path, name, res.status_code, res.text)
            if write and visitor:
                assert res.json()["error"] == "read_only", (method, path, name)


def test_every_read_by_every_person(client, people, ctx):
    cluster = ctx.clusters.all()[0].id
    for method, path, body, allowed in READS:
        check(client, method, path.replace("{cluster}", cluster), body, allowed, people, write=False)


def test_public_routes_need_no_login(client):
    for method, path in PUBLIC:
        res = client.request(method, path)
        assert res.status_code == 200, (path, res.text)


def test_every_write_by_every_person(client, people):
    for method, path, body, allowed in WRITES:
        check(client, method, path, body, allowed, people, write=True)


def test_the_prompt_upload_is_admin_only(client, people):
    path = "/api/admin/prompts/hi/FC_HELLO.0"
    for name, who, view, visitor in principals(people):
        res = client.post(path, content=wav_bytes(), headers=who.write)
        if name == "out":
            assert res.status_code == 401
        elif view == "admin" and not visitor:
            assert res.status_code not in (401, 403), res.text
        else:
            assert res.status_code == 403


def test_the_demo_reset_is_the_administrators_alone(client, people):
    for name, who, view, visitor in principals(people):
        if view == "admin" and not visitor:
            continue  # the reset itself is tested on its own: it ends every staff login
        res = client.post("/api/demo/reset", headers=who.write)
        assert res.status_code == (401 if name == "out" else 403), (name, res.text)


def test_a_visitor_may_move_the_demo_clock_and_open_a_phone_but_write_nothing_else(
    client, people, moving_clock
):
    for view in VISITORS:
        who = people[f"visitor-{view}"]
        phone = client.post("/api/phone/sessions", headers=who.write)
        assert phone.status_code == 200, (view, phone.text)
        jump = client.post("/api/demo/jump", json={"to": "next_call"}, headers=who.write)
        assert jump.status_code in (200, 409), (view, jump.text)
        assert jump.status_code != 403
    for method, path, body, _allowed in WRITES:
        res = call(client, method, path, body, people["visitor-clinic"], write=True)
        assert res.status_code == 403 and res.json()["error"] == "read_only", (path, res.text)


def test_signing_out_needs_only_a_login(client, people):
    for name, who, _view, _visitor in principals(people):
        res = client.post("/api/auth/signout", headers=who.write)
        assert res.status_code == 200, (name, res.text)
