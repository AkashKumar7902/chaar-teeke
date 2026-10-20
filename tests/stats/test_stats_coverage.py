"""Coverage, ward counts, cluster detail and the oversight summary, on hand-made reports."""

from __future__ import annotations

from datetime import timedelta

from chaarteeke import clock
from chaarteeke.stats.coverage import CLAIM_NOTE, cluster_detail, coverage, oversight_summary, ward_counts


def first_ward(ctx, index=0):
    return ctx.clusters.all()[index].ward_nos[0]


def test_city_share_counts_only_placed_street_dog_bites(sctx, add_reports):
    ward = first_ward(sctx)
    add_reports(ward, "notched", n=30)
    add_reports(ward, "unnotched", n=10)
    add_reports(ward, "unsure", n=8)
    add_reports(ward, "pet", n=9)  # not a street dog
    add_reports(None, "notched", n=20)  # no ward: not in any cluster
    cov = coverage(sctx)
    assert (cov.city.n, cov.city.notched, cov.city.unnotched, cov.city.unsure) == (40, 30, 10, 8)
    assert cov.city.share == 0.75 and cov.city.status == "bracketed"
    assert cov.total_bites == 30 + 10 + 8 + 9 + 20
    assert cov.could_not_place == 20


def test_cluster_below_min_n_shows_how_many_more_are_needed(sctx, add_reports):
    c = sctx.clusters.all()[0]
    add_reports(c.ward_nos[0], "notched", n=8)
    add_reports(c.ward_nos[0], "unnotched", n=3)
    out = next(x for x in coverage(sctx).clusters if x.id == c.id)
    assert out.share.status == "too_few" and out.share.needed == 14 and out.share.share is None
    assert out.share.n == 11 and out.bites == 11 and out.street_dog_bites == 11
    assert (out.share.notched, out.share.unnotched) == ("hidden", "<5")  # 3 is small, so 8 would reveal it


def test_small_counts_are_suppressed_in_the_cluster_row(sctx, add_reports):
    c = sctx.clusters.all()[1]
    add_reports(c.ward_nos[0], "notched", n=3)
    add_reports(c.ward_nos[0], "pet", n=1)
    out = next(x for x in coverage(sctx).clusters if x.id == c.id)
    assert out.bites == "<5" and out.street_dog_bites == "<5"
    assert out.share.notched == "<5"


def test_a_hidden_could_not_see_count_is_not_rebuilt_from_the_street_total(sctx, add_reports):
    c = sctx.clusters.all()[2]
    add_reports(c.ward_nos[0], "notched", n=12)
    add_reports(c.ward_nos[0], "unnotched", n=8)
    add_reports(c.ward_nos[0], "unsure", n=2)
    out = next(x for x in coverage(sctx).clusters if x.id == c.id)
    assert out.share.unsure == "<5" and out.street_dog_bites == "hidden"


def test_beside_a_percentage_the_cluster_row_shows_its_counts(sctx, add_reports):
    c = sctx.clusters.all()[2]
    add_reports(c.ward_nos[0], "notched", n=30)
    add_reports(c.ward_nos[0], "unnotched", n=20)
    add_reports(c.ward_nos[0], "unsure", n=2)
    out = next(x for x in coverage(sctx).clusters if x.id == c.id)
    assert out.share.unsure == 2 and out.street_dog_bites == 52 and out.share.status == "ok"


def test_the_bounds_count_every_report_with_no_usable_animal_answer(sctx, add_reports):
    ward = first_ward(sctx)
    add_reports(ward, "notched", n=30)
    add_reports(ward, "unnotched", n=20)
    add_reports(ward, "unsure", n=10)
    add_reports(ward, "animal_unknown", n=5)
    add_reports(ward, "no_answer", n=5)
    s = coverage(sctx).city
    assert (s.n, s.unsure) == (50, 10) and s.share == 0.6
    assert (s.bound_low, s.bound_high) == (round(30 / 70, 4), round(50 / 70, 4))  # 10 + 5 + 5 unknown


def test_window_is_twelve_weeks_and_older_reports_are_left_out(sctx, add_reports):
    ward = first_ward(sctx)
    add_reports(ward, "notched", n=30, weeks_ago=11)
    add_reports(ward, "notched", n=30, weeks_ago=12)
    cov = coverage(sctx)
    assert cov.city.n == 30
    assert cov.window_start == (clock.week_start(clock.local_today()) - timedelta(weeks=11)).isoformat()
    assert cov.window_end == clock.local_today().isoformat()


def test_claim_band_and_label(sctx):
    cov = coverage(sctx)
    assert cov.claim_band == (0.78, 0.80)
    assert cov.claim_label == "78-80% claimed: expected only if sterilised and unsterilised dogs bite equally"
    assert cov.city.status == "no_data" and cov.is_demo is True


