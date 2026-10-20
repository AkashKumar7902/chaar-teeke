"""The clinic desk over HTTP: scoping to the signed-in clinic, enrolling, linking, doses, stopping, the
audited phone reveal, the vaccine switch and the vets' call-back requests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from api_support import Who

from chaarteeke import clock, enrol
from chaarteeke.api import auth

NUMBER = "9876543210"
E164 = "+919876543210"


def enrol_body(ctx, clinic="cln_gmc", day_offset=0, regimen="id_trc"):
    day0 = clock.local_today(ctx.settings.local_tz) + timedelta(days=day_offset)
    return {
        "clinic_id": clinic, "register_no": "R-77", "regimen_id": regimen, "lang": "hi",
        "day0": day0.isoformat(), "eligibility": "adult",
    }  # fmt: skip


@pytest.fixture
def other_clinic(ctx) -> Who:
    """A login for the second demo clinic."""
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO staff(id, username, display_name, role, clinic_id, password_hash, active, is_demo)"
            " VALUES ('stf_other','other','Other desk','clinic','cln_demo2',?,1,1)",
            (auth.hash_password("a long password"),),
        )
    token, csrf = auth.sign_in(ctx, "other", "a long password")
    return Who(token, csrf)


@pytest.fixture
def patient(ctx, client, people):
    """A linked course in cln_gmc whose second dose is due today, with the number 9876543210."""
    made = client.post("/api/courses", json=enrol_body(ctx, day_offset=-3), headers=people["clinic"].write)
    assert made.status_code == 200, made.text
    course_id = made.json()["id"]
    got = enrol.record_missed_call(ctx, NUMBER, "ari", clock.now())
    assert got.status == "unlinked"
    linked = client.post(
        f"/api/missed-calls/{got.missed_call_id}/link",
        json={"course_id": course_id},
        headers=people["clinic"].write,
    )
    assert linked.status_code == 200, linked.text
    return course_id


def test_today_is_the_signed_in_clinic_and_never_from_the_request(client, people):
    res = client.get("/api/clinic/today", headers=people["clinic"].read)
    assert res.status_code == 200
    assert res.json()["clinic_id"] == "cln_gmc"
    same = client.get("/api/clinic/today?clinic_id=cln_gmc", headers=people["clinic"].read)
    assert same.status_code == 200
    other = client.get("/api/clinic/today?clinic_id=cln_demo2", headers=people["clinic"].read)
    assert other.status_code == 403 and other.json()["error"] == "forbidden"


def test_clinic_staff_cannot_read_or_change_another_clinic(client, ctx, people, other_clinic, patient):
    theirs = other_clinic
    read, write = theirs.read, theirs.write
    assert client.get("/api/clinic/today", headers=read).json()["clinic_id"] == "cln_demo2"
    assert client.get("/api/clinic/today?clinic_id=cln_gmc", headers=read).status_code == 403
    assert client.get(f"/api/courses/{patient}", headers=read).status_code == 403
    assert client.post(f"/api/courses/{patient}/reveal-phone", headers=write).status_code == 403
    assert client.post(f"/api/courses/{patient}/doses/2/given", json={}, headers=write).status_code == 403
    assert client.post(f"/api/courses/{patient}/stop", json={"reason": "x"}, headers=write).status_code == 403
    assert client.post("/api/clinics/cln_gmc/vaccine", json={"out": True}, headers=write).status_code == 403
    assert client.post("/api/courses", json=enrol_body(ctx), headers=write).status_code == 403
    assert client.get("/api/clinic/callbacks?clinic_id=cln_gmc", headers=read).status_code == 403
    still = ctx.db.one("SELECT status FROM courses WHERE id = ?", (patient,))["status"]
    assert still == "active"
    assert ctx.db.one("SELECT vaccine_out FROM clinics WHERE id = 'cln_gmc'")["vaccine_out"] == 0
    assert ctx.db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'phone.reveal'")["n"] == 0


def test_a_clinic_cannot_link_a_missed_call_to_another_clinics_course(
    client, ctx, people, other_clinic, patient
):
    mine = client.post("/api/courses", json=enrol_body(ctx, "cln_demo2"), headers=other_clinic.write).json()
    got = enrol.record_missed_call(ctx, "9123456780", "ari", clock.now())
    res = client.post(
        f"/api/missed-calls/{got.missed_call_id}/link",
        json={"course_id": patient},
        headers=other_clinic.write,
    )
    assert res.status_code == 403
    ok = client.post(
        f"/api/missed-calls/{got.missed_call_id}/link",
        json={"course_id": mine["id"]},
        headers=other_clinic.write,
    )
    assert ok.status_code == 200 and ok.json()["status"] == "active"


def test_enrolling_makes_a_course_waiting_for_its_missed_call(client, ctx, people):
    res = client.post("/api/courses", json=enrol_body(ctx), headers=people["clinic"].write)
    assert res.status_code == 200
    course = res.json()
    assert course["status"] == "awaiting_call" and course["clinic_id"] == "cln_gmc"
    assert [d["status"] for d in course["doses"]][0] == "given"
    today = client.get("/api/clinic/today", headers=people["clinic"].read).json()
    assert course["id"] in [r["course_id"] for r in today["awaiting_call"]]


def test_enrolling_with_a_bad_date_regimen_or_body_is_a_plain_refusal(client, ctx, people):
    who = people["clinic"].write
    future = enrol_body(ctx, day_offset=3)
    assert client.post("/api/courses", json=future, headers=who).json()["error"] == "bad_date"
    off = client.post("/api/courses", json=enrol_body(ctx, regimen="id_1week"), headers=who)
    assert off.status_code == 409 and off.json()["error"] == "regimen_disabled"
    unknown = client.post("/api/courses", json=enrol_body(ctx, regimen="nope"), headers=who)
    assert unknown.status_code == 400 and unknown.json()["error"] == "unknown_regimen"
    bad = client.post("/api/courses", json={"clinic_id": "cln_gmc"}, headers=who)
    assert bad.status_code == 400 and bad.json()["error"] == "bad_request"
    assert "register_no" in bad.json()["message"]


def test_linking_a_missed_call_activates_the_course_and_shows_only_the_last_four(
    client, ctx, people, patient
):
    course = client.get(f"/api/courses/{patient}", headers=people["clinic"].read).json()
    assert course["status"] == "active" and course["phone_last4"] == "3210"
    assert NUMBER not in str(course) and E164 not in str(course)
    today = client.get("/api/clinic/today", headers=people["clinic"].read).json()
    assert NUMBER not in str(today)
    first = [c for c in course["calls"] if c["kind"] == "first"]
    assert len(first) == 1 and first[0]["status"] == "scheduled"


def test_the_unlinked_missed_calls_are_listed_with_the_last_four(client, ctx, people):
    got = enrol.record_missed_call(ctx, NUMBER, "ari", clock.now())
    today = client.get("/api/clinic/today", headers=people["clinic"].read).json()
    mine = [m for m in today["unlinked"] if m["id"] == got.missed_call_id]
    assert mine and mine[0]["phone_last4"] == "3210"
    assert "phone_cipher" not in str(today) and NUMBER not in str(today)


def test_marking_a_dose_given(client, ctx, people, patient):
    who = people["clinic"].write
    res = client.post(f"/api/courses/{patient}/doses/2/given", headers=who)
    assert res.status_code == 200
    assert res.json()["status"] == "given" and res.json()["marked_by"] == "clinic"
    again = client.post(f"/api/courses/{patient}/doses/2/given", json={}, headers=who)
    assert again.status_code == 200 and again.json()["status"] == "given"
    row = ctx.db.one("SELECT actor FROM audit WHERE action = 'dose.given' AND object_id = ?", (patient,))
    assert row["actor"] == "staff:clinic"


def test_marking_a_dose_not_yet_due_or_on_a_bad_day_is_refused(client, ctx, people, patient):
    who = people["clinic"].write
    soon = client.post(f"/api/courses/{patient}/doses/3/given", json={}, headers=who)
    assert soon.status_code == 409 and soon.json()["error"] == "dose_in_future"
    tomorrow = (clock.local_today(ctx.settings.local_tz) + timedelta(days=1)).isoformat()
    future = client.post(f"/api/courses/{patient}/doses/2/given", json={"on": tomorrow}, headers=who)
    assert future.status_code == 400 and future.json()["error"] == "date_in_future"
    junk = client.post(f"/api/courses/{patient}/doses/2/given", json={"on": "19/10/2026"}, headers=who)
    assert junk.status_code == 400 and junk.json()["error"] == "bad_date"
    assert client.post(f"/api/courses/{patient}/doses/9/given", json={}, headers=who).status_code == 404


def test_revealing_the_phone_gives_the_number_and_is_audited(client, ctx, people, patient):
    res = client.post(f"/api/courses/{patient}/reveal-phone", headers=people["clinic"].write)
    assert res.status_code == 200 and res.json() == {"phone": E164}
    row = ctx.db.one(
        "SELECT actor, detail_json FROM audit WHERE action = 'phone.reveal' AND object_id = ?", (patient,)
    )
    assert row["actor"] == "staff:clinic" and E164 not in (row["detail_json"] or "")
    assert res.headers["cache-control"] == "no-store"


def test_the_phone_cannot_be_revealed_after_it_has_gone(client, ctx, people, patient):
    with ctx.db.transaction() as conn:
        conn.execute("DELETE FROM contacts WHERE course_id = ?", (patient,))
    res = client.post(f"/api/courses/{patient}/reveal-phone", headers=people["clinic"].write)
    assert res.status_code == 410 and res.json()["error"] == "phone_purged"
    assert ctx.db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'phone.reveal_refused'")["n"] == 1


def test_stopping_a_course_ends_it_and_a_second_stop_is_refused(client, ctx, people, patient):
    who = people["clinic"].write
    res = client.post(f"/api/courses/{patient}/stop", json={"reason": "Patient moved away"}, headers=who)
    assert res.status_code == 200 and res.json()["status"] == "stopped"
    again = client.post(f"/api/courses/{patient}/stop", json={"reason": "again"}, headers=who)
    assert again.status_code == 409 and again.json()["error"] == "course_closed"
    empty = client.post(f"/api/courses/{patient}/stop", json={"reason": ""}, headers=who)
    assert empty.status_code == 400


def test_the_vaccine_switch(client, ctx, people):
    who = people["clinic"]
    res = client.post("/api/clinics/cln_gmc/vaccine", json={"out": True}, headers=who.write)
    assert res.status_code == 200 and res.json()["vaccine_out"] is True and res.json()["vaccine_out_since"]
    today = client.get("/api/clinic/today", headers=who.read).json()
    assert today["vaccine_out"] is True
    back = client.post("/api/clinics/cln_gmc/vaccine", json={"out": False}, headers=who.write)
    assert back.json() == {"vaccine_out": False, "vaccine_out_since": None}


def test_a_visitor_viewing_as_clinic_sees_the_confirmed_demo_clinic_and_the_visitor_desk(client, ctx, people):
    who = people["visitor-clinic"].read
    assert client.get("/api/clinic/today", headers=who).json()["clinic_id"] == "cln_gmc"
    assert client.get("/api/clinic/today?clinic_id=cln_demo_visitor", headers=who).status_code == 200
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO clinics(id, name, short_name, default_regimen, default_lang, arv_status, is_demo)"
            " VALUES ('cln_real','Real clinic','Real','id_trc','hi','confirmed',0)"
        )
    res = client.get("/api/clinic/today?clinic_id=cln_real", headers=who)
    assert res.status_code == 403 and res.json()["error"] == "forbidden"


def test_the_administrator_reads_no_patient_but_reads_a_clinics_settings(client, ctx, people, patient):
    """docs/PRIVACY.md: the administrator sets the system up and does not read a clinic's patients."""
    for who in (people["admin"], people["visitor-admin"]):
        for path in (
            "/api/clinic/today?clinic_id=cln_gmc",
            "/api/clinic/today",
            f"/api/courses/{patient}",
            "/api/clinic/callbacks?clinic_id=cln_gmc",
        ):
            res = client.get(path, headers=who.read)
            assert res.status_code == 403 and res.json()["error"] == "forbidden", path
        for write in (
            f"/api/courses/{patient}/reveal-phone",
            f"/api/courses/{patient}/doses/2/given",
            f"/api/courses/{patient}/doses/2/unmark",
        ):
            assert client.post(write, headers=who.write).status_code == 403, write
    who = people["admin"].read
    assert client.get("/api/clinic/config", headers=who).json()["error"] == "choose_clinic"
    config = client.get("/api/clinic/config?clinic_id=cln_gmc", headers=who)
    assert config.status_code == 200 and config.json()["clinic_id"] == "cln_gmc"
    assert client.get("/api/clinic/config?clinic_id=cln_nowhere", headers=who).status_code == 404
    assert "R-77" not in config.text  # no register number, no patient
    assert ctx.db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'phone.reveal'")["n"] == 0


