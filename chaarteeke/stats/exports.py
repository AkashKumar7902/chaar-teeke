"""CSV exports. The weekly digest carries aggregates only; the register export is for Health.

Neither holds a name, a phone number or a row-level date. Weeks are Monday labels, months are YYYY-MM.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict

from .. import clock
from ..context import AppContext
from ..models import Share
from ..regimens import regimens
from .clusters import ward_table
from .intervals import (
    WITHHELD,
    hide_across_areas,
    hide_group,
    hide_small,
    share,
    street_total,
    suppressed_label,
)
from .tally import Tally, clusters_of, coverage_window, is_demo, load_rows, sum_tallies, tally_by

DEMO_LINE = "# DEMO DATA: synthetic, not real patients."

WEEKLY_COLUMNS = [
    "period_type",
    "period",
    "weeks_in_period",
    "area",
    "area_name",
    "bites",
    "street_dog_bites",
    "notch_seen",
    "no_notch_seen",
    "could_not_see",
    "answers",
    "notch_share_percent",
    "interval_low_percent",
    "interval_high_percent",
    "answers_needed",
    "school",
    "hospital",
    "bus_market",
    "alerts",
]


def _stamp(ctx: AppContext, what: str) -> list[str]:
    cfg = ctx.policy["stats"]
    rule = (
        f"# {what} Counts under {cfg['suppress_below']} show {suppressed_label(ctx.policy)} wherever no "
        f"percentage sits beside them. A percentage appears only with at least {cfg['min_n_for_share']} "
        "answers, and the counts behind it are then shown."
    )
    return ([DEMO_LINE] if is_demo(ctx) else []) + [rule]


def _pct(v: float | None) -> str:
    """Whole percent: a share of a few dozen answers has no tenth of a percent in it."""
    return "" if v is None else f"{round(v * 100)}"


COUNT_COLUMNS = ("bites", "street", "school", "hospital", "bus_market", "alerts")


def _row(kind: str, period: str, weeks: int, area: str, name: str, s: Share, counts: dict) -> list:
    return [
        kind,
        period,
        weeks,
        area,
        name,
        counts["bites"],
        counts["street"],
        s.notched,
        s.unnotched,
        s.unsure,
        "" if s.status == "no_data" else s.n,
        _pct(s.share),
        _pct(s.ci_low),
        _pct(s.ci_high),
        s.needed if s.needed is not None else "",
        counts["school"],
        counts["hospital"],
        counts["bus_market"],
        counts["alerts"],
    ]


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """A text cell that a spreadsheet would run as a formula (a register number typed by a clinic) is
    written with a leading apostrophe, so it opens as text."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def _write(lines: list[str], header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    for line in lines:
        buf.write(line + "\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows([_cell(v) for v in row] for row in rows)
    return buf.getvalue()


def _area_counts(t: Tally, alerts: int) -> dict[str, int]:
    return {
        "bites": t.bites,
        "street": t.notched + t.unnotched + t.unsure,
        "school": t.school,
        "hospital": t.hospital,
        "bus_market": t.bus_market,
        "alerts": alerts,
    }


def weekly_csv(ctx: AppContext) -> str:
    """One row for the whole city and one per analysis area, each over the whole coverage window.

    One granularity only. Rows by week for the city and by month for the areas would let a hidden count be
    worked out from the weeks that add up to the month, and the area weeks on the cluster page would give the
    rest away. The areas' counts are hidden together with the city's (hide_group): a "<5" cell never stands
    beside a total and shown cells that give it back.
    """
    clusters = clusters_of(ctx)
    policy = ctx.policy
    win = coverage_window(ctx)
    by = tally_by(ctx, load_rows(ctx, win.first_week, win.last_week))
    weeks = win.week_list()
    per = {c.id: sum_tallies(by[(c.id, wk)] for wk in weeks if (c.id, wk) in by) for c in clusters.all()}
    city = sum_tallies(per.values())
    alert_rows = ctx.db.query(
        "SELECT cluster_id, COUNT(*) AS n FROM alerts WHERE local_day BETWEEN ? AND ? GROUP BY cluster_id",
        (win.first_week.isoformat(), win.end.isoformat()),
    )
    by_area = {r["cluster_id"]: r["n"] for r in alert_rows if r["cluster_id"]}
    city_alerts = sum(r["n"] for r in alert_rows)

    raw = {cid: _area_counts(t, by_area.get(cid, 0)) for cid, t in per.items()}
    shares = {cid: _share_of(t, policy) for cid, t in per.items()}
    hide_across_areas({cid: (t.notched, t.unnotched, t.unsure) for cid, t in per.items()}, shares, policy)
    shown: dict[str, dict] = {cid: {} for cid in per}
    city_shown: dict = {}
    for col in COUNT_COLUMNS:
        cells, total = hide_group({cid: raw[cid][col] for cid in per}, policy)
        for cid in per:
            shown[cid][col] = cells[cid]
        city_shown[col] = total
    city_raw = _area_counts(city, city_alerts)
    for col in ("bites", "street", "school", "hospital", "bus_market"):
        if city_shown[col] != WITHHELD:
            city_shown[col] = hide_small(city_raw[col], policy)
    city_shown["street"] = street_total(city.notched, city.unnotched, city.unsure, policy)
    city_shown["alerts"] = hide_small(city_alerts, policy)
    for cid, t in per.items():
        own = street_total(t.notched, t.unnotched, t.unsure, policy)
        if own == WITHHELD or shares[cid].unsure == WITHHELD:
            shown[cid]["street"] = WITHHELD

    period = win.first_week.isoformat()
    rows = [_row("window", period, win.weeks, "city", "Whole city", _share_of(city, policy), city_shown)]
    for c in clusters.all():
        rows.append(_row("window", period, win.weeks, c.id, c.name, shares[c.id], shown[c.id]))
    note = f"# One row per area over the {win.weeks} weeks that begin on the Monday shown."
    return _write([*_stamp(ctx, "Aggregates only."), note], WEEKLY_COLUMNS, rows)


def _share_of(t: Tally, policy) -> Share:
    return share(t.notched, t.unnotched, t.unsure, policy, unanswered=t.unanswered)


REGISTER_COLUMNS = [
    "clinic",
    "register_no",
    "type_of_patient",
    "animal_type",
    "ward_no",
    "ward_name",
    "route",
    "doses_given",
    "doses_scheduled",
    "pep_status",
]
_ANIMAL_TYPE = {
    "notched": "stray dog",
    "unnotched": "stray dog",
    "unsure": "stray dog",
    "pet": "pet dog",
    "other": "other animal",
    "animal_unknown": "not known",
    "no_answer": "not known",
}


def register_csv(ctx: AppContext) -> str:
    """The NRCP animal bite exposure register columns this system holds, one row per course.

    Type of patient, stray or pet, ward of the bite, route, dose numbers given and whether the course
    is complete. Name, age, address, phone, category and RIG are not held, so they are not columns.
    Where the patient did not answer the questions the cell says so rather than guessing.
    """
    today = clock.local_today(ctx.settings.local_tz).isoformat()
    doses: dict[str, list] = defaultdict(list)
    for d in ctx.db.query(
        "SELECT course_id, dose_no, due_date, status FROM doses ORDER BY course_id, dose_no"
    ):
        doses[d["course_id"]].append(d)
    table = ward_table(ctx.settings.data_dir / "wards.geojson")
    regs = regimens()
    out = []
    for c in ctx.db.query(
        "SELECT c.id, c.register_no, c.patient_type, c.regimen_id, c.status, cl.short_name AS clinic, "
        "b.animal, b.ward_no, b.location_status "
        "FROM courses c JOIN clinics cl ON cl.id = c.clinic_id "
        "LEFT JOIN bite_reports b ON b.course_id = c.id ORDER BY cl.id, c.register_no, c.id"
    ):
        ds = doses[c["id"]]
        given = [str(d["dose_no"]) for d in ds if d["status"] == "given"]
        if not ds:
            pep = "no doses scheduled"
        elif len(given) == len(ds):
            pep = "complete"
        elif any(d["status"] == "not_needed" for d in ds):
            pep = "stopped by clinic"
        elif c["status"] == "stopped" or max(d["due_date"] for d in ds) < today:
            pep = "incomplete"
        else:
            pep = "in progress"
        if c["animal"] is None:
            animal = "not asked"
        else:
            animal = _ANIMAL_TYPE[c["animal"]]
        if c["ward_no"] is not None:
            info = table.get(c["ward_no"])
            ward_no, ward_name = c["ward_no"], info.name if info else ""
        elif c["location_status"] == "outside_jmc":
            ward_no, ward_name = "", "outside JMC"
        else:
            ward_no, ward_name = "", "not known" if c["animal"] is not None else "not asked"
        route = regs[c["regimen_id"]].route
        out.append(
            [
                c["clinic"],
                c["register_no"] or "",
                c["patient_type"],
                animal,
                ward_no,
                ward_name,
                route,
                ";".join(given),
                len(ds),
                pep,
            ]
        )
    return _write(
        _stamp(ctx, "Register columns held by Chaar Teeke; no names or phone numbers."), REGISTER_COLUMNS, out
    )
