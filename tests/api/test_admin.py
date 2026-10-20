"""The administrator: clinics, people, voice prompts and retention."""

from __future__ import annotations

import pytest
from api_support import wav_bytes

from chaarteeke import clock
from chaarteeke.api import auth

GOOD_PASSWORD = "a long password"


# --------------------------------------------------------------------- clinics
def test_clinics_list_shows_visit_time_phone_and_what_is_missing(client, people):
    rows = {c["id"]: c for c in client.get("/api/admin/clinics", headers=people["admin"].read).json()}
    assert {"cln_gmc", "cln_demo2", "cln_demo_visitor"} <= set(rows)
    gmc = rows["cln_gmc"]
    assert gmc["visit_time"] == "09:00" and gmc["phone"] is None
    assert any("phone number" in gap for gap in gmc["setup_gaps"])
    assert rows["cln_demo2"]["phone"] and rows["cln_demo2"]["setup_gaps"] == []


def test_changing_a_clinics_visit_time_and_phone(client, ctx, people):
    admin = people["admin"].write
    res = client.put("/api/admin/clinics", json={"id": "cln_gmc", "visit_time": "10:00", "phone": "+91 99999 04567"}, headers=admin)  # fmt: skip
    assert res.status_code == 200
    assert (res.json()["visit_time"], res.json()["phone"]) == ("10:00", "+9199999 04567".replace(" ", ""))
    assert res.json()["setup_gaps"] == []
    row = ctx.db.one("SELECT visit_time, phone, name FROM clinics WHERE id = 'cln_gmc'")
    assert (row["visit_time"], row["phone"]) == ("10:00", "+919999904567") and row["name"].startswith("GMC")
    audit = ctx.db.one("SELECT detail_json FROM audit WHERE action = 'clinic.update'")
    assert audit["detail_json"] == '{"fields": ["phone", "visit_time"]}'


def test_a_visit_time_or_phone_sent_as_null_is_cleared(client, ctx, people):
    res = client.put("/api/admin/clinics", json={"id": "cln_demo2", "visit_time": None, "phone": None}, headers=people["admin"].write)  # fmt: skip
    assert res.status_code == 200 and res.json()["visit_time"] is None and len(res.json()["setup_gaps"]) == 2


@pytest.mark.parametrize(
    "patch",
    [{"visit_time": "9am"}, {"visit_time": "25:00"}, {"phone": "call me"}, {"phone": "12345"}, {"default_lang": "en"}, {"name": None}],
)  # fmt: skip
def test_bad_clinic_fields_are_refused_and_change_nothing(client, ctx, people, patch):
    before = dict(ctx.db.one("SELECT * FROM clinics WHERE id = 'cln_gmc'"))
    res = client.put("/api/admin/clinics", json={"id": "cln_gmc", **patch}, headers=people["admin"].write)
    assert res.status_code == 400, res.text
    assert dict(ctx.db.one("SELECT * FROM clinics WHERE id = 'cln_gmc'")) == before


def test_clinic_regimen_ward_and_unknown_clinic(client, people):
    admin = people["admin"].write
    assert client.put("/api/admin/clinics", json={"id": "cln_gmc", "default_regimen": "id_1week"}, headers=admin).json()["error"] == "regimen_disabled"  # fmt: skip
    assert client.put("/api/admin/clinics", json={"id": "cln_gmc", "ward_no": 9999}, headers=admin).json()["error"] == "unknown_ward"  # fmt: skip
    assert (
        client.put("/api/admin/clinics", json={"id": "cln_nowhere", "name": "x"}, headers=admin).status_code
        == 404
    )


def test_adding_a_clinic(client, ctx, people):
    body = {"name": "Satwari urban clinic", "short_name": "Satwari", "default_regimen": "im_essen", "default_lang": "doi", "visit_time": "10:00"}  # fmt: skip
    res = client.post("/api/admin/clinics", json=body, headers=people["admin"].write)
    assert res.status_code == 200
    made = res.json()
    assert (
        made["id"].startswith("cln_") and made["arv_status"] == "unverified" and made["visit_time"] == "10:00"
    )
    assert any("phone number" in g for g in made["setup_gaps"])
    assert ctx.db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'clinic.create' AND object_id = ?", (made["id"],))["n"] == 1  # fmt: skip