def test_a_clinic_login_with_no_clinic_gets_a_plain_refusal(client, ctx):
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO staff(id, username, display_name, role, clinic_id, password_hash, active, is_demo)"
            " VALUES ('stf_lost','lost','Lost','clinic',NULL,?,1,1)",
            (auth.hash_password("a long password"),),
        )
    token, _ = auth.sign_in(ctx, "lost", "a long password")
    res = client.get("/api/clinic/today", headers={"cookie": f"ct_session={token}"})
    assert res.status_code == 409 and res.json()["error"] == "no_clinic"


def claimable_alert(ctx):
    """A new alert on a report whose course belongs to cln_gmc."""
    row = ctx.db.one(
        "SELECT a.id, c.id AS course_id, c.register_no FROM alerts a JOIN bite_reports r ON r.id = a.report_id"
        " JOIN courses c ON c.id = r.course_id WHERE a.status = 'new' AND c.clinic_id = 'cln_gmc' AND r.is_demo = 1"
        " AND NOT EXISTS (SELECT 1 FROM callback_requests cb WHERE cb.alert_id = a.id) LIMIT 1"
    )
    assert row is not None, "the demo seed has no new alert on a cln_gmc course"
    return row


def test_a_vets_callback_request_reaches_the_patients_clinic_only(client, ctx, people, other_clinic):
    alert = claimable_alert(ctx)
    vet = people["vet"]
    assert client.post(f"/api/alerts/{alert['id']}/claim", json={}, headers=vet.write).status_code == 200
    asked = client.post(f"/api/alerts/{alert['id']}/callback", headers=vet.write)
    assert asked.status_code == 200
    assert asked.json()["status"] == "open" and asked.json()["course_id"] is None
    assert asked.json()["register_no"] is None  # the vet never sees which patient
    clinic = people["clinic"]
    listed = client.get("/api/clinic/callbacks", headers=clinic.read).json()
    mine = [c for c in listed if c["id"] == asked.json()["id"]]
    assert (
        mine and mine[0]["course_id"] == alert["course_id"] and mine[0]["register_no"] == alert["register_no"]
    )
    assert client.get("/api/clinic/callbacks", headers=other_clinic.read).json() == []
    today = client.get("/api/clinic/today", headers=clinic.read).json()
    assert any(c["id"] == asked.json()["id"] for c in today["callbacks"])

    refused = client.post(f"/api/callbacks/{asked.json()['id']}/done", json={}, headers=other_clinic.write)
    assert refused.status_code == 403
    done = client.post(
        f"/api/callbacks/{asked.json()['id']}/done",
        json={"note": "Phoned, patient is well"},
        headers=clinic.write,
    )
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert client.get("/api/clinic/callbacks", headers=clinic.read).json() == [
        c for c in listed if c["id"] != asked.json()["id"]
    ]
    twice = client.post(f"/api/callbacks/{asked.json()['id']}/done", json={}, headers=clinic.write)
    assert twice.status_code == 409 and twice.json()["error"] == "already_closed"


