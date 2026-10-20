"""GET /api/clinic/config: the enrol form's defaults, the languages with complete voice, the number to ring."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from chaarteeke import clock
from chaarteeke.ivr.prompts import PromptCatalogue

SHAPE = {
    "clinic_id", "clinic_name", "default_regimen", "default_lang", "regimens", "langs", "ct_number",
    "sim_phone", "clinics",
}  # fmt: skip


def test_the_config_has_exactly_the_agreed_shape(client, people):
    res = client.get("/api/clinic/config", headers=people["clinic"].read)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == SHAPE
    assert body["clinic_id"] == "cln_gmc" and body["clinic_name"]
    assert body["default_regimen"] and body["default_lang"] in ("hi", "doi", "ur")
    assert body["ct_number"] is None  # demo mode has no missed-call number
    assert body["sim_phone"] is True
    assert body["clinics"] == []  # clinic staff cannot switch clinic
    assert body["langs"] == ["hi", "doi", "ur"]  # the test voice is complete


def test_only_enabled_regimens_are_offered_with_their_days(client, people):
    regimens = client.get("/api/clinic/config", headers=people["clinic"].read).json()["regimens"]
    by_id = {r["id"]: r for r in regimens}
    assert set(by_id) == {"id_trc", "im_essen", "reexposure"}
    assert all(r["enabled"] is True for r in regimens)
    assert by_id["id_trc"]["days"] == [0, 3, 7, 28] and by_id["im_essen"]["days"] == [0, 3, 7, 14, 28]
    assert set(by_id["id_trc"]) == {"id", "name", "route", "days", "enabled"}


def test_the_clinic_is_the_signed_in_one_and_another_is_refused(client, people):
    own = client.get("/api/clinic/config?clinic_id=cln_gmc", headers=people["clinic"].read)
    assert own.status_code == 200
    other = client.get("/api/clinic/config?clinic_id=cln_demo2", headers=people["clinic"].read)
    assert other.status_code == 403 and other.json()["error"] == "forbidden"


def test_a_visitor_viewing_as_clinic_may_switch_between_demo_clinics(client, ctx, people):
    who = people["visitor-clinic"]
    body = client.get("/api/clinic/config", headers=who.read).json()
    demo = {r["id"] for r in ctx.db.query("SELECT id FROM clinics WHERE is_demo = 1")}
    assert {c["id"] for c in body["clinics"]} == demo and len(demo) >= 3
    assert all(set(c) == {"id", "name"} for c in body["clinics"])
    assert [c["name"] for c in body["clinics"]] == sorted(c["name"] for c in body["clinics"])
    other = client.get("/api/clinic/config?clinic_id=cln_demo2", headers=who.read)
    assert other.status_code == 200 and other.json()["clinic_id"] == "cln_demo2"


def test_a_visitor_cannot_ask_for_a_clinic_that_is_not_a_demo_one(client, ctx, people):
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE clinics SET is_demo = 0 WHERE id = 'cln_demo2'")
    res = client.get("/api/clinic/config?clinic_id=cln_demo2", headers=people["visitor-clinic"].read)
    assert res.status_code == 403


def test_the_administrator_must_say_which_clinic_and_gets_the_switch_list(client, people):
    admin = people["admin"]
    assert client.get("/api/clinic/config", headers=admin.read).status_code == 400
    res = client.get("/api/clinic/config?clinic_id=cln_gmc", headers=admin.read)
    assert res.status_code == 200 and len(res.json()["clinics"]) >= 3
    gone = client.get("/api/clinic/config?clinic_id=cln_nobody", headers=admin.read)
    assert gone.status_code == 404 and gone.json()["error"] == "unknown_clinic"


def test_other_roles_and_signed_out_people_are_refused(client, people):
    assert client.get("/api/clinic/config").status_code == 401
    for role in ("abc", "vet", "oversight", "reviewer"):
        assert client.get("/api/clinic/config", headers=people[role].read).status_code == 403, role


def test_a_language_with_a_file_missing_is_not_offered(app, client, ctx, people, voice_dir, tmp_path):
    audio = tmp_path / "audio"
    shutil.copytree(voice_dir, audio)
    shutil.rmtree(audio / "ur")
    prompts = ctx.settings.data_dir / "prompts" / "prompts.json"
    ctx.prompts = PromptCatalogue(prompts, audio)
    assert client.get("/api/clinic/config", headers=people["clinic"].read).json()["langs"] == ["hi", "doi"]


def test_a_clinic_without_its_own_clip_is_offered_no_language(client, ctx, people, voice_dir, tmp_path):
    audio = tmp_path / "audio"
    shutil.copytree(voice_dir, audio)
    for lang in ("hi", "doi", "ur"):
        (audio / lang / "slot" / "clinic_cln_gmc.wav").unlink()
    ctx.prompts = PromptCatalogue(ctx.settings.data_dir / "prompts" / "prompts.json", audio)
    body = client.get("/api/clinic/config", headers=people["clinic"].read).json()
    assert body["langs"] == []  # nothing stands in for the clinic's spoken name


def test_no_voice_at_all_offers_no_language(make_app, clock_t0, people_for):
    app = make_app(voice=False)
    clock.install(clock_t0)
    body = TestClient(app).get("/api/clinic/config", headers=people_for(app, "clinic").read).json()
    assert body["langs"] == []


def test_the_missed_call_number_is_shown_in_pilot_mode_only_when_it_is_set(make_app, clock_t0, people_for):
    shown = make_app(mode="pilot", ct_number="+91 99999 01234")
    clock.install(clock_t0)
    body = TestClient(shown).get("/api/clinic/config", headers=people_for(shown, "clinic").read).json()
    assert body["ct_number"] == "+91 99999 01234"
    unset = make_app(mode="pilot")
    clock.install(clock_t0)
    body = TestClient(unset).get("/api/clinic/config", headers=people_for(unset, "clinic").read).json()
    assert body["ct_number"] is None


def test_demo_mode_never_shows_a_number_even_if_one_is_set(make_app, clock_t0, people_for):
    app = make_app(ct_number="+91 99999 01234")
    clock.install(clock_t0)
    body = TestClient(app).get("/api/clinic/config", headers=people_for(app, "clinic").read).json()
    assert body["ct_number"] is None


def test_the_browser_phone_flag_follows_the_telephony_setting(make_app, clock_t0, people_for):
    app = make_app(telephony="ari")
    clock.install(clock_t0)
    body = TestClient(app).get("/api/clinic/config", headers=people_for(app, "clinic").read).json()
    assert body["sim_phone"] is False


def test_the_sticker_page_is_a_registered_route_and_reads_the_same_config():
    """The bite-card sticker (/clinic/sticker) shows the clinic's name and number from this config, for the clinic
    and admin views and a visitor viewing as clinic; its page shell must be in the route table."""
    from chaarteeke.api.app import PAGES

    assert PAGES["/clinic/sticker"] == "sticker.html"
    assert (Path(__file__).resolve().parents[2] / "web" / "sticker.html").is_file()