# ---------------------------------------------------------------------- people
def person(client, admin, **fields):
    body = {
        "username": "newperson",
        "display_name": "New Person",
        "role": "abc",
        "password": GOOD_PASSWORD,
        **fields,
    }
    return client.post("/api/admin/people", json=body, headers=admin.write)


def test_people_list_has_no_password_hash(client, people):
    rows = client.get("/api/admin/people", headers=people["admin"].read).json()
    assert {r["username"] for r in rows} >= {"clinic", "abc", "vet", "oversight", "reviewer", "admin"}
    assert all(set(r) == {"id", "username", "display_name", "role", "clinic_id", "clinic_name", "active", "is_demo"} for r in rows)  # fmt: skip
    assert "scrypt" not in str(rows)


def test_adding_a_person_who_can_then_sign_in(client, ctx, people):
    res = person(client, people["admin"])
    assert res.status_code == 200 and res.json()["role"] == "abc" and res.json()["clinic_id"] is None
    assert "scrypt" not in res.text and GOOD_PASSWORD not in res.text
    signin = client.post("/api/auth/signin", json={"username": "newperson", "password": GOOD_PASSWORD})
    assert signin.status_code == 200 and signin.json()["role"] == "abc"
    detail = ctx.db.one("SELECT detail_json FROM audit WHERE action = 'staff.create'")["detail_json"]
    assert GOOD_PASSWORD not in detail


def test_the_stored_hash_is_the_same_scrypt_format_the_demo_seed_uses(client, ctx, people, logins):
    person(client, people["admin"])
    mine = ctx.db.one("SELECT password_hash FROM staff WHERE username = 'newperson'")["password_hash"]
    seeded = ctx.db.one("SELECT password_hash FROM staff WHERE username = 'admin'")["password_hash"]
    assert mine.split("$")[0] == seeded.split("$")[0] == "scrypt"
    assert [len(p) for p in mine.split("$")] == [len(p) for p in seeded.split("$")]
    assert auth.verify_password(logins["admin"], seeded) and auth.verify_password(GOOD_PASSWORD, mine)


def test_a_clinic_person_needs_a_real_clinic_and_others_get_none(client, people):
    admin = people["admin"]
    assert person(client, admin, role="clinic").json()["error"] == "bad_request"
    assert person(client, admin, role="clinic", clinic_id="cln_nowhere").status_code == 404
    ok = person(client, admin, role="clinic", clinic_id="cln_demo2")
    assert ok.status_code == 200 and ok.json()["clinic_name"] == "Demo clinic (unverified)"
    other = person(client, admin, username="vet-two", role="vet", clinic_id="cln_demo2")
    assert other.status_code == 200 and other.json()["clinic_id"] is None


@pytest.mark.parametrize("fields", [{"username": "Bad Name"}, {"username": "ab"}, {"username": "x" * 40}, {"password": "short"}, {"role": "owner"}])  # fmt: skip
def test_bad_people_are_refused(client, people, fields):
    assert person(client, people["admin"], **fields).status_code == 400


def test_a_username_is_taken_once(client, people):
    assert person(client, people["admin"], username="clinic").status_code == 409
    assert person(client, people["admin"]).status_code == 200
    assert person(client, people["admin"]).json()["error"] == "username_taken"


def find(client, people, username):
    return next(r for r in client.get("/api/admin/people", headers=people["admin"].read).json() if r["username"] == username)  # fmt: skip