def test_a_callback_can_be_declined(client, ctx, people):
    alert = claimable_alert(ctx)
    vet, clinic = people["vet"], people["clinic"]
    client.post(f"/api/alerts/{alert['id']}/claim", json={}, headers=vet.write)
    cb = client.post(f"/api/alerts/{alert['id']}/callback", headers=vet.write).json()
    res = client.post(f"/api/callbacks/{cb['id']}/done", json={"outcome": "declined"}, headers=clinic.write)
    assert res.status_code == 200 and res.json()["status"] == "declined"
    assert (
        client.post(
            f"/api/callbacks/{cb['id']}/done", json={"outcome": "maybe"}, headers=clinic.write
        ).status_code
        == 400
    )


# ------------------------------------------------------- the phone reveal has a limit per login
def reveal(client, who, course_id):
    return client.post(f"/api/courses/{course_id}/reveal-phone", headers=who.write)


def test_a_login_may_read_only_so_many_numbers_an_hour(client, ctx, people, patient, monkeypatch):
    monkeypatch.setitem(ctx.policy.raw["clinic"], "phone_reveals_per_hour", 3)
    who = people["clinic"]
    assert [reveal(client, who, patient).status_code for _ in range(3)] == [200, 200, 200]
    over = reveal(client, who, patient)
    assert over.status_code == 429 and over.json()["error"] == "rate_limited"
    assert "3 phone numbers in the last hour" in over.json()["message"] and E164 not in over.text
    assert 0 < int(over.headers["retry-after"]) <= 3601
    reveal(client, who, patient)  # asking again at once is refused again ...
    rows = ctx.db.query(
        "SELECT actor, object_id, detail_json FROM audit WHERE action = 'phone.reveal_limited'"
    )
    assert len(rows) == 1 and rows[0]["actor"] == "staff:clinic"  # ... and the log notes the burst once
    assert (
        ctx.db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'phone.reveal'")["n"] == 3
    )  # no number left


