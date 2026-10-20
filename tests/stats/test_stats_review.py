"""Cases found in review: hidden cells that other cells give back, minimums on percentages, spreadsheet
formulas in exports, the part-way week in look-here, what Reset keeps, and audit of re-placed alerts."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

import pytest
from stats_helpers import build_ctx

from chaarteeke import alerts, clock
from chaarteeke.demo import seed as seeder
from chaarteeke.stats.coverage import coverage, oversight_summary
from chaarteeke.stats.errors import StatsError
from chaarteeke.stats.exports import register_csv, weekly_csv
from chaarteeke.stats.intervals import share, street_total

SUNDAY = datetime(2026, 10, 18, 4, 30, tzinfo=UTC)  # Sunday 18 Oct 2026, 10:00 IST


def parse(text):
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(lines))))


# ------------------------------------------------------------ hidden cells stay hidden
def small(v):
    return 0 < v < 5


def test_no_hidden_cell_can_be_read_back_from_the_cells_that_are_shown(ctx):
    """Over every small combination: a cell hidden as 1 to 4 is never n minus a shown cell, nor the street
    total minus n. Beside a percentage (25 or more answers) nothing is hidden, because the share gives it."""
    for x in range(0, 40):
        for y in range(0, 40):
            for u in range(0, 12):
                s = share(x, y, u, ctx.policy)
                street = street_total(x, y, u, ctx.policy)
                if x + y >= 25:
                    assert (s.notched, s.unnotched, s.unsure) == (x, y, u) and street == x + y + u
                    continue
                if small(x):
                    assert not isinstance(s.unnotched, int), (x, y, u)
                if small(y):
                    assert not isinstance(s.notched, int), (x, y, u)
                if small(u):
                    assert not isinstance(street, int), (x, y, u)


@pytest.mark.parametrize(("u", "want"), [(0, 20), (2, "hidden"), (5, 25)])
def test_street_total_is_not_a_way_back_to_a_hidden_could_not_see(ctx, u, want):
    assert street_total(12, 8, u, ctx.policy) == want


def test_street_total_below_five_reads_less_than_five(ctx):
    assert street_total(1, 1, 1, ctx.policy) == "<5"
    assert street_total(0, 0, 0, ctx.policy) == "<5"


def test_weekly_csv_does_not_give_a_hidden_could_not_see_back(sctx, add_reports):
    ward = sctx.clusters.all()[0].ward_nos[0]
    add_reports(ward, "notched", n=12)
    add_reports(ward, "unnotched", n=8)
    add_reports(ward, "unsure", n=2)
    row = next(r for r in parse(weekly_csv(sctx)) if r["area"] == sctx.clusters.all()[0].id)
    assert row["could_not_see"] == "<5"
    assert row["street_dog_bites"] == "hidden"
    assert (row["notch_seen"], row["no_notch_seen"], row["answers"]) == ("12", "8", "20")


# ------------------------------------------------ percentages need the minimum number
def courses(conn, count, completed, clinic="cln_t"):
    for i in range(count):
        cid = f"crs_{i}"
        conn.execute(
            "INSERT INTO courses(id, clinic_id, register_no, regimen_id, day0, lang, status, eligibility, source, created_at, is_demo)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,1)",
            (
                cid,
                clinic,
                f"R{i}",
                "id_trc",
                "2026-09-01",
                "hi",
                "completed" if i < completed else "stopped",
                "adult",
                "demo",
                "2026-09-01T05:00:00.000Z",
            ),
        )
        for n, due in enumerate(("2026-09-01", "2026-09-04"), start=1):
            conn.execute(
                "INSERT INTO doses(course_id, dose_no, offset_days, due_date, status) VALUES (?,?,0,?,'due')",
                (cid, n, due),
            )


def test_completion_rate_needs_the_minimum_number_of_finished_courses(sctx):
    with sctx.db.transaction() as conn:
        courses(conn, 24, 20)
    s = oversight_summary(sctx)
    assert s.completion_rate is None
    assert s.completion_basis.startswith("n = 24 of 25 needed")


def test_a_tiny_number_of_finished_courses_is_not_printed(sctx):
    with sctx.db.transaction() as conn:
        courses(conn, 3, 2)
    s = oversight_summary(sctx)
    assert s.completion_rate is None
    assert "<5" in s.completion_basis and "3" not in s.completion_basis.split("of 25")[0][-3:]


def test_completion_rate_with_enough_courses_carries_its_interval_and_hides_small_counts(sctx):
    with sctx.db.transaction() as conn:
        courses(conn, 30, 27)
        conn.execute(
            "INSERT INTO courses(id, clinic_id, register_no, regimen_id, day0, lang, status, eligibility, source, created_at, is_demo)"
            " VALUES ('crs_run','cln_t','RUN','id_trc','2026-10-15','hi','active','adult','demo','2026-10-15T05:00:00.000Z',1)"
        )
        conn.execute(
            "INSERT INTO doses(course_id, dose_no, offset_days, due_date, status) VALUES ('crs_run',1,0,'2026-10-30','due')"
        )
    s = oversight_summary(sctx)
    assert s.completion_rate == 0.9
    assert "27 of 30" in s.completion_basis and "95% interval" in s.completion_basis
    assert "<5 still running" in s.completion_basis  # one course still running: a small count


# ------------------------------------------------------- spreadsheet formulas in exports
def test_register_csv_neutralises_text_a_spreadsheet_would_run(sctx):
    with sctx.db.transaction() as conn:
        courses(conn, 1, 0)
        conn.execute("UPDATE courses SET register_no = '=HYPERLINK(\"x\")'")
    rows = parse(register_csv(sctx))
    assert rows[0]["register_no"] == '\'=HYPERLINK("x")'


def test_register_numbers_that_are_plain_stay_as_they_are(sctx):
    with sctx.db.transaction() as conn:
        courses(conn, 1, 0)
        conn.execute("UPDATE courses SET register_no = '2026/114'")
    assert parse(register_csv(sctx))[0]["register_no"] == "2026/114"


# ------------------------------------------------------------- look-here and the week
def seed_low_current_week(sctx, add_reports):
    """c3 is low only in the current week (30 answers); every other week of every cluster is at 80%."""
    for c in sctx.clusters.all():
        ward = c.ward_nos[0]
        for weeks_ago in range(0, 8):
            low = c.id == "c3" and weeks_ago == 0
            add_reports(ward, "notched", n=3 if low else 8, weeks_ago=weeks_ago)
            add_reports(ward, "unnotched", n=27 if low else 2, weeks_ago=weeks_ago)


def test_on_a_sunday_the_current_week_is_complete_and_counts(sctx, add_reports):
    with clock.use_clock(clock.FixedClock(SUNDAY)):
        seed_low_current_week(sctx, add_reports)
        assert coverage(sctx).look_here == ["c3"]


def test_on_a_monday_the_part_way_week_is_left_out(sctx, add_reports):
    seed_low_current_week(sctx, add_reports)  # T0 is a Monday
    assert coverage(sctx).look_here == []


# ---------------------------------------------------------- loud failures
def test_a_report_in_a_ward_that_belongs_to_no_area_stops_the_view(sctx, add_reports):
    add_reports(None, "notched", n=1)
    with sctx.db.transaction() as conn:
        conn.execute("UPDATE bite_reports SET ward_no = 999")
    with pytest.raises(StatsError) as e:
        coverage(sctx)
    assert e.value.code == "unknown_ward"


# ----------------------------------------------------------------- alert placement
def test_replacing_an_alerts_ward_is_audited_once(sctx, add_reports):
    with sctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO courses(id, clinic_id, register_no, regimen_id, day0, lang, status, eligibility, source, created_at, is_demo)"
            " VALUES ('crs_1','cln_t','S1','id_trc','2026-10-18','hi','active','adult','demo','2026-10-18T05:00:00.000Z',1)"
        )
    ward = sctx.clusters.all()[0].ward_nos[0]
    (rid,) = add_reports(None, "unnotched", signs="yes", course_id="crs_1", location_status="needs_review")
    with sctx.db.transaction() as conn:
        aid = alerts.from_report(sctx, conn, rid)
    for _ in range(2):
        with sctx.db.transaction() as conn:
            conn.execute(
                "UPDATE bite_reports SET ward_no = ?, location_status = 'decided' WHERE id = ?", (ward, rid)
            )
            alerts.update_location(sctx, conn, rid)
    placed = sctx.db.query("SELECT actor, object_id, detail_json FROM audit WHERE action = 'alert.place'")
    assert len(placed) == 1  # the second call changed nothing
    assert placed[0]["object_id"] == aid and str(ward) in placed[0]["detail_json"]


# ------------------------------------------------------------------- Reset
def test_reset_signs_staff_out_but_keeps_visitors(tmp_path, fixed_clock):
    ctx = build_ctx(tmp_path)
    seeder.seed(ctx)
    staff_id = ctx.db.one("SELECT id FROM staff WHERE username = 'clinic'")["id"]
    expires = clock.iso(clock.now())
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, staff_id, role, visitor_label, csrf, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            ("t_staff", staff_id, "clinic", None, "c", expires, expires),
        )
        conn.execute(
            "INSERT INTO sessions(token_hash, staff_id, role, visitor_label, csrf, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            ("t_visitor", None, "visitor", "visitor:1", "c", expires, expires),
        )
    seeder.seed(ctx, reset=True)
    left = {r["token_hash"] for r in ctx.db.query("SELECT token_hash FROM sessions")}
    assert left == {"t_visitor"}
    ctx.db.close()