def test_a_password_reset_works_and_ends_that_persons_logins(client, ctx, people, logins):
    target = find(client, people, "abc")
    res = client.put("/api/admin/people", json={"id": target["id"], "password": "brand new password"}, headers=people["admin"].write)  # fmt: skip
    assert res.status_code == 200
    assert client.get("/api/me", headers=people["abc"].read).status_code == 401
    assert (
        client.post("/api/auth/signin", json={"username": "abc", "password": logins["abc"]}).status_code
        == 401
    )
    assert client.post("/api/auth/signin", json={"username": "abc", "password": "brand new password"}).status_code == 200  # fmt: skip
    audit = ctx.db.one("SELECT detail_json FROM audit WHERE action = 'staff.update'")["detail_json"]
    assert (
        "brand new password" not in audit
        and "password_hash" not in audit
        and '"password_reset": true' in audit
    )


def test_a_weak_reset_password_is_refused_and_the_old_one_still_works(client, people, logins):
    target = find(client, people, "abc")
    res = client.put(
        "/api/admin/people", json={"id": target["id"], "password": "short"}, headers=people["admin"].write
    )
    assert res.status_code == 400 and res.json()["error"] == "weak_password"
    assert (
        client.post("/api/auth/signin", json={"username": "abc", "password": logins["abc"]}).status_code
        == 200
    )


def test_switching_a_person_off_ends_their_logins(client, people, logins):
    target = find(client, people, "vet")
    res = client.put(
        "/api/admin/people", json={"id": target["id"], "active": False}, headers=people["admin"].write
    )
    assert res.status_code == 200 and res.json()["active"] is False
    assert client.get("/api/me", headers=people["vet"].read).status_code == 401
    assert (
        client.post("/api/auth/signin", json={"username": "vet", "password": logins["vet"]}).status_code
        == 401
    )


def test_changing_a_role_ends_logins_and_clears_the_clinic(client, people):
    target = find(client, people, "clinic")
    res = client.put(
        "/api/admin/people", json={"id": target["id"], "role": "abc"}, headers=people["admin"].write
    )
    assert res.status_code == 200 and (res.json()["role"], res.json()["clinic_id"]) == ("abc", None)
    assert client.get("/api/me", headers=people["clinic"].read).status_code == 401


def test_the_last_working_administrator_cannot_be_switched_off_or_demoted(client, people):
    me = find(client, people, "admin")
    off = client.put(
        "/api/admin/people", json={"id": me["id"], "active": False}, headers=people["admin"].write
    )
    assert off.status_code == 409 and off.json()["error"] == "last_admin"
    down = client.put(
        "/api/admin/people", json={"id": me["id"], "role": "vet"}, headers=people["admin"].write
    )
    assert down.status_code == 409 and down.json()["error"] == "last_admin"
    assert client.get("/api/me", headers=people["admin"].read).status_code == 200
    person(client, people["admin"], username="second-admin", role="admin")
    ok = client.put(
        "/api/admin/people", json={"id": me["id"], "active": False}, headers=people["admin"].write
    )
    assert ok.status_code == 200


def test_a_reset_for_nobody_is_a_404(client, people):
    res = client.put("/api/admin/people", json={"id": "stf_nobody", "password": GOOD_PASSWORD}, headers=people["admin"].write)  # fmt: skip
    assert res.status_code == 404


# --------------------------------------------------------------------- prompts
def lang(body, code):
    return next(entry for entry in body["languages"] if entry["lang"] == code)


def test_prompts_list_every_language_with_its_prompts_and_nothing_missing_with_the_full_voice(client, people):
    body = client.get("/api/admin/prompts", headers=people["admin"].read).json()
    assert [entry["lang"] for entry in body["languages"]] == ["hi", "doi", "ur"]
    hi = lang(body, "hi")
    assert hi["ready"] is True and hi["missing"] == [] and hi["name"] == "Hindi"
    hello = next(p for p in hi["prompts"] if p["id"] == "FC_HELLO")
    assert hello["has_audio"] and hello["text"] and hello["review_status"]
    assert hello["voice"] == "machine voice draft"
    assert len(hi["prompts"]) == 47
    ids = [p["id"] for p in hi["prompts"]]
    assert (
        ids.index("FC_NOTICE_FULL") == ids.index("FC_NOTICE") + 1
    )  # the full notice is listed beside the short
    for lang_code in ("hi", "doi", "ur"):
        rows = {p["id"]: p for p in lang(body, lang_code)["prompts"]}
        for pid in ("FC_NOTICE", "FC_NOTICE_FULL", "FC_PLACE", "ENR_DUP"):
            assert rows[pid]["has_audio"] and rows[pid]["voice"] == "machine voice draft", (lang_code, pid)
            assert rows[pid]["review_status"] == "machine_draft_needs_native_review", (lang_code, pid)
        assert len(rows["FC_NOTICE_FULL"]["text"]) > 2 * len(rows["FC_NOTICE"]["text"])