def test_the_limit_is_per_login_and_ends_an_hour_after_the_oldest_reveal(
    client, ctx, people, other_clinic, patient, monkeypatch, clock_t0
):
    monkeypatch.setitem(ctx.policy.raw["clinic"], "phone_reveals_per_hour", 2)
    who = people["clinic"]
    assert reveal(client, who, patient).status_code == 200
    clock_t0.advance(timedelta(minutes=30))
    assert reveal(client, who, patient).status_code == 200
    assert reveal(client, who, patient).status_code == 429
    clock_t0.advance(timedelta(minutes=31))  # the first reveal is now more than an hour old
    assert reveal(client, who, patient).status_code == 200
    assert reveal(client, who, patient).status_code == 429
    with ctx.db.transaction() as conn:  # a colleague at the same desk has their own count
        conn.execute(
            "INSERT INTO staff(id, username, display_name, role, clinic_id, password_hash, active, is_demo)"
            " VALUES ('stf_two','colleague','Colleague','clinic','cln_gmc',?,1,1)",
            (auth.hash_password("a long password"),),
        )
    token, csrf = auth.sign_in(ctx, "colleague", "a long password")
    assert reveal(client, Who(token, csrf), patient).status_code == 200


def test_a_refused_reveal_does_not_use_up_the_limit(client, ctx, people, patient, monkeypatch):
    monkeypatch.setitem(ctx.policy.raw["clinic"], "phone_reveals_per_hour", 1)
    who = people["clinic"]
    with ctx.db.transaction() as conn:
        conn.execute("DELETE FROM contacts WHERE course_id = ?", (patient,))
    assert reveal(client, who, patient).status_code == 410  # the number has gone
    assert ctx.db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'phone.reveal_refused'")["n"] == 1
    assert reveal(client, who, patient).status_code == 410  # not 429: a refusal is not a reveal


