"""The alert state machine, who may do what, and the call-back request."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from chaarteeke import alerts, clock
from chaarteeke.ids import new_id

VET1, VET2, CLINIC_T, CLINIC_U, ABC = (
    "staff:vet1",
    "staff:vet2",
    "staff:clinic_t",
    "staff:clinic_u",
    "staff:abc",
)


@pytest.fixture
def world(sctx, add_reports):
    with sctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO clinics(id, name, short_name, default_regimen, default_lang, arv_status, is_demo) "
            "VALUES ('cln_u','Other clinic','Other','id_trc','hi','confirmed',1)"
        )
        for username, role, clinic in (
            ("vet1", "vet", None),
            ("vet2", "vet", None),
            ("clinic_t", "clinic", "cln_t"),
            ("clinic_u", "clinic", "cln_u"),
            ("abc", "abc", None),
        ):
            conn.execute(
                "INSERT INTO staff(id, username, display_name, role, clinic_id, password_hash, is_demo) "
                "VALUES (?,?,?,?,?,'x',1)",
                (new_id("stf"), username, username, role, clinic),
            )
        conn.execute(
            "INSERT INTO courses(id, clinic_id, register_no, regimen_id, day0, lang, status, eligibility, source, created_at, is_demo)"
            " VALUES ('crs_1','cln_t','S1','id_trc','2026-10-18','hi','active','adult','demo','2026-10-18T05:00:00.000Z',1)"
        )
    ward = sctx.clusters.all()[2].ward_nos[0]
    made = iter(range(2, 1000))
    first = ["crs_1"]

    def report(**kw):
        """One bite report; each gets its own course (a course has at most one report)."""
        if "course_id" not in kw:
            if first:
                kw["course_id"] = first.pop()
            else:
                n = next(made)
                with sctx.db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO courses(id, clinic_id, register_no, regimen_id, day0, lang, status, eligibility, source, created_at, is_demo)"
                        " VALUES (?,'cln_t',?,'id_trc','2026-10-18','hi','active','adult','demo','2026-10-18T05:00:00.000Z',1)",
                        (f"crs_{n}", f"S{n}"),
                    )
                kw["course_id"] = f"crs_{n}"
        (rid,) = add_reports(ward, "unnotched", signs=kw.pop("signs", "yes"), **kw)
        return rid

    def alert(**kw):
        rid = report(**kw)
        with sctx.db.transaction() as conn:
            return alerts.from_report(sctx, conn, rid)

    sctx.ward = ward
    sctx.report, sctx.alert = report, alert
    return sctx


def test_only_signs_yes_makes_an_alert(world):
    no, unsure = world.report(signs="no"), world.report(signs="dont_know")
    with world.db.transaction() as conn:
        assert alerts.from_report(world, conn, no) is None
        assert alerts.from_report(world, conn, unsure) is None
    assert world.db.one("SELECT COUNT(*) AS n FROM alerts")["n"] == 0
    assert world.alert() is not None


def test_alert_carries_ward_cluster_place_type_and_local_day_only(world):
    # 20:00 UTC on the 18th is 01:30 on the 19th in Jammu: the alert's day is the local one
    alert_id = world.alert(place_type="school", created_at="2026-10-18T20:00:00.000Z")
    a = alerts.get(world, alert_id)
    assert (a.ward_no, a.cluster_id, a.place_type, a.local_day, a.status) == (
        world.ward,
        world.clusters.for_ward(world.ward),
        "school",
        "2026-10-19",
        "new",
    )
    assert a.ward_name and a.claimed_by is None and a.callback_status is None and a.simulated is False
    row = dict(world.db.one("SELECT * FROM alerts WHERE id = ?", (alert_id,)))
    assert not {"course_id", "phone", "phone_cipher", "call_time"} & set(row)
    assert "crs_1" not in str(row)


def test_from_report_is_idempotent_and_publishes_one_event(world):
    queue = world.bus.subscribe()
    rid = world.report()
    with world.db.transaction() as conn:
        first = alerts.from_report(world, conn, rid)
        again = alerts.from_report(world, conn, rid)
    assert first == again
    ev = queue.get_nowait()
    assert ev.type == "alert" and ev.data["alert_id"] == first and ev.data["status"] == "new"
    assert set(ev.data) == {"alert_id", "status", "cluster_id", "ward_no"}
    assert queue.empty()


def test_unknown_report_is_refused(world):
    with world.db.transaction() as conn, pytest.raises(alerts.AlertError) as e:
        alerts.from_report(world, conn, "rep_nope")
    assert e.value.code == "not_found"


def test_full_path_new_claimed_acted(world):
    aid = world.alert()
    a = alerts.claim(world, aid, VET1)
    assert a.status == "claimed" and a.claimed_by == VET1 and a.claimed_at
    assert alerts.claim(world, aid, VET1).claimed_at == a.claimed_at  # a second tap changes nothing
    done = alerts.act(world, aid, VET1, "Team caught the dog")
    assert done.status == "acted" and done.outcome_note == "Team caught the dog"
    row = world.db.one("SELECT * FROM alerts WHERE id = ?", (aid,))
    assert row["closed_at"] and row["first_seen_by_vet_at"]


def test_a_second_vet_cannot_take_or_close_a_claimed_alert(world):
    aid = world.alert()
    alerts.claim(world, aid, VET1)
    with pytest.raises(alerts.AlertError) as e:
        alerts.claim(world, aid, VET2)
    assert e.value.code == "already_claimed"
    with pytest.raises(alerts.AlertError) as e:
        alerts.act(world, aid, VET2)
    assert e.value.code == "not_yours"
    assert alerts.get(world, aid).claimed_by == VET1


def test_closing_needs_a_claim_first(world):
    aid = world.alert()
    for fn in (alerts.act, alerts.not_act):
        with pytest.raises(alerts.AlertError) as e:
            fn(world, aid, VET1, "because")
        assert e.value.code == "not_claimed"
    assert alerts.get(world, aid).status == "new"


def test_not_acting_needs_a_reason(world):
    aid = world.alert()
    alerts.claim(world, aid, VET1)
    for note in (None, "", "   "):
        with pytest.raises(alerts.AlertError) as e:
            alerts.not_act(world, aid, VET1, note)
        assert e.value.code == "note_needed"
    closed = alerts.not_act(world, aid, VET1, "Could not trace the dog")
    assert closed.status == "not_acted" and closed.outcome_note == "Could not trace the dog"


def test_closed_alerts_stay_closed(world):
    aid = world.alert()
    alerts.claim(world, aid, VET1)
    alerts.act(world, aid, VET1)
    for call in (
        lambda: alerts.claim(world, aid, VET1),
        lambda: alerts.act(world, aid, VET1),
        lambda: alerts.not_act(world, aid, VET1, "x"),
    ):
        with pytest.raises(alerts.AlertError) as e:
            call()
        assert e.value.code == "already_closed"


@pytest.mark.parametrize(
    "actor", [CLINIC_T, ABC, "staff:nobody", "visitor:3", "system:demo", "patient:crs_1"]
)
def test_only_a_vet_may_work_an_alert(world, actor):
    aid = world.alert()
    with pytest.raises(alerts.AlertError) as e:
        alerts.claim(world, aid, actor)
    assert e.value.code in ("forbidden", "read_only")
    assert alerts.get(world, aid).status == "new"
    assert actor != "visitor:3" or e.value.code == "read_only"


def test_unknown_alert(world):
    with pytest.raises(alerts.AlertError) as e:
        alerts.claim(world, "alr_nope", VET1)
    assert e.value.code == "not_found"


def test_list_is_newest_first_and_filters_by_status(world):
    first = world.alert(created_at="2026-10-18T05:00:00.000Z")
    second = world.alert(created_at="2026-10-19T03:00:00.000Z")
    with world.db.transaction() as conn:
        conn.execute("UPDATE alerts SET created_at = '2026-10-18T05:00:00.000Z' WHERE id = ?", (first,))
        conn.execute("UPDATE alerts SET created_at = '2026-10-19T03:00:00.000Z' WHERE id = ?", (second,))
    alerts.claim(world, first, VET1)
    assert [a.id for a in alerts.list(world, None)] == [second, first]
    assert [a.id for a in alerts.list(world, "claimed")] == [first]
    assert [a.id for a in alerts.list(world, "new")] == [second]
    assert alerts.list is alerts.list_alerts
    with pytest.raises(alerts.AlertError) as e:
        alerts.list(world, "open")
    assert e.value.code == "bad_status"


def test_simulated_alerts_are_marked(world):
    aid = world.alert(source="simulator")
    assert alerts.get(world, aid).simulated is True


def test_unseen_alerts_are_reported_after_the_policy_time(world):
    aid = world.alert(created_at=clock.iso(clock.now()))
    assert alerts.unseen(world) == []
    world_clock = clock.current()
    world_clock.advance(timedelta(minutes=world.policy["alerts"]["unseen_by_vet_minutes"] - 1))
    assert alerts.unseen(world) == []
    world_clock.advance(timedelta(minutes=2))
    assert [a.id for a in alerts.unseen(world)] == [aid]


def test_marking_seen_clears_unseen_once(world):
    world.alert(created_at=clock.iso(clock.now()))
    clock.current().advance(timedelta(hours=1))
    assert alerts.mark_seen(world, VET1) == 1
    assert alerts.unseen(world) == []
    assert alerts.mark_seen(world, VET2) == 0
    first_seen = world.db.one("SELECT first_seen_by_vet_at FROM alerts")["first_seen_by_vet_at"]
    assert first_seen is not None
    with pytest.raises(alerts.AlertError):
        alerts.mark_seen(world, CLINIC_T)


def test_claiming_counts_as_seeing(world):
    aid = world.alert(created_at=clock.iso(clock.now()))
    clock.current().advance(timedelta(hours=1))
    alerts.claim(world, aid, VET1)
    assert alerts.unseen(world) == []


def test_a_claimed_alert_is_never_reported_unseen(world):
    aid = world.alert(created_at=clock.iso(clock.now()))
    alerts.claim(world, aid, VET1)
    with world.db.transaction() as conn:
        conn.execute("UPDATE alerts SET first_seen_by_vet_at = NULL")
    clock.current().advance(timedelta(hours=2))
    assert alerts.unseen(world) == []  # someone is on it


def test_callback_request_reaches_the_clinic_and_hides_the_patient_from_the_vet(world):
    aid = world.alert()
    alerts.claim(world, aid, VET1)
    cb = alerts.request_callback(world, aid, VET1)
    assert cb.status == "open" and cb.course_id is None and cb.register_no is None
    assert cb.ward_no == world.ward
    assert alerts.request_callback(world, aid, VET1).id == cb.id  # one open request per alert
    assert alerts.get(world, aid).callback_status == "open"
    mine = alerts.callbacks_for_clinic(world, "cln_t")
    assert [(c.id, c.course_id, c.register_no) for c in mine] == [(cb.id, "crs_1", "S1")]
    assert alerts.callbacks_for_clinic(world, "cln_u") == []


def test_callback_needs_a_claim_by_the_same_vet_and_a_patient_record(world):
    aid = world.alert()
    with pytest.raises(alerts.AlertError) as e:
        alerts.request_callback(world, aid, VET1)
    assert e.value.code == "not_claimed"
    alerts.claim(world, aid, VET1)
    with pytest.raises(alerts.AlertError) as e:
        alerts.request_callback(world, aid, VET2)
    assert e.value.code == "not_claimed"
    orphan = world.alert(course_id=None)
    alerts.claim(world, orphan, VET1)
    with pytest.raises(alerts.AlertError) as e:
        alerts.request_callback(world, orphan, VET1)
    assert e.value.code == "no_course"


def test_only_the_patients_clinic_closes_the_callback(world):
    aid = world.alert()
    alerts.claim(world, aid, VET1)
    cb = alerts.request_callback(world, aid, VET1)
    for actor in (CLINIC_U, VET1, ABC):
        with pytest.raises(alerts.AlertError) as e:
            alerts.callback_done(world, cb.id, actor, "rang")
        assert e.value.code == "forbidden"
    done = alerts.callback_done(world, cb.id, CLINIC_T, " Spoke to the patient ")
    assert done.status == "done"
    row = world.db.one("SELECT * FROM callback_requests WHERE id = ?", (cb.id,))
    assert row["done_at"] and row["note"] == "Spoke to the patient"
    assert alerts.get(world, aid).callback_status == "done"
    with pytest.raises(alerts.AlertError) as e:
        alerts.callback_done(world, cb.id, CLINIC_T)
    assert e.value.code == "already_closed"
    assert alerts.callbacks_for_clinic(world, "cln_t") == []


def test_callback_can_be_declined_or_closed_with_no_contact_but_not_given_another_outcome(world):
    aid = world.alert()
    alerts.claim(world, aid, VET1)
    cb = alerts.request_callback(world, aid, VET1)
    with pytest.raises(alerts.AlertError):
        alerts.callback_done(world, cb.id, CLINIC_T, outcome="maybe")
    assert (
        alerts.callback_done(world, cb.id, CLINIC_T, "Number no longer held", outcome="declined").status
        == "declined"
    )


def test_a_callback_closed_with_no_contact_is_recorded_and_published(world):
    aid = world.alert()
    alerts.claim(world, aid, VET1)
    cb = alerts.request_callback(world, aid, VET1)
    queue = world.bus.subscribe()
    closed = alerts.callback_done(world, cb.id, CLINIC_T, "Phone off twice", outcome="closed_no_contact")
    assert closed.status == "closed_no_contact"
    assert alerts.get(world, aid).callback_status == "closed_no_contact"
    assert alerts.callbacks_for_clinic(world, "cln_t") == []
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    callback = [e for e in events if e.type == "callback"]
    assert [e.data for e in callback] == [
        {"callback_id": cb.id, "alert_id": aid, "status": "closed_no_contact"}
    ]


def test_a_callback_request_is_published_once_even_if_asked_twice(world):
    aid = world.alert()
    alerts.claim(world, aid, VET1)
    queue = world.bus.subscribe()
    first = alerts.request_callback(world, aid, VET1)
    alerts.request_callback(world, aid, VET1)
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    assert [e.data for e in seen if e.type == "callback"] == [
        {"callback_id": first.id, "alert_id": aid, "status": "open"}
    ]


def test_claimed_by_me_is_true_only_for_the_vet_who_took_it(world):
    aid = world.alert()
    assert alerts.get(world, aid, VET1).claimed_by_me is False
    alerts.claim(world, aid, VET1)
    assert alerts.get(world, aid, VET1).claimed_by_me is True
    assert alerts.get(world, aid, VET2).claimed_by_me is False
    assert alerts.get(world, aid).claimed_by_me is False
    assert [a.claimed_by_me for a in alerts.list_alerts(world, None, VET1)] == [True]
    assert [a.claimed_by_me for a in alerts.list_alerts(world, None, ABC)] == [False]
    assert alerts.claim(world, aid, VET1).claimed_by_me is True


def test_every_transition_is_audited_and_published(world):
    queue = world.bus.subscribe()
    aid = world.alert()
    alerts.claim(world, aid, VET1)
    alerts.request_callback(world, aid, VET1)
    alerts.not_act(world, aid, VET1, "No dog found")
    actions = [r["action"] for r in world.db.query("SELECT action FROM audit ORDER BY seq")]
    assert actions == ["alert.create", "alert.claimed", "callback.request", "alert.not_acted"]
    statuses = []
    while not queue.empty():
        statuses.append(queue.get_nowait().data["status"])
    assert statuses == ["new", "claimed", "claimed", "open", "not_acted"]
    assert (
        "No dog found"
        in world.db.one("SELECT detail_json FROM audit WHERE action = 'alert.not_acted'")["detail_json"]
    )


def test_a_later_ward_decision_reaches_the_alert(world, add_reports):
    (rid,) = add_reports(None, "unnotched", signs="yes", course_id="crs_1", location_status="needs_review")
    with world.db.transaction() as conn:
        aid = alerts.from_report(world, conn, rid)
    assert alerts.get(world, aid).ward_no is None
    with world.db.transaction() as conn:
        conn.execute(
            "UPDATE bite_reports SET ward_no = ?, location_status = 'decided' WHERE id = ?", (world.ward, rid)
        )
        alerts.update_location(world, conn, rid)
    a = alerts.get(world, aid)
    assert a.ward_no == world.ward and a.cluster_id == world.clusters.for_ward(world.ward)


# ------------------------------------------------ no time of day leaves the server (T10)
TIME_OF_DAY = re.compile(r"\d{1,2}:\d{2}")


def _strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def test_an_alert_is_served_with_days_never_a_time_of_day(world):
    aid = world.alert(created_at="2026-10-18T20:15:42.000Z")  # 01:45 on the 19th in Jammu
    alerts.claim(world, aid, VET1)
    for served in (alerts.get(world, aid), *alerts.list(world), *alerts.list(world, "claimed", viewer=VET1)):
        body = served.model_dump()
        assert not any(TIME_OF_DAY.search(s) for s in _strings(body)), body
        assert body["created_at"] == body["local_day"] == "2026-10-19"
        if body["claimed_at"]:
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", body["claimed_at"])
    # the exact times stay in the table for the unseen check and the audit trail
    row = world.db.one("SELECT created_at, claimed_at FROM alerts")
    assert TIME_OF_DAY.search(row["created_at"]) and TIME_OF_DAY.search(row["claimed_at"])


def test_a_visitors_own_alert_is_never_a_fault_in_demo_mode(world):
    mine = world.alert(source="simulator", created_at=clock.iso(clock.now()))
    history = world.alert(created_at=clock.iso(clock.now()))
    clock.current().advance(timedelta(hours=3))  # a demo clock jump
    assert [a.id for a in alerts.unseen(world)] == [history]
    assert alerts.get(world, mine).simulated


def test_in_pilot_mode_every_unseen_alert_is_a_fault(world):
    world.settings = world.settings.model_copy(update={"mode": "pilot"})
    aid = world.alert(created_at=clock.iso(clock.now()))
    clock.current().advance(timedelta(hours=3))
    assert [a.id for a in alerts.unseen(world)] == [aid]
