"""Regimen calendars, late states (DOMAIN R5) and marking doses given."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from chaarteeke import clock, doses, enrol, regimens
from chaarteeke.doses import DoseError
from chaarteeke.models import EnrolIn


def test_schedule_for_every_enabled_regimen():
    day0 = date(2026, 10, 19)
    enabled = {r.id: r for r in regimens.regimens().values() if r.enabled}
    assert set(enabled) == {"id_trc", "im_essen", "reexposure"}
    expect = {
        "id_trc": [0, 3, 7, 28],
        "im_essen": [0, 3, 7, 14, 28],
        "reexposure": [0, 3],
    }
    for rid, days in expect.items():
        sched = regimens.schedule(enabled[rid], day0)
        assert [s.offset_days for s in sched] == days
        assert [s.dose_no for s in sched] == list(range(1, len(days) + 1))
        assert [s.due_date for s in sched] == [day0 + timedelta(days=d) for d in days]


@pytest.mark.parametrize("rid", ["id_trc", "im_essen", "reexposure"])
def test_create_course_writes_the_whole_calendar(ctx, world, rid):
    cid = world.course(regimen=rid)
    course = enrol.get_course(ctx, cid)
    reg = regimens.get(rid)
    assert [d.offset_days for d in course.doses] == list(reg.days)
    assert course.status == "awaiting_call"
    # dose 1 is the visit that starts the course
    assert course.doses[0].status == "given"
    assert course.doses[0].marked_by == "clinic"
    assert all(d.status == "due" for d in course.doses[1:])


@pytest.mark.parametrize(
    ("days_late", "expected"),
    [(-1, "due"), (0, "due"), (1, "late_minor"), (2, "late_minor"), (3, "late_review")],
)
def test_late_states_by_days_past_due(ctx, world, days_late, expected):
    # dose 2 is due on day 3, so day0 = today - 3 - days_late puts it days_late past due
    cid = world.active(day0=world.today() - timedelta(days=3 + days_late))
    doses.refresh_states(ctx, cid)
    assert world.dose(cid, 2)["status"] == expected
    # the next dose is still in the future and the calendar did not move
    assert world.dose(cid, 3)["status"] == "due"
    assert (
        world.dose(cid, 3)["due_date"]
        == (world.today() - timedelta(days=3 + days_late) + timedelta(days=7)).isoformat()
    )


def test_refresh_counts_changes_once(ctx, world, events):
    cid = world.active(day0=world.today() - timedelta(days=5))
    events()
    assert doses.refresh_states(ctx, cid) == 1
    assert doses.refresh_states(ctx, cid) == 0
    assert [e for e in events() if e[0] == "dose"] == [
        ("dose", {"course_id": cid, "dose_no": 2, "status": "late_minor"})
    ]


def test_state_can_fall_back_to_due_when_the_date_is_earlier(ctx, world):
    cid = world.active(day0=world.today() - timedelta(days=5))
    doses.refresh_states(ctx, cid)
    assert world.dose(cid, 2)["status"] == "late_minor"
    doses.refresh_states(ctx, cid, today=world.today() - timedelta(days=3))
    assert world.dose(cid, 2)["status"] == "due"


def test_refresh_leaves_given_and_awaiting_courses_alone(ctx, world):
    waiting = world.course(day0=world.today() - timedelta(days=20))
    assert doses.refresh_states(ctx) == 0
    assert world.dose(waiting, 2)["status"] == "due"
    cid = world.active(day0=world.today() - timedelta(days=4))
    doses.mark_given(ctx, cid, 2, "clinic", world.today(), "staff:test")
    doses.refresh_states(ctx, cid)
    assert world.dose(cid, 2)["status"] == "given"


def test_too_few_doses_by_day_28_sends_the_last_dose_to_review(ctx, world):
    # id_trc, day 29: doses 1 and 2 given, 3 missed, 4 only one day past due
    cid = world.active(day0=world.today() - timedelta(days=29))
    doses.mark_given(ctx, cid, 2, "clinic", world.dose_date(cid, 2), "staff:test")
    doses.refresh_states(ctx, cid)
    assert world.dose(cid, 3)["status"] == "late_review"
    assert world.dose(cid, 4)["status"] == "late_review"  # by days alone it would be late_minor


def test_all_but_last_given_by_day_28_keeps_the_last_dose_minor(ctx, world):
    cid = world.active(day0=world.today() - timedelta(days=29))
    for n in (2, 3):
        doses.mark_given(ctx, cid, n, "clinic", world.dose_date(cid, n), "staff:test")
    doses.refresh_states(ctx, cid)
    assert world.dose(cid, 4)["status"] == "late_minor"


def test_fewer_than_three_doses_by_day_10_flags_overdue_doses(ctx, world):
    cid = world.active(regimen="im_essen", day0=world.today() - timedelta(days=11))
    doses.refresh_states(ctx, cid)
    assert [world.dose(cid, n)["status"] for n in (2, 3, 4, 5)] == [
        "late_review",
        "late_review",
        "due",
        "due",
    ]


def test_mark_given_by_clinic_and_by_patient(ctx, world, events):
    cid = world.active(day0=world.today() - timedelta(days=3))
    events()
    out = doses.mark_given(ctx, cid, 2, "clinic", world.today(), "staff:asha")
    assert (out.status, out.marked_by, out.given_on) == ("given", "clinic", world.today().isoformat())
    assert ("dose", {"course_id": cid, "dose_no": 2, "status": "given"}) in events()
    assert "dose.given" in world.audit_actions()
    row = ctx.db.one("SELECT actor, detail_json FROM audit WHERE action='dose.given'")
    assert row["actor"] == "staff:asha"

    cid2 = world.active(day0=world.today() - timedelta(days=4))
    out2 = doses.mark_given(ctx, cid2, 2, "patient", world.today() - timedelta(days=1), f"patient:{cid2}")
    assert (out2.marked_by, out2.given_on) == ("patient", (world.today() - timedelta(days=1)).isoformat())


def test_mark_given_twice_changes_nothing(ctx, world):
    cid = world.active(day0=world.today() - timedelta(days=3))
    doses.mark_given(ctx, cid, 2, "clinic", world.today(), "staff:a")
    before = ctx.db.one("SELECT COUNT(*) FROM audit")[0]
    again = doses.mark_given(ctx, cid, 2, "patient", world.today(), "patient:x")
    assert again.marked_by == "clinic"
    assert ctx.db.one("SELECT COUNT(*) FROM audit")[0] == before


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("future_dose", "dose_in_future"),
        ("future_date", "date_in_future"),
        ("before_day0", "bad_date"),
        ("awaiting", "course_not_active"),
        ("stopped", "course_not_active"),
        ("no_such_dose", "not_found"),
    ],
)
def test_mark_given_refusals(ctx, world, setup, code):
    day0 = world.today() - timedelta(days=3)
    if setup == "future_dose":
        cid, n, on = world.active(day0=world.today()), 2, world.today()
    elif setup == "future_date":
        cid, n, on = world.active(day0=day0), 2, world.today() + timedelta(days=1)
    elif setup == "before_day0":
        cid, n, on = world.active(day0=day0), 2, day0 - timedelta(days=1)
    elif setup == "awaiting":
        cid, n, on = world.course(day0=day0), 2, world.today()
    elif setup == "stopped":
        cid = world.active(day0=day0)
        enrol.stop(ctx, cid, "moved away", "staff:a")
        n, on = 2, world.today()
    else:
        cid, n, on = world.active(day0=day0), 9, world.today()
    with pytest.raises(DoseError) as e:
        doses.mark_given(ctx, cid, n, "clinic", on, "staff:a")
    assert e.value.code == code
    assert isinstance(e.value, ValueError)


def test_unknown_course_is_refused(ctx):
    with pytest.raises(DoseError) as e:
        doses.mark_given(ctx, "crs_missing", 1, "clinic", date(2026, 10, 19), "staff:a")
    assert e.value.code == "not_found"


def test_last_dose_completes_the_course(ctx, world, events):
    cid = world.active(regimen="reexposure", day0=world.today() - timedelta(days=3))
    events()
    doses.mark_given(ctx, cid, 2, "clinic", world.today(), "staff:a")
    course = enrol.get_course(ctx, cid)
    assert course.status == "completed"
    assert ("course", {"course_id": cid, "status": "completed"}) in events()
    assert ctx.db.one("SELECT completed_at FROM courses WHERE id=?", (cid,))["completed_at"]


def test_marking_a_dose_cancels_its_waiting_calls(ctx, world):
    cid = world.active(day0=world.today() - timedelta(days=3))
    with ctx.db.transaction() as conn:
        for kind in ("reminder", "missed_dose"):
            enrol.new_call(
                conn,
                course_id=cid,
                kind=kind,
                job_key=f"{kind}:{cid}:2:x",
                scheduled_for=clock.now(),
                dose_no=2,
            )
    doses.mark_given(ctx, cid, 2, "clinic", world.today(), "staff:a")
    rows = world.calls(cid)
    assert {r["kind"]: r["status"] for r in rows if r["dose_no"] == 2} == {
        "reminder": "cancelled",
        "missed_dose": "cancelled",
    }
    # the first call (no dose) is untouched
    assert [r["status"] for r in rows if r["kind"] == "first"] == ["scheduled"]


def test_next_due_is_the_first_dose_not_given(ctx, world):
    cid = world.active(day0=world.today() - timedelta(days=4))
    assert doses.next_due(ctx, cid).dose_no == 2  # even though it is already late
    doses.mark_given(ctx, cid, 2, "clinic", world.today(), "staff:a")
    assert doses.next_due(ctx, cid).dose_no == 3
    done = world.active(regimen="reexposure", day0=world.today() - timedelta(days=3))
    doses.mark_given(ctx, done, 2, "clinic", world.today(), "staff:a")
    assert doses.next_due(ctx, done) is None


def test_enrol_input_is_unchanged_by_dose_work(ctx, world):
    assert isinstance(world.data(), EnrolIn)