def test_prompts_list_the_missing_files_per_language(make_app, clock_t0, people_for):
    from fastapi.testclient import TestClient

    app = make_app(voice=False)
    client = TestClient(app)
    admin = people_for(app, "admin")
    hi = lang(client.get("/api/admin/prompts", headers=admin.read).json(), "hi")
    assert hi["ready"] is False
    assert set(segments_of(app.state.ctx, "hi", "FC_HELLO")) <= set(hi["missing"])
    assert any(path.startswith("hi/slot/clinic_cln_gmc") for path in hi["missing"])
    assert any(path.startswith("hi/slot/ward_") for path in hi["missing"])
    assert any(not p["has_audio"] and p["voice"] is None for p in hi["prompts"])


def a_missing_file(client, admin):
    hi = lang(client.get("/api/admin/prompts", headers=admin.read).json(), "hi")
    return next(path for path in hi["missing"] if path.startswith("hi/") and not path.endswith(")"))


def test_uploading_a_recording_fills_the_gap_and_marks_the_voice(make_app, clock_t0, people_for):
    from fastapi.testclient import TestClient

    app = make_app(voice=False)
    ctx, client = app.state.ctx, TestClient(app)
    admin = people_for(app, "admin")
    path = a_missing_file(client, admin)
    name = path.removeprefix("hi/").removesuffix(".wav")
    res = client.post(f"/api/admin/prompts/hi/{name}", content=wav_bytes(0.3), headers=admin.write)
    assert res.status_code == 200, res.text
    assert res.json() == {
        "ok": True,
        "path": path,
        "bytes": len(wav_bytes(0.3)),
        "voice": "recording uploaded",
    }
    stored = ctx.settings.data_dir / "prompts" / "audio" / path
    assert stored.read_bytes() == wav_bytes(0.3)
    assert path not in lang(client.get("/api/admin/prompts", headers=admin.read).json(), "hi")["missing"]
    row = ctx.db.one("SELECT actor FROM audit WHERE action = 'prompt.upload' AND object_id = ?", (path,))
    assert row["actor"] == "staff:admin"
    assert not list(stored.parent.glob("*.tmp"))


def segments_of(ctx, lang_code, prompt_id):
    from chaarteeke.ivr.prompts import required_extent

    items = ctx.prompts.manifest(lang_code, **required_extent(ctx))
    return [i.path for i in items if i.kind == "segment" and i.prompt_id == prompt_id]


def test_a_prompt_is_a_recording_only_when_every_part_was_uploaded(make_app, clock_t0, people_for):
    from fastapi.testclient import TestClient

    app = make_app(voice=False)
    ctx, client = app.state.ctx, TestClient(app)
    admin = people_for(app, "admin")
    audio = ctx.settings.data_dir / "prompts" / "audio"
    notice = segments_of(ctx, "hi", "FC_NOTICE")
    hello = segments_of(ctx, "hi", "FC_HELLO")
    assert notice and len(hello) >= 2
    for path in notice:  # every part uploaded
        name = path.removeprefix("hi/").removesuffix(".wav")
        assert (
            client.post(f"/api/admin/prompts/hi/{name}", content=wav_bytes(), headers=admin.write).status_code
            == 200
        )
    for path in hello:  # every part present, one of them put there by the voice job, not uploaded
        (audio / path).parent.mkdir(parents=True, exist_ok=True)
        (audio / path).write_bytes(wav_bytes())
    name = hello[0].removeprefix("hi/").removesuffix(".wav")
    client.post(f"/api/admin/prompts/hi/{name}", content=wav_bytes(), headers=admin.write)
    rows = {
        p["id"]: p for p in lang(client.get("/api/admin/prompts", headers=admin.read).json(), "hi")["prompts"]
    }
    assert rows["FC_NOTICE"]["voice"] == "recording uploaded"
    assert rows["FC_HELLO"]["voice"] == "machine voice draft"
    assert rows["FC_QCONSENT"]["voice"] is None and rows["FC_QCONSENT"]["has_audio"] is False


