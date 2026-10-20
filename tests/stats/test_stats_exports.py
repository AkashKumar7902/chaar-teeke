"""The CSVs hold aggregates (weekly) or register columns (register): no phone numbers, no row-level dates."""

from __future__ import annotations

import csv
import io
import re
from datetime import date

from chaarteeke.stats.exports import REGISTER_COLUMNS, WEEKLY_COLUMNS, register_csv, weekly_csv

PHONE = re.compile(r"\+?\d{10,}")
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def parse(text):
    lines = text.splitlines()
    comments = [ln for ln in lines if ln.startswith("#")]
    rows = list(csv.DictReader(io.StringIO("\n".join(ln for ln in lines if not ln.startswith("#")))))
    return comments, rows


def test_weekly_csv_is_stamped_demo_and_has_the_columns(seeded):
    text = weekly_csv(seeded)
    comments, rows = parse(text)
    assert comments[0] == "# DEMO DATA: synthetic, not real patients."
    assert list(rows[0]) == WEEKLY_COLUMNS
    # one granularity only: the whole coverage window, for the city and for each area
    assert {r["period_type"] for r in rows} == {"window"}
    assert [r["area"] for r in rows] == ["city"] + [f"c{i}" for i in range(1, 9)]
    assert {r["weeks_in_period"] for r in rows} == {"12"}


def test_weekly_csv_has_no_phone_number_and_no_row_level_date(seeded):
    text = weekly_csv(seeded)
    assert not PHONE.search(text) and not INSTANT.search(text) and "+91" not in text
    _, rows = parse(text)
    for r in rows:
        assert date.fromisoformat(r["period"]).weekday() == 0  # a Monday label, not a day


def test_percentages_only_where_the_share_may_be_shown(seeded):
    _, rows = parse(weekly_csv(seeded))
    for r in rows:
        percent = (r["notch_share_percent"], r["interval_low_percent"], r["interval_high_percent"])
        if r["answers"] == "":  # nothing yet: blank, never a zero
            assert not any(percent) and r["answers_needed"] == ""
        elif int(r["answers"]) < 25:
            assert not any(percent) and r["answers_needed"] == str(25 - int(r["answers"]))
        else:
            assert all(percent) and r["answers_needed"] == ""


def test_percentages_are_whole_numbers(seeded):
    _, rows = parse(weekly_csv(seeded))
    cells = [
        r[c] for r in rows for c in ("notch_share_percent", "interval_low_percent", "interval_high_percent")
    ]
    assert any(cells) and all(c == "" or re.fullmatch(r"\d{1,3}", c) for c in cells)


def test_small_counts_read_less_than_five_and_thin_areas_show_no_percentage(sctx, add_reports):
    area = sctx.clusters.all()[0]
    add_reports(area.ward_nos[0], "notched", n=3)
    add_reports(area.ward_nos[0], "unnotched", n=7, place_type="school")
    text = weekly_csv(sctx)
    _, rows = parse(text)
    row = next(r for r in rows if r["area"] == area.id)
    assert row["bites"] == "10" and row["notch_seen"] == "<5" and row["no_notch_seen"] == "hidden"
    assert (row["answers"], row["notch_share_percent"], row["answers_needed"]) == ("10", "", "15")
    assert row["school"] == "7"
    assert "<5" in text


def test_a_hidden_area_cell_is_not_the_city_cell_minus_the_other_areas(sctx, add_reports):
    """Area 1 has 3 school bites; the city has 35 and area 2 has 32. Without more hiding, 35 - 32 = 3."""
    first, second = sctx.clusters.all()[:2]
    add_reports(first.ward_nos[0], "unnotched", n=3, place_type="school")
    add_reports(first.ward_nos[0], "unnotched", n=20)
    add_reports(second.ward_nos[0], "unnotched", n=32, place_type="school")
    add_reports(second.ward_nos[0], "unnotched", n=20)
    _, rows = parse(weekly_csv(sctx))
    schools = {r["area"]: r["school"] for r in rows}
    assert schools["city"] == "35"
    assert schools[first.id] == "<5" and schools[second.id] == "hidden"


def test_pilot_data_is_not_stamped_demo(sctx, add_reports):
    sctx.settings = sctx.settings.model_copy(update={"mode": "pilot"})
    with sctx.db.transaction() as conn:
        conn.execute("UPDATE clinics SET is_demo = 0")
    add_reports(sctx.clusters.all()[0].ward_nos[0], "notched", n=1, is_demo=0)
    comments, _ = parse(weekly_csv(sctx))
    assert not any("DEMO" in c for c in comments)
    assert any("Aggregates only" in c for c in comments)


def test_register_csv_has_the_columns_we_hold_and_nothing_personal(seeded):
    text = register_csv(seeded)
    comments, rows = parse(text)
    assert comments[0].startswith("# DEMO DATA")
    assert list(rows[0]) == REGISTER_COLUMNS
    assert len(rows) == seeded.db.one("SELECT COUNT(*) AS n FROM courses")["n"]
    assert not PHONE.search(text) and not INSTANT.search(text) and "+91" not in text
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text)  # no dates at all
    assert {r["type_of_patient"] for r in rows} == {"new", "old"}
    assert {r["animal_type"] for r in rows} >= {"stray dog", "pet dog", "other animal", "not asked"}
    assert {r["pep_status"] for r in rows} >= {"complete", "incomplete", "in progress"}
    assert {r["route"] for r in rows} <= {"ID", "IM", "ID or IM"}


def test_register_rows_agree_with_the_doses(seeded):
    _, rows = parse(register_csv(seeded))
    given = {
        (r["clinic"], r["register_no"]): sorted(int(x) for x in r["doses_given"].split(";") if x)
        for r in rows
    }
    sample = seeded.db.query(
        "SELECT cl.short_name AS clinic, c.register_no, c.id FROM courses c JOIN clinics cl ON cl.id = c.clinic_id "
        "ORDER BY c.id LIMIT 40"
    )
    for s in sample:
        want = [
            d["dose_no"]
            for d in seeded.db.query(
                "SELECT dose_no FROM doses WHERE course_id = ? AND status = 'given' ORDER BY dose_no",
                (s["id"],),
            )
        ]
        assert given[(s["clinic"], s["register_no"])] == want
