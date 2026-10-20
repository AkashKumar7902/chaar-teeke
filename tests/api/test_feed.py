"""GET /api/feed: what the landing page's "Latest" list shows after a reload."""

from __future__ import annotations

import json
from datetime import timedelta

from chaarteeke import alerts, clock, enrol
from chaarteeke.ids import new_id

ITEM_KEYS = {
    "kind",
    "id",
    "at",
    "day",
    "cluster_id",
    "ward_no",
    "ward_name",
    "animal",
    "location_status",
    "simulated",
}


def add_report(
    ctx, ward_no, *, source="simulator", signs="no", animal="unnotched", status="matched", age=timedelta(0)
):
    """A bite report (and, when the dog showed signs, its alert) made `age` ago on the test clock."""
    report_id = new_id("rep")
    made = clock.iso(clock.now() - age)
    cluster = ctx.clusters.for_ward(ward_no)
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO bite_reports(id, clinic_id, bite_week, place_type, animal, signs, ward_no, location_status,"
            " cluster_id, source, created_at, is_demo) VALUES (?, 'cln_demo_visitor', '2026-10-12', 'home_street', ?, ?, ?,"
            " ?, ?, ?, ?, 1)",
            (report_id, animal, signs, ward_no, status, cluster, source, made),
        )
        alert_id = alerts.from_report(ctx, conn, report_id)
        if alert_id:  # an alert is made now; give it the report's time
            conn.execute("UPDATE alerts SET created_at = ? WHERE id = ?", (made, alert_id))
    return report_id, alert_id


def feed(client, who):
    res = client.get("/api/feed", headers=who.read)
    assert res.status_code == 200, res.text
    return res.json()


def test_the_feed_needs_a_signed_in_view(client, people):
    res = client.get("/api/feed", headers=people["out"].read)
    assert (res.status_code, res.json()["error"]) == (401, "signed_out")


def test_every_signed_in_view_reads_it_a_visitor_included(client, people):
    for name in people:
        if name != "out":
            assert client.get("/api/feed", headers=people[name].read).status_code == 200, name


def test_the_feed_of_a_fresh_demo_is_the_seeded_history_and_marks_what_is_simulated(client, people, ctx):
    rows = feed(client, people["visitor-abc"])
    assert rows and {r["kind"] for r in rows} <= {
        "alert",
        "missed_call",
    }  # the seeded desk has no reports of its own
    assert all(set(r) == ITEM_KEYS for r in rows)
    assert len(rows) <= ctx.policy["feed"]["max_items"]
    real = [r for r in rows if not r["simulated"]]
    assert real and len(real) <= ctx.policy["feed"]["max_real_alerts"]
    for r in real:
        assert r["kind"] == "alert"
        assert r["at"] is None  # a real alert is served with its day only, never the second it was made
        assert r["animal"] is None  # no answer of a real report is ever sent
    assert all(r["at"] for r in rows if r["simulated"])
    order = [(r["day"], r["at"] or "") for r in rows]
    assert order == sorted(order, reverse=True)  # newest first


def test_the_visitors_own_phone_shows_as_missed_call_report_and_alert_with_their_facts(
    client, people, ctx, wards
):
    missed = enrol.record_missed_call(ctx, "sim:visitor-phone", "sim", clock.now() - timedelta(minutes=5))
    ward = wards[0]
    report_id, alert_id = add_report(ctx, ward, signs="yes", animal="notched", age=timedelta(minutes=2))
    rows = feed(client, people["visitor-abc"])
    by_id = {r["id"]: r for r in rows}
    assert {missed.missed_call_id, report_id, alert_id} <= set(by_id)
    assert not [r for r in rows if r["kind"] == "call"]  # the first call is only planned: no call yet
    report = by_id[report_id]
    assert (
        report["kind"] == "report"
        and report["animal"] == "notched"
        and report["location_status"] == "matched"
    )
    assert (
        report["ward_no"] == ward
        and report["ward_name"]
        and report["cluster_id"] == ctx.clusters.for_ward(ward)
    )
    assert report["at"] and report["day"] == clock.local_today(ctx.settings.local_tz).isoformat()
    alert = by_id[alert_id]
    assert (
        alert["kind"] == "alert"
        and alert["simulated"]
        and alert["at"] == report["at"]
        and alert["animal"] is None
    )
    assert alert["ward_no"] == ward and alert["location_status"] == "matched"
    assert by_id[missed.missed_call_id]["kind"] == "missed_call" and by_id[missed.missed_call_id]["at"]
    assert {r["id"] for r in rows[:2]} == {report_id, alert_id}  # the newest rows come first
    assert rows[2]["id"] == missed.missed_call_id


