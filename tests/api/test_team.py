"""Coverage, areas, ward counts, sick-dog alerts and the team's field records over HTTP."""

from __future__ import annotations

from datetime import timedelta

from chaarteeke import clock


def test_coverage_has_the_city_the_areas_and_the_band(client, people, ctx):
    res = client.get("/api/coverage", headers=people["abc"].read)
    assert res.status_code == 200
    body = res.json()
    assert len(body["clusters"]) == len(ctx.clusters.all())
    assert body["city"]["status"] in ("ok", "bracketed", "too_few", "no_data")
    assert body["claim_band"] == [0.78, 0.8] and "equally" in body["claim_label"]
    assert body["is_demo"] is True and len(body["look_here"]) <= ctx.policy["stats"]["lookhere_max"]


def test_coverage_window_is_the_policy_window_or_refused(client, people, ctx):
    weeks = ctx.policy["stats"]["coverage_window_weeks"]
    assert client.get(f"/api/coverage?window={weeks}w", headers=people["abc"].read).status_code == 200
    res = client.get("/api/coverage?window=4w", headers=people["abc"].read)
    assert res.status_code == 400 and res.json()["error"] == "bad_request"


def test_a_cluster_and_its_wards(client, people, ctx):
    cluster = ctx.clusters.all()[0]
    res = client.get(f"/api/clusters/{cluster.id}", headers=people["abc"].read)
    assert res.status_code == 200 and res.json()["id"] == cluster.id
    assert {w["ward_no"] for w in res.json()["wards"]} == set(cluster.ward_nos)
    missing = client.get("/api/clusters/nowhere", headers=people["abc"].read)
    assert missing.status_code == 404 and missing.json()["error"] == "unknown_cluster"


def test_ward_counts_hold_counts_only(client, people, wards):
    rows = client.get("/api/wards", headers=people["abc"].read).json()
    assert sorted(r["ward_no"] for r in rows) == sorted(wards)
    assert set(rows[0]) == {"ward_no", "ward_name", "cluster_id", "bites", "school", "hospital", "bus_market", "alerts", "worked_on"}  # fmt: skip
    assert all(isinstance(r["bites"], int) or r["bites"] in ("<5", "hidden") for r in rows)


def test_alerts_carry_ward_day_place_type_and_nothing_about_the_patient(client, people):
    rows = client.get("/api/alerts", headers=people["abc"].read).json()
    assert rows
    assert set(rows[0]) == {"id", "ward_no", "ward_name", "cluster_id", "place_type", "local_day", "status", "claimed_by", "claimed_at", "outcome_note", "created_at", "made_at", "location_status", "callback_status", "simulated", "claimed_by_me"}  # fmt: skip
    new_only = client.get("/api/alerts?status=new", headers=people["abc"].read).json()
    assert new_only and {a["status"] for a in new_only} == {"new"}
    bad = client.get("/api/alerts?status=lost", headers=people["abc"].read)
    assert bad.status_code == 400 and bad.json()["error"] == "bad_status"


def test_opening_the_list_as_a_vet_marks_alerts_seen_and_as_anyone_else_does_not(client, ctx, people):
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE alerts SET first_seen_by_vet_at = NULL")
    client.get("/api/alerts", headers=people["abc"].read)
    assert ctx.db.one("SELECT COUNT(*) AS n FROM alerts WHERE first_seen_by_vet_at IS NOT NULL")["n"] == 0
    client.get("/api/alerts", headers=people["visitor-abc"].read)
    assert ctx.db.one("SELECT COUNT(*) AS n FROM alerts WHERE first_seen_by_vet_at IS NOT NULL")["n"] == 0
    client.get("/api/alerts", headers=people["vet"].read)
    assert ctx.db.one("SELECT COUNT(*) AS n FROM alerts WHERE first_seen_by_vet_at IS NULL")["n"] == 0
    assert (
        ctx.db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'alert.seen' AND actor = 'staff:vet'")["n"]
        == 1
    )


def test_a_link_on_another_site_cannot_mark_alerts_seen_for_a_vet(client, ctx, people):
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE alerts SET first_seen_by_vet_at = NULL")
    for site in ("cross-site", "same-site"):
        res = client.get("/api/alerts", headers={**people["vet"].read, "sec-fetch-site": site})
        assert res.status_code == 200
    assert ctx.db.one("SELECT COUNT(*) AS n FROM alerts WHERE first_seen_by_vet_at IS NOT NULL")["n"] == 0
    client.get("/api/alerts", headers={**people["vet"].read, "sec-fetch-site": "same-origin"})
    assert ctx.db.one("SELECT COUNT(*) AS n FROM alerts WHERE first_seen_by_vet_at IS NULL")["n"] == 0


def new_alert(client, people):
    return client.get("/api/alerts?status=new", headers=people["vet"].read).json()[0]["id"]


def test_a_vet_claims_and_closes_an_alert(client, people):
    vet = people["vet"].write
    alert = new_alert(client, people)
    claimed = client.post(f"/api/alerts/{alert}/claim", json={}, headers=vet)
    assert claimed.status_code == 200 and claimed.json()["status"] == "claimed"
    assert claimed.json()["claimed_by"] == "staff:vet"
    closed = client.post(f"/api/alerts/{alert}/acted", json={"note": "Team caught the dog"}, headers=vet)
    assert closed.status_code == 200 and closed.json()["status"] == "acted"
    assert closed.json()["outcome_note"] == "Team caught the dog"
    again = client.post(f"/api/alerts/{alert}/acted", json={}, headers=vet)
    assert again.status_code == 409 and again.json()["error"] == "already_closed"