def test_the_reveal_limit_comes_from_policy(ctx):
    assert ctx.policy["clinic"] == {
        "phone_reveals_per_hour": 30,
        "undo_dose_minutes": 15,
        "undo_callback_minutes": 15,
        "edit_window_minutes": 30,
    }


# ------------------------------------------------------------------------ taking things back
def audit_actions(ctx, action):
    return ctx.db.query("SELECT actor, detail_json FROM audit WHERE action = ? ORDER BY seq", (action,))


def mark(client, who, course_id, dose_no=2):
    return client.post(f"/api/courses/{course_id}/doses/{dose_no}/given", json={}, headers=who.write)


def unmark(client, who, course_id, dose_no=2):
    return client.post(f"/api/courses/{course_id}/doses/{dose_no}/unmark", headers=who.write)


def dose_status(ctx, course_id, dose_no=2):
    return ctx.db.one("SELECT * FROM doses WHERE course_id = ? AND dose_no = ?", (course_id, dose_no))


def test_a_dose_marked_by_mistake_can_be_unmarked_and_it_is_audited(client, ctx, people, patient):
    clinic = people["clinic"]
    assert mark(client, clinic, patient).json()["status"] == "given"
    res = unmark(client, clinic, patient)
    assert res.status_code == 200, res.text
    assert (
        res.json()["status"] == "due" and res.json()["given_on"] is None and res.json()["marked_by"] is None
    )
    row = dose_status(ctx, patient)
    assert (row["status"], row["given_on"], row["marked_by"]) == ("due", None, None)
    assert [(r["actor"], r["detail_json"]) for r in audit_actions(ctx, "dose.unmarked")] == [
        ("staff:clinic", f'{{"dose_no": 2, "was_on": "{clock.local_today(ctx.settings.local_tz)}"}}')
    ]
    assert len(audit_actions(ctx, "dose.given")) == 1  # the first write's row stays
    again = unmark(client, clinic, patient)
    assert again.status_code == 409 and again.json()["error"] == "nothing_to_undo"