def test_a_simulated_call_that_has_started_is_listed_and_one_that_is_only_planned_is_not(client, people, ctx):
    got = enrol.record_missed_call(ctx, "sim:visitor-phone", "sim", clock.now())
    call = ctx.db.one("SELECT id FROM calls WHERE course_id = ?", (got.course_id,))["id"]
    assert [r for r in feed(client, people["visitor-abc"]) if r["kind"] == "call"] == []
    with ctx.db.transaction() as conn:
        conn.execute(
            "UPDATE calls SET status = 'completed', started_at = ? WHERE id = ?",
            (clock.iso(clock.now()), call),
        )
    calls = [r for r in feed(client, people["visitor-abc"]) if r["kind"] == "call"]
    assert [c["id"] for c in calls] == [call] and calls[0]["simulated"] is True


def test_no_real_call_missed_call_or_report_is_listed_and_no_number_or_course_is(client, people, ctx, wards):
    real = enrol.record_missed_call(ctx, "+919999900123", "ari", clock.now())
    add_report(ctx, wards[0], source="clinic", animal="unnotched")
    sim = enrol.record_missed_call(ctx, "sim:visitor-phone", "sim", clock.now())
    for name in ("visitor-abc", "abc", "vet", "admin"):
        text = json.dumps(feed(client, people[name]))
        assert real.missed_call_id not in text and "9999900123" not in text, name
        assert "+91" not in text and "sim:" not in text and sim.course_id not in text, name
        assert "crs_" not in text and "course" not in text, name
        assert "unnotched" not in text, name  # a real report's answer is not sent


def test_what_a_view_may_see_of_real_alerts_follows_the_alert_rules(client, people, ctx):
    assert any(r["kind"] == "alert" for r in feed(client, people["abc"]))
    assert any(r["kind"] == "alert" for r in feed(client, people["visitor-abc"]))
    for name in ("oversight", "reviewer", "clinic", "visitor-oversight", "visitor-reviewer"):
        assert [r for r in feed(client, people[name]) if r["kind"] == "alert"] == [], name


def test_a_view_that_cannot_read_alerts_still_sees_the_visitors_own_phone(client, people, ctx):
    enrol.record_missed_call(ctx, "sim:visitor-phone", "sim", clock.now())
    for name in ("oversight", "clinic", "visitor-reviewer"):
        assert {r["kind"] for r in feed(client, people[name])} == {"missed_call"}, name


def test_the_list_is_newest_first_and_capped_by_the_policy(client, people, ctx, wards):
    ctx.policy.raw["feed"].update({"max_items": 4, "max_real_alerts": 2})
    for minutes in (50, 40, 30, 20, 10):
        add_report(ctx, wards[0], signs="yes", age=timedelta(minutes=minutes))
    rows = feed(client, people["visitor-abc"])
    assert len(rows) == 4
    stamps = [r["at"] for r in rows if r["at"]]
    assert stamps == sorted(stamps, reverse=True)
    real = [r for r in rows if not r["simulated"]]
    assert len(real) <= 2  # the real history never fills the list


def test_real_alerts_never_push_the_visitors_phone_out_of_the_list(client, people, ctx, wards):
    ctx.policy.raw["feed"].update({"max_items": 3, "max_real_alerts": 3})
    enrol.record_missed_call(ctx, "sim:visitor-phone", "sim", clock.now())
    rows = feed(client, people["visitor-abc"])
    assert "missed_call" in {r["kind"] for r in rows}