def test_slot_clips_can_be_uploaded_too(make_app, clock_t0, people_for):
    from fastapi.testclient import TestClient

    app = make_app(voice=False)
    client = TestClient(app)
    admin = people_for(app, "admin")
    res = client.post("/api/admin/prompts/doi/slot/ward_1", content=wav_bytes(), headers=admin.write)
    assert res.status_code == 200 and res.json()["path"] == "doi/slot/ward_1.wav"


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"not a wav at all", "bad_wav"),
        (b"RIFF\x00\x00\x00\x00WAVEjunk", "bad_wav"),
        (wav_bytes(channels=2), "bad_wav"),
        (wav_bytes(width=1), "bad_wav"),
        (wav_bytes(seconds=0), "bad_wav"),
        (b"", "bad_wav"),
    ],
)
def test_a_file_that_is_not_a_plain_mono_16_bit_wav_is_refused(
    make_app, clock_t0, people_for, content, error
):
    from fastapi.testclient import TestClient

    app = make_app(voice=False)
    client = TestClient(app)
    admin = people_for(app, "admin")
    res = client.post("/api/admin/prompts/hi/FC_HELLO.0", content=content, headers=admin.write)
    assert res.status_code == 400 and res.json()["error"] == error
    assert not (app.state.ctx.settings.data_dir / "prompts" / "audio" / "hi" / "FC_HELLO.0.wav").exists()


@pytest.mark.parametrize(
    "target",
    [
        "hi/../../../etc/passwd",
        "hi/NOT_A_PROMPT",
        "hi/FC_HELLO.99",
        "xx/FC_HELLO.0",
        "hi/slot/ward_9999",
        "en/FC_HELLO.0",
    ],
)
def test_only_files_the_voice_must_provide_can_be_replaced(make_app, clock_t0, people_for, target):
    from fastapi.testclient import TestClient

    app = make_app(voice=False)
    client = TestClient(app)
    admin = people_for(app, "admin")
    res = client.post(f"/api/admin/prompts/{target}", content=wav_bytes(), headers=admin.write)
    assert res.status_code in (404, 400), (target, res.status_code, res.text)
    root = app.state.ctx.settings.data_dir / "prompts" / "audio"
    assert not list(root.rglob("*.wav"))


def test_a_recording_over_the_size_limit_is_refused(make_app, clock_t0, people_for, monkeypatch):
    from fastapi.testclient import TestClient

    from chaarteeke.policy import get_policy

    monkeypatch.setitem(get_policy().raw["limits"], "upload_body_max_bytes", 1000)
    app = make_app(voice=False)
    client = TestClient(app)
    admin = people_for(app, "admin")
    big = client.post("/api/admin/prompts/hi/FC_HELLO.0", content=wav_bytes(1.0), headers=admin.write)
    assert big.status_code == 413 and big.json()["error"] == "too_large"
    assert big.json()["code"] == "wav.too_large"  # the page translates it as it does the route's own refusal
    small = client.post("/api/admin/prompts/hi/FC_HELLO.0", content=wav_bytes(0.01), headers=admin.write)
    assert small.status_code == 200


def test_an_upload_needs_the_csrf_token(make_app, clock_t0, people_for):
    from fastapi.testclient import TestClient

    app = make_app(voice=False)
    client = TestClient(app)
    admin = people_for(app, "admin")
    res = client.post("/api/admin/prompts/hi/FC_HELLO.0", content=wav_bytes(), headers=admin.read)
    assert res.status_code == 403 and res.json()["error"] == "csrf"