def test_unmarking_gives_back_the_reminder_that_marking_cancelled(client, ctx, people, patient):
    from chaarteeke.enrol import new_call

    with ctx.db.transaction() as conn:
        call_id = new_call(
            conn, course_id=patient, kind="reminder", job_key=f"reminder:{patient}:2:x",
            scheduled_for=clock.now() + timedelta(hours=2), dose_no=2,
        )  # fmt: skip
    mark(client, people["clinic"], patient)
    cancelled = ctx.db.one("SELECT status, error FROM calls WHERE id = ?", (call_id,))
    assert cancelled["status"] == "cancelled"
    unmark(client, people["clinic"], patient)
    back = ctx.db.one("SELECT status, error FROM calls WHERE id = ?", (call_id,))
    assert back["status"] == "scheduled" and back["error"] is None


def test_unmarking_the_last_dose_opens_a_finished_course_again(client, ctx, people):
    made = client.post(
        "/api/courses",
        json=enrol_body(ctx, day_offset=-3, regimen="reexposure"),
        headers=people["clinic"].write,
    ).json()
    course = made["id"]
    ctx.db.connect().execute("UPDATE courses SET status = 'active' WHERE id = ?", (course,))
    assert mark(client, people["clinic"], course).status_code == 200
    done = ctx.db.one("SELECT status, completed_at FROM courses WHERE id = ?", (course,))
    assert done["status"] == "completed" and done["completed_at"]
    assert unmark(client, people["clinic"], course).status_code == 200
    reopened = ctx.db.one("SELECT status, completed_at FROM courses WHERE id = ?", (course,))
    assert reopened["status"] == "active" and reopened["completed_at"] is None


def test_an_unmarked_late_dose_is_late_again(client, ctx, people):
    made = client.post(
        "/api/courses", json=enrol_body(ctx, day_offset=-10), headers=people["clinic"].write
    ).json()
    course = made["id"]
    ctx.db.connect().execute("UPDATE courses SET status = 'active' WHERE id = ?", (course,))
    assert mark(client, people["clinic"], course).status_code == 200
    res = unmark(client, people["clinic"], course)
    assert res.json()["status"] == "late_review"  # worked out from the calendar, as after any change


def test_a_dose_can_be_unmarked_only_inside_the_policy_window(client, ctx, people, patient, clock_t0):
    mark(client, people["clinic"], patient)
    minutes = ctx.policy["clinic"]["undo_dose_minutes"]
    clock_t0.advance(timedelta(minutes=minutes, seconds=1))
    late = unmark(client, people["clinic"], patient)
    assert late.status_code == 409 and late.json()["error"] == "undo_window_over"
    assert f"{minutes} minutes ago" in late.json()["message"]
    assert dose_status(ctx, patient)["status"] == "given"


