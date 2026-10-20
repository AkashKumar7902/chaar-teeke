"""Every enrolment path and error code, missed calls, link, withdraw, stop, reveal and the Today list."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from chaarteeke import clock, doses, enrol, privacy
from chaarteeke.enrol import EnrolError


def codes(fn, *a, **kw) -> str:
    with pytest.raises(EnrolError) as e:
        fn(*a, **kw)
    return e.value.code


# ------------------------------------------------------------------- create
def test_create_course_happy_path(ctx, world, events):
    world.clinic()
    events()
    course = enrol.create_course(ctx, world.data(register_no="ARC-7", lang="doi"), "staff:asha")
    assert course.status == "awaiting_call"
    assert (course.register_no, course.lang, course.source) == ("ARC-7", "doi", "clinic")
    assert course.regimen_name.startswith("Intradermal")
    assert course.phone_last4 is None and course.phone_purged is False
    assert events() == [("course", {"course_id": course.id, "status": "awaiting_call"})]
    assert ctx.db.one("SELECT actor FROM audit WHERE action='course.create'")["actor"] == "staff:asha"


def test_create_course_errors(ctx, world):
    world.clinic()
    assert codes(enrol.create_course, ctx, world.data(clinic_id="cln_nope"), "staff:a") == "unknown_clinic"
    assert codes(enrol.create_course, ctx, world.data(regimen="id_1week"), "staff:a") == "regimen_disabled"
    assert codes(enrol.create_course, ctx, world.data(regimen="im_abbrev"), "staff:a") == "regimen_disabled"
    assert codes(enrol.create_course, ctx, world.data(regimen="made_up"), "staff:a") == "unknown_regimen"
    bad = world.data().model_copy(update={"day0": "19-10-2026"})
    assert codes(enrol.create_course, ctx, bad, "staff:a") == "bad_date"
    future = world.data(day0=world.today() + timedelta(days=1))
    assert codes(enrol.create_course, ctx, future, "staff:a") == "bad_date"
    assert ctx.db.one("SELECT COUNT(*) FROM courses")[0] == 0


@pytest.mark.parametrize("text", ["20261019", "2026-W43-1", " 2026-10-19", "2026-10-19T00:00"])
def test_only_a_plain_iso_date_is_taken_as_the_first_dose_date(ctx, world, text):
    """Python also reads 20261019 and week dates; the register and the stats lane compare day0 as text."""
    world.clinic()
    bad = world.data(day0=world.today()).model_copy(update={"day0": text})
    assert codes(enrol.create_course, ctx, bad, "staff:a") == "bad_date"
    assert ctx.db.one("SELECT COUNT(*) FROM courses")[0] == 0


# -------------------------------------------------------------- missed calls
def test_withheld_numbers(ctx, world, events):
    events()
    for number in (None, "", "anonymous", "12345"):
        res = enrol.record_missed_call(ctx, number, "ari", clock.now())
        assert res.status == "withheld" and res.course_id is None
    row = ctx.db.one("SELECT * FROM missed_calls LIMIT 1")
    assert row["status"] == "withheld" and row["phone_cipher"] is None
    assert [d["status"] for t, d in events() if t == "missed_call"] == ["withheld"] * 4


def test_unlinked_missed_call_is_stored_encrypted_and_never_in_events(ctx, world, events):
    events()
    res = enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    assert res.status == "unlinked"
    row = ctx.db.one("SELECT * FROM missed_calls WHERE id=?", (res.missed_call_id,))
    assert row["phone_last4"] == "3210"
    assert "9876543210" not in row["phone_cipher"]
    assert ctx.vault.decrypt(row["phone_cipher"]) == "+919876543210"
    seen = json.dumps(events()) + json.dumps([dict(r) for r in ctx.db.query("SELECT * FROM audit")])
    assert "9876543210" not in seen
    assert '"phone_last4": "3210"' in seen


def test_number_forms_are_one_number(ctx, world):
    first = enrol.record_missed_call(ctx, "09876543210", "ari", clock.now())
    second = enrol.record_missed_call(ctx, "+91 98765 43210", "ari", clock.now())
    assert (first.status, second.status) == ("unlinked", "duplicate")


def test_second_call_from_an_unlinked_number_does_not_add_to_the_list(ctx, world):
    enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    world.clinic()
    assert enrol.clinic_today(ctx, "cln_a").counts["unlinked"] == 1


def test_clinic_hint_must_exist(ctx, world):
    assert (
        codes(enrol.record_missed_call, ctx, "+919876543210", "ari", clock.now(), "cln_nope")
        == "unknown_clinic"
    )


# ---------------------------------------------------------------------- link
def test_link_moves_the_number_and_schedules_the_first_call(ctx, world, fixed_clock, events):
    cid = world.course(regimen="id_trc")
    res = enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    events()
    course = enrol.link(ctx, res.missed_call_id, cid, "staff:asha")
    assert course.status == "active" and course.phone_last4 == "3210"
    mc = ctx.db.one("SELECT * FROM missed_calls WHERE id=?", (res.missed_call_id,))
    assert (mc["status"], mc["course_id"], mc["phone_cipher"]) == ("linked", cid, None)
    contact = ctx.db.one("SELECT * FROM contacts WHERE course_id=?", (cid,))
    assert ctx.vault.decrypt(contact["phone_cipher"]) == "+919876543210"
    # last dose is day 28; purge 7 days later, at the start of that local day
    purge = clock.at_local(world.today() + timedelta(days=35), "00:00")
    assert contact["purge_after"] == clock.iso(purge)
    first = world.calls(cid, "first")
    assert len(first) == 1 and first[0]["job_key"] == f"first:{cid}"
    delay = ctx.policy["calls"]["first_call_delay_s"]
    assert first[0]["scheduled_for"] == clock.iso(clock.now() + timedelta(seconds=delay))
    assert first[0]["status"] == "scheduled" and first[0]["transport"] == "ari"
    types = [t for t, _ in events()]
    assert sorted(types) == ["call", "course", "missed_call"]
    assert "missed_call.link" in world.audit_actions()


def test_purge_date_follows_the_regimen(ctx, world):
    cid = world.active(regimen="reexposure")  # last dose day 3
    contact = ctx.db.one("SELECT purge_after FROM contacts WHERE course_id=?", (cid,))
    assert contact["purge_after"] == clock.iso(clock.at_local(world.today() + timedelta(days=10), "00:00"))


def test_link_errors(ctx, world):
    cid = world.course()
    res = enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    assert codes(enrol.link, ctx, "mc_nope", cid, "staff:a") == "not_found"
    assert codes(enrol.link, ctx, res.missed_call_id, "crs_nope", "staff:a") == "not_found"
    enrol.link(ctx, res.missed_call_id, cid, "staff:a")
    # the same missed call again
    assert codes(enrol.link, ctx, res.missed_call_id, cid, "staff:a") == "already_linked"
    # a new missed call to a course that already has a number
    other = enrol.record_missed_call(ctx, "+919876543211", "ari", clock.now())
    assert codes(enrol.link, ctx, other.missed_call_id, cid, "staff:a") == "not_awaiting"
    # a closed course
    enrol.stop(ctx, cid, "moved away", "staff:a")
    assert codes(enrol.link, ctx, other.missed_call_id, cid, "staff:a") == "course_closed"
    # withheld rows have nothing to link
    withheld = enrol.record_missed_call(ctx, None, "ari", clock.now())
    fresh = world.course(register_no="R2")
    assert codes(enrol.link, ctx, withheld.missed_call_id, fresh, "staff:a") == "missed_call_closed"


def test_a_missed_call_meant_for_one_clinic_cannot_be_linked_to_another_clinics_patient(ctx, world):
    world.clinic("cln_a")
    world.clinic("cln_b", name="Other clinic")
    theirs = world.course(clinic_id="cln_b")
    ours = world.course(clinic_id="cln_a", register_no="R2")
    hinted = enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now(), clinic_hint="cln_a")
    assert codes(enrol.link, ctx, hinted.missed_call_id, theirs, "staff:b") == "wrong_clinic"
    assert enrol.get_course(ctx, theirs).status == "awaiting_call"
    assert ctx.db.one("SELECT COUNT(*) FROM contacts")[0] == 0
    assert enrol.link(ctx, hinted.missed_call_id, ours, "staff:a").status == "active"
    # a missed call with no hint can be linked by any clinic to its own patient
    open_call = enrol.record_missed_call(ctx, "+919876543211", "ari", clock.now())
    assert enrol.link(ctx, open_call.missed_call_id, theirs, "staff:b").status == "active"


def test_link_refuses_a_missed_call_older_than_the_keep_time(ctx, world, fixed_clock):
    cid = world.course()
    res = enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    fixed_clock.advance(timedelta(hours=ctx.policy["retention"]["unlinked_missed_call_hours"], seconds=1))
    assert codes(enrol.link, ctx, res.missed_call_id, cid, "staff:a") == "missed_call_closed"


# -------------------------------------------------- duplicate and withdrawal
def test_missed_call_from_an_enrolled_number_gets_one_short_notice(ctx, world, fixed_clock):
    cid = world.active(number="+919876543210")
    res = enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    assert (res.status, res.course_id) == ("duplicate", cid)
    notice = world.calls(cid, "duplicate_notice")
    assert len(notice) == 1 and notice[0]["status"] == "scheduled"
    # the number is on the row for the 48 hours any missed call is kept, so staff can link it to another
    # register entry; it is the number the course already holds
    kept = ctx.db.one("SELECT phone_cipher FROM missed_calls WHERE id=?", (res.missed_call_id,))[0]
    assert ctx.vault.decrypt(kept) == "+919876543210"
    # a further call while the notice is still waiting does not queue a second notice
    again = enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    assert again.status == "duplicate" and len(world.calls(cid, "duplicate_notice")) == 1
    assert enrol.get_course(ctx, cid).status == "active"


def test_a_missed_call_never_withdraws_a_patient_however_often_it_comes(ctx, world, fixed_clock):
    cid = world.active(number="+919876543210")
    enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    ctx.db.connect().execute("UPDATE calls SET status='completed' WHERE kind='duplicate_notice'")
    res = enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    assert (res.status, res.course_id) == ("duplicate", cid)
    assert enrol.get_course(ctx, cid).status == "active"
    assert world.calls(cid, "withdrawn_notice") == []
    notices = world.calls(cid, "duplicate_notice")
    assert [c["status"] for c in notices] == ["completed", "scheduled"]  # the notice is played again, with 9
    # twenty days on, after the notice was played, a call is still just the notice again
    ctx.db.connect().execute("UPDATE calls SET status='completed' WHERE kind='duplicate_notice'")
    fixed_clock.advance(timedelta(days=20))
    late = enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    assert late.status == "duplicate"
    assert enrol.get_course(ctx, cid).phone_purged is False
    assert len(world.calls(cid, "duplicate_notice")) == 3
    assert "course.withdraw" not in world.audit_actions()


def test_a_repeat_call_before_the_first_call_is_done_asks_for_the_first_call_again(
    ctx, world, fixed_clock, events
):
    cid = world.active(number="+919876543210", consent=False)
    first = world.calls(cid, "first")[0]
    ctx.db.connect().execute("UPDATE calls SET status='no_answer' WHERE id=?", (first["id"],))
    events()
    res = enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    assert (res.status, res.course_id) == ("duplicate", cid)
    assert world.calls(cid, "duplicate_notice") == []  # no notice: they have not even heard the first call
    rows = world.calls(cid, "first")
    assert [r["status"] for r in rows] == ["no_answer", "scheduled"]
    assert rows[1]["attempt"] == 1 and rows[1]["job_key"] == f"first:{cid}:{res.missed_call_id}"
    assert rows[1]["scheduled_for"] == clock.iso(
        clock.now() + timedelta(seconds=ctx.policy["calls"]["first_call_delay_s"])
    )
    assert enrol.get_course(ctx, cid).status == "active"
    # asking again while that call waits changes nothing but its time, and never starts a second one
    fixed_clock.advance(timedelta(seconds=3))
    enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    assert len(world.calls(cid, "first")) == 2


def test_a_repeat_call_brings_a_waiting_first_call_forward(ctx, world, fixed_clock):
    cid = world.active(number="+919876543210", consent=False)
    waiting = world.calls(cid, "first")[0]
    ctx.db.connect().execute(
        "UPDATE calls SET scheduled_for=? WHERE id=?",
        (clock.iso(clock.now() + timedelta(hours=2)), waiting["id"]),
    )
    enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    rows = world.calls(cid, "first")
    assert len(rows) == 1
    assert rows[0]["scheduled_for"] == clock.iso(
        clock.now() + timedelta(seconds=ctx.policy["calls"]["first_call_delay_s"])
    )


def test_a_repeat_call_while_the_first_call_is_ringing_does_nothing_to_it(ctx, world, fixed_clock):
    cid = world.active(number="+919876543210", consent=False)
    ctx.db.connect().execute("UPDATE calls SET status='ringing' WHERE kind='first'")
    enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    assert [c["status"] for c in world.calls(cid, "first")] == ["ringing"]
    assert world.calls(cid, "duplicate_notice") == []


def test_withdraw_stops_everything_and_deletes_the_number(ctx, world, events):
    cid = world.active()
    events()
    enrol.withdraw(ctx, cid, f"patient:{cid}", "key 9 on a call")
    course = enrol.get_course(ctx, cid)
    assert (course.status, course.phone_purged, course.phone_last4) == ("stopped", True, None)
    assert [c["status"] for c in world.calls(cid)] == ["cancelled"]
    log = ctx.db.one("SELECT * FROM purge_log WHERE course_id=?", (cid,))
    assert (log["what"], log["reason"]) == ("phone", "withdrawn")
    assert ("course", {"course_id": cid, "status": "stopped"}) in events()
    actor = ctx.db.one("SELECT actor, detail_json FROM audit WHERE action='course.withdraw'")
    assert actor["actor"] == f"patient:{cid}"
    # a second withdrawal changes nothing and does not fail
    n = ctx.db.one("SELECT COUNT(*) FROM audit")[0]
    enrol.withdraw(ctx, cid, f"patient:{cid}", "again")
    assert ctx.db.one("SELECT COUNT(*) FROM audit")[0] == n
    assert codes(enrol.withdraw, ctx, "crs_nope", "x", "y") == "not_found"


def test_withdraw_works_for_a_course_with_no_number_yet(ctx, world):
    cid = world.course()
    enrol.withdraw(ctx, cid, "staff:a", "patient asked")
    assert enrol.get_course(ctx, cid).status == "stopped"


# ----------------------------------------------------------- stop and vaccine
def test_stop(ctx, world, events):
    cid = world.active()
    events()
    enrol.stop(ctx, cid, "moved to another city", "staff:asha")
    row = ctx.db.one("SELECT status, stopped_reason FROM courses WHERE id=?", (cid,))
    assert (row["status"], row["stopped_reason"]) == ("stopped", "moved to another city")
    assert [c["status"] for c in world.calls(cid)] == ["cancelled"]
    assert ("course", {"course_id": cid, "status": "stopped"}) in events()
    assert codes(enrol.stop, ctx, cid, "again", "staff:a") == "course_closed"
    assert codes(enrol.stop, ctx, "crs_nope", "x", "staff:a") == "not_found"
    done = world.active(regimen="reexposure", day0=world.today() - timedelta(days=3))
    doses.mark_given(ctx, done, 2, "clinic", world.today(), "staff:a")
    assert codes(enrol.stop, ctx, done, "late", "staff:a") == "course_closed"


def test_a_stopped_course_keeps_the_number_for_seven_days_at_most(ctx, world, fixed_clock):
    long = world.active(regimen="im_essen")  # last dose on day 28: the number would stay for 35 days
    short = world.active(  # already due to go in 2 days: a stop never keeps it longer
        regimen="reexposure", register_no="S", day0=world.today() - timedelta(days=8)
    )
    before = ctx.db.one("SELECT purge_after FROM contacts WHERE course_id=?", (short,))["purge_after"]
    enrol.stop(ctx, long, "moved to another city", "staff:asha")
    enrol.stop(ctx, short, "moved to another city", "staff:asha")
    week = clock.iso(clock.at_local(world.today() + timedelta(days=7), "00:00", "Asia/Kolkata"))
    assert ctx.db.one("SELECT purge_after FROM contacts WHERE course_id=?", (long,))["purge_after"] == week
    assert ctx.db.one("SELECT purge_after FROM contacts WHERE course_id=?", (short,))["purge_after"] == before
    fixed_clock.advance(timedelta(days=7))
    assert privacy.run_purges(ctx).phones == 2
    assert ctx.db.one("SELECT 1 FROM contacts WHERE course_id=?", (long,)) is None


def test_vaccine_out(ctx, world, fixed_clock, events):
    world.clinic()
    events()
    enrol.set_vaccine_out(ctx, "cln_a", True, "staff:asha")
    t = enrol.clinic_today(ctx, "cln_a")
    assert t.vaccine_out is True and t.vaccine_out_since == clock.iso(clock.now())
    assert events() == [("system", {"check": "vaccine_out:cln_a", "ok": False})]
    since = t.vaccine_out_since
    fixed_clock.advance(timedelta(hours=2))
    enrol.set_vaccine_out(ctx, "cln_a", True, "staff:asha")  # no change, no new time
    assert enrol.clinic_today(ctx, "cln_a").vaccine_out_since == since
    enrol.set_vaccine_out(ctx, "cln_a", False, "staff:asha")
    t = enrol.clinic_today(ctx, "cln_a")
    assert (t.vaccine_out, t.vaccine_out_since) == (False, None)
    assert world.audit_actions().count("clinic.vaccine_out") == 2
    assert codes(enrol.set_vaccine_out, ctx, "cln_nope", True, "staff:a") == "unknown_clinic"


# ------------------------------------------------------------- reveal phone
def test_reveal_is_audited(ctx, world):
    cid = world.active(number="+919876543210")
    assert enrol.reveal_phone(ctx, cid, "staff:asha") == "+919876543210"
    row = ctx.db.one("SELECT * FROM audit WHERE action='phone.reveal'")
    assert (row["actor"], row["object_id"]) == ("staff:asha", cid)
    assert "9876543210" not in (row["detail_json"] or "")
    enrol.reveal_phone(ctx, cid, "staff:asha")
    assert world.audit_actions().count("phone.reveal") == 2
    assert codes(enrol.reveal_phone, ctx, "crs_nope", "staff:a") == "not_found"


def test_reveal_is_refused_after_the_purge_and_the_refusal_is_audited(ctx, world, fixed_clock):
    cid = world.active(regimen="reexposure", number="+919876543210")
    fixed_clock.advance(timedelta(days=10))
    # past purge_after but the job has not run yet: still refused
    assert codes(enrol.reveal_phone, ctx, cid, "staff:asha") == "phone_purged"
    privacy.run_purges(ctx)
    assert codes(enrol.reveal_phone, ctx, cid, "staff:asha") == "phone_purged"
    assert world.audit_actions().count("phone.reveal_refused") == 2
    assert "phone.reveal" not in world.audit_actions()
    assert enrol.get_course(ctx, cid).phone_purged is True


def test_reveal_is_refused_for_a_course_with_no_number(ctx, world):
    cid = world.course()
    assert codes(enrol.reveal_phone, ctx, cid, "staff:a") == "phone_purged"


# ----------------------------------------------------------------- demo mode
def test_the_dialler_is_not_given_a_number_past_its_keep_time(ctx, world, fixed_clock):
    cid = world.active(number="+919876543210", regimen="reexposure")
    call_id = world.calls(cid, "first")[0]["id"]
    assert enrol.dial_number(ctx, call_id) == "+919876543210"
    purge_after = clock.parse_iso(ctx.db.one("SELECT purge_after FROM contacts")["purge_after"])
    fixed_clock.set(purge_after)  # the purge job has not run yet
    assert codes(enrol.dial_number, ctx, call_id) == "phone_purged"


def test_demo_sim_number_is_linked_at_once_to_the_visitor_desk(ctx, world, events):
    events()
    res = enrol.record_missed_call(ctx, "sim:abc123", "sim", clock.now())
    assert res.status == "linked" and res.course_id
    clinic = ctx.db.one("SELECT * FROM clinics WHERE id='cln_demo_visitor'")
    assert clinic["is_demo"] == 1 and clinic["name"] == "Demo visitor desk"
    course = enrol.get_course(ctx, res.course_id)
    assert (course.status, course.regimen_id, course.eligibility, course.source) == (
        "active",
        "id_trc",
        "adult",
        "simulator",
    )
    assert course.clinic_id == "cln_demo_visitor"
    first = world.calls(res.course_id, "first")
    assert len(first) == 1 and first[0]["transport"] == "sim" and first[0]["is_demo"] == 1
    assert {d["simulated"] for t, d in events() if t == "call"} == {True}
    # the second demo course reuses the clinic
    enrol.record_missed_call(ctx, "sim:def456", "sim", clock.now())
    assert ctx.db.one("SELECT COUNT(*) FROM clinics WHERE id='cln_demo_visitor'")[0] == 1
    assert ctx.db.one("SELECT COUNT(*) FROM audit WHERE action='clinic.create'")[0] == 1
    # the same browser phone again is a duplicate, not another course
    again = enrol.record_missed_call(ctx, "sim:abc123", "sim", clock.now())
    assert (again.status, again.course_id) == ("duplicate", res.course_id)


def test_sim_number_is_not_auto_linked_in_pilot_mode(ctx, world):
    ctx.settings.mode = "pilot"
    res = enrol.record_missed_call(ctx, "sim:abc123", "sim", clock.now())
    assert res.status == "unlinked"
    assert ctx.db.one("SELECT COUNT(*) FROM clinics")[0] == 0


def test_a_sim_number_over_a_real_line_is_not_a_number(ctx, world):
    assert enrol.record_missed_call(ctx, "sim:abc123", "ari", clock.now()).status == "withheld"


# ------------------------------------------------------------ get_course
def test_get_course_lists_doses_and_calls(ctx, world):
    cid = world.active(number="+919876543210", regimen="im_essen")
    course = enrol.get_course(ctx, cid)
    assert len(course.doses) == 5 and course.phone_last4 == "3210"
    assert [(c.kind, c.status, c.attempt) for c in course.calls] == [("first", "scheduled", 1)]
    assert codes(enrol.get_course, ctx, "crs_nope") == "not_found"


# ------------------------------------------------------------- clinic_today
def _alert_for(ctx, clinic_id, course_id, ward_no):
    """A bite report, an alert and an open callback request, as the stats lane would leave them."""
    now = clock.iso(clock.now())
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO bite_reports(id, clinic_id, bite_week, location_status, source, created_at)"
            " VALUES ('rep_1', ?, '2026-10-19', 'matched', 'clinic', ?)",
            (clinic_id, now),
        )
        conn.execute(
            "INSERT INTO alerts(id, report_id, ward_no, place_type, local_day, status, created_at)"
            " VALUES ('alr_1', 'rep_1', ?, 'home_street', '2026-10-19', 'new', ?)",
            (ward_no, now),
        )
        conn.execute(
            "INSERT INTO callback_requests(id, alert_id, course_id, requested_by, requested_at, status)"
            " VALUES ('cb_1','alr_1',?, 'staff:vet', ?, 'open')",
            (course_id, now),
        )
        conn.execute(
            "INSERT INTO callback_requests(id, alert_id, course_id, requested_by, requested_at, status)"
            " VALUES ('cb_2','alr_1',?, 'staff:vet', ?, 'done')",
            (course_id, now),
        )


def test_clinic_today_counts_agree_with_the_lists(ctx, world):
    today = world.today()
    world.clinic("cln_b", "Other clinic")
    due_open = world.active(day0=today - timedelta(days=3), register_no="DUE-1")
    due_marked = world.active(day0=today - timedelta(days=3), register_no="DUE-2")
    doses.mark_given(ctx, due_marked, 2, "clinic", today, "staff:a")
    due_patient = world.active(day0=today - timedelta(days=3), register_no="DUE-3")
    doses.mark_given(ctx, due_patient, 2, "patient", today, f"patient:{due_patient}")
    minor = world.active(day0=today - timedelta(days=4), register_no="LATE-1")
    review = world.active(day0=today - timedelta(days=9), register_no="LATE-2")
    waiting = world.course(register_no="WAIT-1")
    on_time = world.active(day0=today - timedelta(days=1), register_no="LATER", number="+919000000001")
    other_clinic = world.active(clinic_id="cln_b", day0=today - timedelta(days=4), register_no="OTHER")
    unlinked = enrol.record_missed_call(ctx, "+919888888888", "ari", clock.now())
    _alert_for(ctx, "cln_a", review, 12)

    t = enrol.clinic_today(ctx, "cln_a")
    ids = lambda rows: sorted(r.course_id for r in rows)  # noqa: E731
    assert ids(t.due_today) == sorted([due_open, due_marked, due_patient])
    assert {r.course_id: r.state for r in t.due_today} == {
        due_open: "due_today",
        due_marked: "marked_given",
        due_patient: "patient_says_taken",
    }
    assert [r.course_id for r in t.missed] == [minor, review]  # the most recent miss first
    assert [r.state for r in t.missed] == ["late_minor", "late_review"]
    assert ids(t.awaiting_call) == [waiting]
    assert [u.id for u in t.unlinked] == [unlinked.missed_call_id]
    assert t.unlinked[0].phone_last4 == "8888"
    assert [(c.id, c.register_no, c.ward_no, c.status) for c in t.callbacks] == [
        ("cb_1", "LATE-2", 12, "open")
    ]
    assert t.counts == {
        "due_today": len(t.due_today),
        "missed": len(t.missed),
        "late_review": 1,
        "awaiting_call": 1,
        "unlinked": 1,
        "callbacks": 1,
    }
    assert t.counts == {
        "due_today": 3,
        "missed": 2,
        "late_review": 1,
        "awaiting_call": 1,
        "unlinked": 1,
        "callbacks": 1,
    }
    # a patient appears once, the other clinic's patient and the on-time one not at all
    everyone = [r.course_id for r in t.due_today + t.missed + t.awaiting_call]
    assert len(everyone) == len(set(everyone)) and on_time not in everyone and other_clinic not in everyone
    assert t.next_due_date == (today + timedelta(days=2)).isoformat()  # dose 2 of the on-time patient (day 3)
    assert (t.clinic_id, t.clinic_name, t.local_date) == ("cln_a", "Test clinic", today.isoformat())
    # the row carries the last four digits only
    row = next(r for r in t.due_today if r.course_id == due_open)
    assert row.phone_last4 and len(row.phone_last4) == 4 and row.next_dose_no == 2


def test_clinic_today_empty_clinic_says_when_the_next_dose_is_due(ctx, world):
    world.clinic()
    t = enrol.clinic_today(ctx, "cln_a")
    assert t.counts == {k: 0 for k in t.counts} and t.next_due_date is None
    world.active(day0=world.today(), register_no="X")
    t = enrol.clinic_today(ctx, "cln_a")
    assert t.counts["due_today"] == 0 and t.next_due_date == (world.today() + timedelta(days=3)).isoformat()
    assert codes(enrol.clinic_today, ctx, "cln_nope") == "unknown_clinic"


def test_clinic_today_shows_how_the_reminders_went(ctx, world):
    today = world.today()
    cid = world.active(day0=today - timedelta(days=3), register_no="CALLS")

    def add(status, attempt):
        with ctx.db.transaction() as conn:
            enrol.new_call(
                conn,
                course_id=cid,
                kind="reminder",
                job_key=f"reminder:{cid}:2:a{attempt}",
                scheduled_for=clock.now() + timedelta(minutes=attempt),
                dose_no=2,
                attempt=attempt,
            )
            conn.execute(
                "UPDATE calls SET status=? WHERE job_key=?", (status, f"reminder:{cid}:2:a{attempt}")
            )

    def row():
        return enrol.clinic_today(ctx, "cln_a").due_today[0]

    assert (row().state, row().attempts) == ("due_today", 0)
    add("no_answer", 1)
    add("scheduled", 2)
    assert (row().state, row().attempts, row().last_call_status) == ("tried", 1, "no_answer")
    ctx.db.connect().execute("UPDATE calls SET status='no_answer' WHERE job_key LIKE '%a2'")
    assert (row().state, row().attempts) == ("no_answer", 2)
    add("completed", 3)
    assert (row().state, row().last_call_status) == ("reminded", "completed")


def test_unlinked_list_respects_clinic_hint_and_age(ctx, world, fixed_clock):
    world.clinic("cln_b", "Other clinic")
    world.clinic()
    enrol.record_missed_call(ctx, "+919000000001", "ari", clock.now(), "cln_b")
    enrol.record_missed_call(ctx, "+919000000002", "ari", clock.now())
    assert enrol.clinic_today(ctx, "cln_a").counts["unlinked"] == 1
    assert enrol.clinic_today(ctx, "cln_b").counts["unlinked"] == 2
    fixed_clock.advance(timedelta(hours=49))
    assert enrol.clinic_today(ctx, "cln_b").counts["unlinked"] == 0


# ----------------------------------------------------------- withdraw takes the clip with it
def bite_report(ctx, world, cid, *, status, decided=False, clip=True):
    clips = ctx.settings.var_dir / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    file = clips / "rep_w.wav"
    if clip:
        file.write_bytes(b"RIFF....WAVE")
    now = clock.iso(clock.now())
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO bite_reports(id, course_id, clinic_id, bite_week, location_status, source, created_at)"
            " VALUES ('rep_w', ?, 'cln_a', '2026-10-19', ?, 'clinic', ?)",
            (cid, status, now),
        )
        conn.execute(
            "INSERT INTO place_captures(report_id, audio_path, transcript, candidates_json, asr_status,"
            " decided_at, expires_at) VALUES ('rep_w', ?, 'near the old bus stand', '[]', 'done', ?, ?)",
            (str(file), now if decided else None, now),
        )
    return file


def test_withdraw_deletes_the_clip_and_transcript_and_an_undecided_place_becomes_cant_tell(ctx, world):
    cid = world.active()
    file = bite_report(ctx, world, cid, status="needs_review")
    enrol.withdraw(ctx, cid, f"patient:{cid}", "key 9 on a call")
    assert not file.exists()
    cap = ctx.db.one("SELECT * FROM place_captures WHERE report_id='rep_w'")
    assert (cap["audio_path"], cap["transcript"], cap["candidates_json"]) == (None, None, None)
    assert cap["clip_deleted_at"] == clock.iso(clock.now())
    assert ctx.db.one("SELECT location_status FROM bite_reports")["location_status"] == "cant_tell"
    log = ctx.db.one("SELECT * FROM purge_log WHERE what='clip'")
    assert (log["course_id"], log["reason"]) == (cid, "withdrawn")
    assert "clip.purge" in world.audit_actions()


def test_withdraw_keeps_a_place_somebody_already_decided(ctx, world):
    cid = world.active()
    file = bite_report(ctx, world, cid, status="decided", decided=True)
    enrol.withdraw(ctx, cid, f"patient:{cid}", "key 9 on a call")
    assert not file.exists()
    assert ctx.db.one("SELECT location_status FROM bite_reports")["location_status"] == "decided"
    assert ctx.db.one("SELECT transcript FROM place_captures")["transcript"] is None


def test_withdraw_with_a_clip_that_cannot_be_deleted_fails_whole_and_can_be_repeated(ctx, world, monkeypatch):
    cid = world.active()
    file = bite_report(ctx, world, cid, status="pending")
    real_unlink = type(file).unlink

    def broken(self, *a, **kw):
        raise PermissionError("read-only disk")

    monkeypatch.setattr(type(file), "unlink", broken)
    with pytest.raises(PermissionError):
        enrol.withdraw(ctx, cid, f"patient:{cid}", "key 9 on a call")
    assert enrol.get_course(ctx, cid).status == "active"  # nothing half done
    assert ctx.db.one("SELECT transcript FROM place_captures")["transcript"] is not None
    monkeypatch.setattr(type(file), "unlink", real_unlink)
    enrol.withdraw(ctx, cid, f"patient:{cid}", "again")
    assert not file.exists() and enrol.get_course(ctx, cid).status == "stopped"