# ------------------------------------------------------------------- retention
def test_retention_reports_rules_and_what_waits(client, people):
    body = client.get("/api/admin/retention", headers=people["admin"].read).json()
    assert body["rules"]["phone_purge_days_after_last_dose"] == 7
    assert set(body["pending"]) >= {
        "phones",
        "phones_due",
        "clips",
        "clips_due",
        "missed_calls",
        "missed_calls_due",
    }
    assert set(body["deleted"]) == {"phone", "clip", "missed_call"}


def test_a_language_with_audio_missing_cannot_be_switched_on_for_a_clinic(make_app, clock_t0, people_for):
    from fastapi.testclient import TestClient

    app = make_app(voice=False)
    clock.install(clock_t0)
    client, admin = TestClient(app), people_for(app, "admin")
    body = {"name": "Satwari urban clinic", "short_name": "Satwari", "default_regimen": "im_essen", "default_lang": "doi"}  # fmt: skip
    res = client.post("/api/admin/clinics", json=body, headers=admin.write)
    assert res.status_code == 409 and res.json()["error"] == "language_not_ready"
    assert "Dogri" in res.json()["message"] and "audio files are missing" in res.json()["message"]
    current = app.state.ctx.db.one("SELECT default_lang FROM clinics WHERE id = 'cln_gmc'")["default_lang"]
    other = next(code for code in ("hi", "doi", "ur") if code != current)
    res = client.put("/api/admin/clinics", json={"id": "cln_gmc", "default_lang": other}, headers=admin.write)
    assert res.status_code == 409 and res.json()["error"] == "language_not_ready"
    assert app.state.ctx.db.one("SELECT default_lang FROM clinics WHERE id = 'cln_gmc'")["default_lang"] == current  # fmt: skip
    # Leaving the language as it is, or changing anything else, is not blocked.
    res = client.put("/api/admin/clinics", json={"id": "cln_gmc", "default_lang": current, "short_name": "GMC"}, headers=admin.write)  # fmt: skip
    assert res.status_code == 200


def test_a_language_with_its_audio_can_be_switched_on(client, ctx, people):
    current = ctx.db.one("SELECT default_lang FROM clinics WHERE id = 'cln_gmc'")["default_lang"]
    other = next(code for code in ("hi", "doi", "ur") if code != current)
    res = client.put("/api/admin/clinics", json={"id": "cln_gmc", "default_lang": other}, headers=people["admin"].write)  # fmt: skip
    assert res.status_code == 200 and res.json()["default_lang"] == other


def test_the_clinic_form_gets_every_schedule_with_its_switch(client, people):
    rows = client.get("/api/admin/regimens", headers=people["admin"].read).json()
    by_id = {r["id"]: r for r in rows}
    assert {"id_trc", "im_essen", "reexposure", "id_1week", "im_abbrev"} <= set(by_id)
    assert by_id["id_trc"] == {"id": "id_trc", "name": by_id["id_trc"]["name"], "route": by_id["id_trc"]["route"], "days": [0, 3, 7, 28], "enabled": True}  # fmt: skip
    assert by_id["id_1week"]["enabled"] is False


def test_admin_refusals_carry_a_stable_code_and_params_beside_the_english_message(client, people):
    admin = people["admin"]
    res = client.post("/api/admin/people", json={"username": "ab", "display_name": "A", "role": "admin", "password": "x" * 12}, headers=admin.write)  # fmt: skip
    body = res.json()
    assert (
        res.status_code == 400 and body["error"] == "bad_request" and body["message"].startswith("A username")
    )
    assert body["code"] == "person.bad_username" and body["params"] == {}
    res = client.post("/api/admin/people", json={"username": "someone.new", "display_name": "A", "role": "admin", "password": "short"}, headers=admin.write)  # fmt: skip
    body = res.json()
    assert (
        body["error"] == "weak_password" and body["code"] == "password.weak" and body["params"] == {"min": 10}
    )
    res = client.put("/api/admin/clinics", json={"id": "cln_gmc", "ward_no": 9999}, headers=admin.write)
    assert res.json() == {"error": "unknown_ward", "message": "That is not a ward number.", "code": "ward.unknown", "params": {}}  # fmt: skip