def test_only_a_dose_the_clinic_marked_can_be_unmarked_and_not_the_first(client, ctx, people, patient):
    from chaarteeke import doses

    doses.mark_given(ctx, patient, 2, "patient", clock.local_today(ctx.settings.local_tz), "patient:x")
    by_phone = unmark(client, people["clinic"], patient)
    assert by_phone.status_code == 409 and "by phone" in by_phone.json()["message"]
    first = unmark(client, people["clinic"], patient, 1)  # given at enrolment: no recent mark to take back
    assert first.status_code == 409 and first.json()["error"] == "nothing_to_undo"
    assert unmark(client, people["clinic"], patient, 9).status_code == 404


def test_another_clinic_cannot_unmark(client, ctx, people, other_clinic, patient):
    mark(client, people["clinic"], patient)
    assert unmark(client, other_clinic, patient).status_code == 403
    assert dose_status(ctx, patient)["status"] == "given"


def link_state(ctx, missed_call_id):
    return ctx.db.one("SELECT * FROM missed_calls WHERE id = ?", (missed_call_id,))


def test_a_link_can_be_taken_back_before_the_first_call_and_made_again(client, ctx, people):
    clinic = people["clinic"]
    course = client.post("/api/courses", json=enrol_body(ctx), headers=clinic.write).json()["id"]
    got = enrol.record_missed_call(ctx, NUMBER, "ari", clock.now())
    client.post(
        f"/api/missed-calls/{got.missed_call_id}/link", json={"course_id": course}, headers=clinic.write
    )
    assert ctx.db.one("SELECT 1 FROM calls WHERE job_key = ?", (f"first:{course}",))
    q = ctx.bus.subscribe()
    res = client.post(f"/api/missed-calls/{got.missed_call_id}/unlink", headers=clinic.write)
    assert res.status_code == 200, res.text
    assert res.json()["id"] == course and res.json()["status"] == "awaiting_call"
    mc = link_state(ctx, got.missed_call_id)
    assert mc["status"] == "unlinked" and mc["course_id"] is None
    assert ctx.vault.decrypt(mc["phone_cipher"]).endswith(NUMBER[-10:])  # the number is back where it was
    assert ctx.db.one("SELECT 1 FROM contacts WHERE course_id = ?", (course,)) is None
    assert (
        ctx.db.one("SELECT 1 FROM calls WHERE course_id = ?", (course,)) is None
    )  # the first call, never placed
    assert [r["actor"] for r in audit_actions(ctx, "missed_call.unlink")] == ["staff:clinic"]
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert ("missed_call", "unlinked") in [(e.type, e.data.get("status")) for e in events]
    assert all("phone" not in str(e.data).lower().replace("phone_last4", "") for e in events)
    again = client.post(
        f"/api/missed-calls/{got.missed_call_id}/link", json={"course_id": course}, headers=clinic.write
    )
    assert again.status_code == 200 and again.json()["status"] == "active"  # linking works again
    assert (
        ctx.db.one("SELECT status FROM calls WHERE job_key = ?", (f"first:{course}",))["status"]
        == "scheduled"
    )


def test_a_link_cannot_be_taken_back_once_the_first_call_is_placed(client, ctx, people, patient):
    clinic = people["clinic"]
    mc = ctx.db.one("SELECT id FROM missed_calls WHERE course_id = ?", (patient,))["id"]
    ctx.db.connect().execute(
        "UPDATE calls SET status = 'dialing' WHERE course_id = ? AND kind = 'first'", (patient,)
    )
    res = client.post(f"/api/missed-calls/{mc}/unlink", headers=clinic.write)
    assert res.status_code == 409 and res.json()["error"] == "already_placed"
    assert link_state(ctx, mc)["status"] == "linked" and ctx.db.one(
        "SELECT 1 FROM contacts WHERE course_id = ?", (patient,)
    )
    assert audit_actions(ctx, "missed_call.unlink") == []