def test_not_acting_needs_a_reason_and_closing_needs_a_claim(client, people):
    vet = people["vet"].write
    alert = new_alert(client, people)
    early = client.post(f"/api/alerts/{alert}/acted", json={}, headers=vet)
    assert early.status_code == 409 and early.json()["error"] == "not_claimed"
    client.post(f"/api/alerts/{alert}/claim", headers=vet)
    no_reason = client.post(f"/api/alerts/{alert}/not-acted", json={}, headers=vet)
    assert no_reason.status_code == 400 and no_reason.json()["error"] == "note_needed"
    done = client.post(
        f"/api/alerts/{alert}/not-acted", json={"note": "Place too vague to search"}, headers=vet
    )
    assert done.status_code == 200 and done.json()["status"] == "not_acted"


def test_the_sterilisation_team_and_visitors_cannot_claim(client, people):
    alert = new_alert(client, people)
    for name in ("abc", "visitor-abc", "admin", "clinic"):
        res = client.post(f"/api/alerts/{alert}/claim", json={}, headers=people[name].write)
        assert res.status_code == 403, name
    assert client.get("/api/alerts?status=new", headers=people["vet"].read).json()[0]["status"] == "new"


def test_a_second_vet_cannot_take_a_claimed_alert(client, ctx, people):
    from chaarteeke.api import auth

    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO staff(id, username, display_name, role, password_hash, active, is_demo)"
            " VALUES ('stf_v2','vet2','Second vet','vet',?,1,1)",
            (auth.hash_password("a long password"),),
        )
    token, csrf = auth.sign_in(ctx, "vet2", "a long password")
    alert = new_alert(client, people)
    client.post(f"/api/alerts/{alert}/claim", headers=people["vet"].write)
    res = client.post(
        f"/api/alerts/{alert}/claim", headers={"cookie": f"ct_session={token}", "x-ct-csrf": csrf}
    )
    assert res.status_code == 409 and res.json()["error"] == "already_claimed"


def test_worked_wards_are_recorded_once_a_day(client, ctx, people, wards):
    abc = people["abc"]
    today = clock.local_today(ctx.settings.local_tz).isoformat()
    body = {"ward_nos": [wards[0], wards[1], wards[0]], "worked_on": today, "note": "  Camp  "}
    res = client.post("/api/worked", json=body, headers=abc.write)
    assert res.status_code == 200
    mine = [r for r in res.json() if r["worked_on"] == today and r["ward_no"] in (wards[0], wards[1])]
    assert sorted(r["ward_no"] for r in mine) == sorted([wards[0], wards[1]])
    again = client.post("/api/worked", json=body, headers=abc.write)
    assert len([r for r in again.json() if r["worked_on"] == today and r["ward_no"] == wards[0]]) == 1
    row = ctx.db.one("SELECT recorded_by, note, is_demo FROM worked_wards WHERE worked_on = ? AND ward_no = ?", (today, wards[0]))  # fmt: skip
    assert (row["recorded_by"], row["note"], row["is_demo"]) == ("staff:abc", "Camp", 1)
    assert ctx.db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'worked.record'")["n"] == 2


def test_worked_wards_refuse_a_future_day_a_bad_day_and_a_ward_that_is_not_there(client, ctx, people, wards):
    abc = people["abc"].write
    tomorrow = (clock.local_today(ctx.settings.local_tz) + timedelta(days=1)).isoformat()
    assert client.post("/api/worked", json={"ward_nos": [wards[0]], "worked_on": tomorrow}, headers=abc).json()["error"] == "date_in_future"  # fmt: skip
    assert client.post("/api/worked", json={"ward_nos": [wards[0]], "worked_on": "yesterday"}, headers=abc).json()["error"] == "bad_date"  # fmt: skip
    nowhere = client.post("/api/worked", json={"ward_nos": [9999], "worked_on": "2026-10-19"}, headers=abc)
    assert nowhere.status_code == 404 and nowhere.json()["error"] == "unknown_ward"
    empty = client.post("/api/worked", json={"ward_nos": [], "worked_on": "2026-10-19"}, headers=abc)
    assert empty.status_code == 400


def test_sight_counts_are_recorded_and_listed(client, ctx, people, wards):
    abc = people["abc"]
    body = {"ward_no": wards[0], "counted_on": "2026-10-18", "notched": 8, "unnotched": 2, "unsure": 1}
    res = client.post("/api/sight-counts", json=body, headers=abc.write)
    assert res.status_code == 200
    mine = [
        r
        for r in res.json()
        if r["ward_no"] == wards[0] and r["counted_on"] == "2026-10-18" and r["notched"] == 8
    ]
    assert mine and mine[0]["ward_name"]
    assert set(mine[0]) == {"id", "ward_no", "ward_name", "counted_on", "notched", "unnotched", "unsure"}
    assert client.get("/api/sight-counts", headers=people["vet"].read).status_code == 200


def test_sight_counts_refuse_negative_counts_and_unknown_wards(client, people, wards):
    abc = people["abc"].write
    body = {"ward_no": wards[0], "counted_on": "2026-10-18", "notched": -1, "unnotched": 0, "unsure": 0}
    assert client.post("/api/sight-counts", json=body, headers=abc).status_code == 400
    body.update(notched=1, ward_no=9999)
    assert client.post("/api/sight-counts", json=body, headers=abc).status_code == 404


def test_field_records_listed_are_those_of_the_coverage_window(client, ctx, people, wards):
    old = (clock.local_today(ctx.settings.local_tz) - timedelta(weeks=ctx.policy["stats"]["coverage_window_weeks"] + 2)).isoformat()  # fmt: skip
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO worked_wards(ward_no, worked_on, recorded_by, is_demo) VALUES (?,?,?,1)", (wards[0], old, "staff:abc"))  # fmt: skip
    rows = client.get("/api/worked", headers=people["abc"].read).json()
    assert old not in {r["worked_on"] for r in rows}