def test_simulated_bites_are_counted_apart(sctx, add_reports):
    ward = first_ward(sctx)
    add_reports(ward, "notched", n=6)
    add_reports(ward, "notched", n=3, source="simulator")
    out = next(x for x in coverage(sctx).clusters if ward in x.ward_nos)
    assert out.simulated_bites == 3 and out.bites == 9


def test_ward_counts_are_counts_only(sctx, add_reports):
    ward = first_ward(sctx)
    add_reports(ward, "notched", n=12, place_type="school")
    add_reports(ward, "notched", n=2, place_type="hospital")
    rows = {r.ward_no: r for r in ward_counts(sctx)}
    assert len(rows) == 75
    r = rows[ward]
    assert (r.bites, r.school, r.hospital, r.bus_market) == (14, 12, "<5", "<5")
    assert r.alerts == "<5" and r.worked_on is None and r.ward_name
    assert not hasattr(r, "share")
    assert rows[sctx.clusters.all()[3].ward_nos[0]].bites == "<5"  # no calls: never a bare zero


def test_ward_counts_show_the_latest_worked_day(sctx):
    ward = first_ward(sctx)
    with sctx.db.transaction() as conn:
        for day in ("2026-09-01", "2026-10-02"):
            conn.execute(
                "INSERT INTO worked_wards(ward_no, worked_on, recorded_by, is_demo) VALUES (?,?,?,1)",
                (ward, day, "staff:abc"),
            )
    assert {r.ward_no: r for r in ward_counts(sctx)}[ward].worked_on == "2026-10-02"


def test_cluster_detail(sctx, add_reports):
    c = sctx.clusters.all()[0]
    ward = c.ward_nos[0]
    add_reports(ward, "notched", n=40)
    add_reports(ward, "unnotched", n=10, place_type="school")
    with sctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO worked_wards(ward_no, worked_on, recorded_by, is_demo) VALUES (?,?,?,1)",
            (ward, (clock.local_today() - timedelta(days=21)).isoformat(), "staff:abc"),
        )
    d = cluster_detail(sctx, c.id)
    assert d["id"] == c.id and d["name"] == c.name and d["bites"] == 50
    assert d["bites_per_10000"] == round(50 / c.population * 10_000, 1)
    assert d["place_types"]["school"] == 10 and d["place_types"]["hospital"] == "<5"
    assert d["share"]["status"] == "ok" and d["share"]["n"] == 50
    w = d["worked_wards"][0]
    assert w["ward_no"] == ward and w["label"] == "early signal"
    assert w["bites_before"] == "<5" and w["after_weeks"] >= 1
    assert all(row["cluster_id"] == c.id for row in d["wards"])


def test_a_small_cluster_gets_no_rate_per_ten_thousand(sctx, add_reports):
    c = sctx.clusters.all()[0]
    add_reports(c.ward_nos[0], "notched", n=3)
    d = cluster_detail(sctx, c.id)
    assert d["bites"] == "<5" and d["bites_per_10000"] is None


def insert_course(conn, cid, day0, status, doses, clinic="cln_t"):
    conn.execute(
        "INSERT INTO courses(id, clinic_id, register_no, regimen_id, day0, lang, status, eligibility, source, created_at, is_demo)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,1)",
        (cid, clinic, cid, "id_trc", day0, "hi", status, "adult", "demo", "2026-10-01T05:00:00.000Z"),
    )
    for n, due in enumerate(doses, start=1):
        conn.execute(
            "INSERT INTO doses(course_id, dose_no, offset_days, due_date, status) VALUES (?,?,?,?,'due')",
            (cid, n, 0, due),
        )


def test_oversight_completion_rate_states_its_basis(sctx):
    with sctx.db.transaction() as conn:
        for i in range(30):  # 25 finished courses are needed before a rate is printed
            insert_course(
                conn,
                f"a{i}",
                "2026-09-01",
                "completed" if i < 20 else "stopped",
                ["2026-09-01", "2026-09-04"],
            )
        insert_course(conn, "d", "2026-10-15", "active", ["2026-10-15", "2026-10-30"])  # still running
    s = oversight_summary(sctx)
    # the one course still running would be the enrolled count minus the 30 shown ones, so enrolled is withheld
    assert s.enrolled == "hidden" and s.completed == 20
    assert s.completion_rate == round(20 / 30, 4)
    assert "20 of 30" in s.completion_basis and "<5 still running" in s.completion_basis
    assert "register" in s.completion_basis
    assert s.coverage.is_demo and s.is_demo