def test_unlink_refuses_what_is_not_linked_what_is_not_there_and_another_clinics(
    client, ctx, people, other_clinic, patient
):
    free = enrol.record_missed_call(ctx, "9123456780", "ari", clock.now()).missed_call_id
    res = client.post(f"/api/missed-calls/{free}/unlink", headers=people["clinic"].write)
    assert res.status_code == 409 and res.json()["error"] == "nothing_to_undo"
    assert (
        client.post("/api/missed-calls/mc_nobody/unlink", headers=people["clinic"].write).status_code == 404
    )
    mine = ctx.db.one("SELECT id FROM missed_calls WHERE course_id = ?", (patient,))["id"]
    assert client.post(f"/api/missed-calls/{mine}/unlink", headers=other_clinic.write).status_code == 403
    assert link_state(ctx, mine)["status"] == "linked"


def test_a_callback_closed_by_mistake_can_be_reopened_inside_the_window(
    client, ctx, people, other_clinic, clock_t0
):
    alert = claimable_alert(ctx)
    vet, clinic = people["vet"], people["clinic"]
    client.post(f"/api/alerts/{alert['id']}/claim", json={}, headers=vet.write)
    cb = client.post(f"/api/alerts/{alert['id']}/callback", headers=vet.write).json()["id"]
    assert (
        client.post(f"/api/callbacks/{cb}/reopen", headers=clinic.write).json()["error"] == "nothing_to_undo"
    )
    client.post(
        f"/api/callbacks/{cb}/done",
        json={"note": "wrong patient", "outcome": "declined"},
        headers=clinic.write,
    )
    assert cb not in [c["id"] for c in client.get("/api/clinic/callbacks", headers=clinic.read).json()]
    assert client.post(f"/api/callbacks/{cb}/reopen", headers=other_clinic.write).status_code == 403
    q = ctx.bus.subscribe()
    res = client.post(f"/api/callbacks/{cb}/reopen", headers=clinic.write)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "open" and res.json()["course_id"] == alert["course_id"]
    row = ctx.db.one("SELECT status, done_at, note FROM callback_requests WHERE id = ?", (cb,))
    assert (row["status"], row["done_at"], row["note"]) == ("open", None, None)
    assert cb in [c["id"] for c in client.get("/api/clinic/callbacks", headers=clinic.read).json()]
    assert [r["actor"] for r in audit_actions(ctx, "callback.reopen")] == ["staff:clinic"]
    assert len(audit_actions(ctx, "callback.declined")) == 1  # the closing stays in the log
    types = []
    while not q.empty():
        ev = q.get_nowait()
        types.append((ev.type, ev.data.get("status")))
    assert ("callback", "open") in types
    closed_again = client.post(f"/api/callbacks/{cb}/done", json={}, headers=clinic.write)
    assert closed_again.status_code == 200  # and it can be closed properly this time
    clock_t0.advance(timedelta(minutes=ctx.policy["clinic"]["undo_callback_minutes"], seconds=1))
    late = client.post(f"/api/callbacks/{cb}/reopen", headers=clinic.write)
    assert late.status_code == 409 and late.json()["error"] == "undo_window_over"


def test_a_callback_is_not_reopened_while_the_vet_has_asked_again(client, ctx, people):
    alert = claimable_alert(ctx)
    vet, clinic = people["vet"], people["clinic"]
    client.post(f"/api/alerts/{alert['id']}/claim", json={}, headers=vet.write)
    first = client.post(f"/api/alerts/{alert['id']}/callback", headers=vet.write).json()["id"]
    client.post(f"/api/callbacks/{first}/done", json={}, headers=clinic.write)
    client.post(f"/api/alerts/{alert['id']}/callback", headers=vet.write)  # a new request is open
    res = client.post(f"/api/callbacks/{first}/reopen", headers=clinic.write)
    assert res.status_code == 409 and res.json()["error"] == "already_open"


def test_the_undo_windows_come_from_policy(ctx):
    assert (
        ctx.policy["clinic"]["undo_dose_minutes"] == 15
        and ctx.policy["clinic"]["undo_callback_minutes"] == 15
    )