def test_oversight_with_nothing_finished_has_no_rate(sctx):
    with sctx.db.transaction() as conn:
        insert_course(conn, "d", "2026-10-15", "active", ["2026-10-15", "2026-10-30"])
    s = oversight_summary(sctx)
    assert s.completion_rate is None and "No course has reached" in s.completion_basis


def test_oversight_counts_places_and_alerts(sctx, add_reports):
    ward = first_ward(sctx)
    add_reports(ward, "notched", n=3)
    add_reports(None, "notched", n=2, location_status="outside_jmc")
    add_reports(None, "notched", n=4, location_status="cant_tell")
    s = oversight_summary(sctx)
    assert (s.bites_inside_jmc, s.bites_with_place) == ("<5", 5)  # 3 inside and 2 outside are both small


def test_the_band_label_has_a_stable_key_and_the_numbers_it_takes(sctx):
    cov = coverage(sctx)
    assert cov.claim_label_key == "share.claim"
    lo, hi = cov.claim_band
    # the English text the key stands for, with {lo} and {hi} filled, is the sentence the server also sends
    assert cov.claim_label == f"{round(lo * 100)}-{round(hi * 100)}% claimed: {CLAIM_NOTE}"


def test_place_types_add_up_to_bites_with_an_unknown_bucket(sctx, add_reports):
    c = sctx.clusters.all()[0]
    ward = c.ward_nos[0]
    add_reports(ward, "notched", n=30)
    add_reports(ward, "unnotched", n=20, place_type="school")
    add_reports(ward, "unnotched", n=12, place_type="no_answer")
    add_reports(ward, "unnotched", n=11, place_type="other")
    d = cluster_detail(sctx, c.id)
    assert d["bites"] == 73
    assert d["place_types"] == {
        "home_street": 30, "school": 20, "hospital": "<5", "bus_market": "<5", "other": 11, "unknown": 12,
    }  # fmt: skip
    assert set(d["place_types"]) == {"home_street", "school", "hospital", "bus_market", "other", "unknown"}


def test_a_single_small_place_type_cannot_be_subtracted_back(sctx, add_reports):
    c = sctx.clusters.all()[0]
    ward = c.ward_nos[0]
    add_reports(ward, "notched", n=30)
    add_reports(ward, "unnotched", n=12, place_type="school")
    add_reports(ward, "unnotched", n=3, place_type="hospital")
    add_reports(ward, "unnotched", n=20, place_type="bus_market")
    add_reports(ward, "unnotched", n=15, place_type="other")
    add_reports(ward, "unnotched", n=11, place_type="no_answer")
    d = cluster_detail(sctx, c.id)
    shown = d["place_types"]
    assert shown["hospital"] == "<5"
    assert list(shown.values()).count("hidden") == 1  # the smallest shown cell goes too
    assert shown["unknown"] == "hidden"  # 11 is the smallest shown cell
    assert d["bites"] == 91


def test_the_weekly_bites_series_is_counts_only_and_covers_the_window(sctx, add_reports):
    c = sctx.clusters.all()[0]
    ward = c.ward_nos[0]
    add_reports(ward, "notched", n=9, weeks_ago=1)
    add_reports(ward, "unnotched", n=2, weeks_ago=2)
    add_reports(ward, "unnotched", n=14, weeks_ago=3)
    other = sctx.clusters.all()[1].ward_nos[0]
    add_reports(other, "notched", n=40, weeks_ago=1)  # another area is not in this series
    d = cluster_detail(sctx, c.id)
    weeks = d["weekly_bites"]
    window = sctx.policy["stats"]["coverage_window_weeks"]
    assert len(weeks) == window
    assert [w["week_start"] for w in weeks] == sorted(w["week_start"] for w in weeks)
    assert set(weeks[0]) == {"week_start", "bites", "partial"}
    by_week = {w["week_start"]: w for w in weeks}
    this_monday = clock.week_start(clock.local_today())
    # 2 bites in one week and nine empty weeks are hidden: together 2, which is itself small, so the
    # smallest shown week (9) is hidden as well
    assert by_week[(this_monday - timedelta(weeks=1)).isoformat()]["bites"] == "hidden"
    assert by_week[(this_monday - timedelta(weeks=2)).isoformat()]["bites"] == "<5"  # 2 bites
    assert by_week[(this_monday - timedelta(weeks=3)).isoformat()]["bites"] == 14
    assert by_week[this_monday.isoformat()]["bites"] == "<5"  # no calls: never a bare zero
    assert [w["week_start"] for w in weeks if w["partial"]] == [this_monday.isoformat()]  # Monday: part-way
    assert all(isinstance(w["bites"], int) or w["bites"] in ("<5", "hidden") for w in weeks)
