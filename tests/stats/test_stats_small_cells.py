"""What the small-cell rule protects, tried the way an attacker would.

Outward counts under 5 read "<5". The attacker here has everything the server shows (the other counts, the
total beside them, n, the share, the interval, the bounds) and asks: which values of the hidden cell fit?
A hidden nonzero cell is safe when two or more values fit. A hidden zero is not tested: it gives away
nothing the total does not already say.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict

import pytest

from chaarteeke import alerts
from chaarteeke.stats.coverage import cluster_detail, coverage, oversight_summary, ward_counts
from chaarteeke.stats.exports import weekly_csv
from chaarteeke.stats.intervals import WITHHELD, hide_group, share

SMALL, LOW = "<5", 5


# --------------------------------------------------------------------------- a share (T02)
def test_no_share_output_lets_a_hidden_cell_be_recomputed_exactly(ctx):
    """For every (x, y, u) up to 30 each: all the triples that give the same Share (every field) are the
    attacker's candidates. A hidden nonzero cell has two or more values among them.

    Structural exceptions stay, all with n under 10: n = 8 with both notch counts under 5 means 4 and 4, and
    n = 6 with one hidden cell and one "hidden" (5 or more) means 1 and 5. Share.n is an int in the
    contract, so it cannot be withheld; the only way out is to hide n, which models.py would have to
    allow."""
    classes: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    outputs = {}
    for x in range(31):
        for y in range(31):
            for u in range(31):
                out = share(x, y, u, ctx.policy)
                key = json.dumps(out.model_dump(), sort_keys=True)
                classes[key].append((x, y, u))
                outputs[(x, y, u)] = out
    exceptions = set()
    for (x, y, u), out in outputs.items():
        key = json.dumps(out.model_dump(), sort_keys=True)
        for idx, name in enumerate(("notched", "unnotched", "unsure")):
            if isinstance(getattr(out, name), str) and (x, y, u)[idx] > 0:
                values = {t[idx] for t in classes[key]}
                if len(values) < 2:
                    exceptions.add((x, y, name))
    assert exceptions == {
        (4, 4, "notched"),
        (4, 4, "unnotched"),
        (1, 5, "notched"),
        (1, 5, "unnotched"),
        (5, 1, "notched"),
        (5, 1, "unnotched"),
    }
    assert all(x + y < 10 for x, y, _ in exceptions)


def test_a_share_is_never_beside_a_hidden_count(ctx):
    """Once a percentage is shown the counts it rests on are exact: it gives them away anyway."""
    for x in range(0, 60, 3):
        for y in range(0, 60, 3):
            for u in range(0, 12):
                out = share(x, y, u, ctx.policy)
                if out.share is not None:
                    assert (out.notched, out.unnotched, out.unsure) == (x, y, u)
                    # and the bounds and interval are consistent with exactly those counts
                    assert round(x / (x + y), 4) == out.share


# ------------------------------------------------------ cells under a total (T08)
def feasible_values(total, cells):
    """Values each "<5" cell can take given the total and the other cells as shown. "hidden" cells are
    unknown amounts of 5 or more."""
    known = sum(c for c in cells if isinstance(c, int))
    rest = total - known
    unknown = [c for c in cells if isinstance(c, str)]
    result = []
    for i, c in enumerate(unknown):
        if c != SMALL:
            result.append(None)
            continue
        others = unknown[:i] + unknown[i + 1 :]
        ok = set()
        for v in range(LOW):
            reach = {0}
            for o in others:
                lo, hi = (0, LOW - 1) if o == SMALL else (LOW, max(rest, LOW))
                reach = {s + k for s in reach for k in range(lo, hi + 1) if s + k <= rest}
            if rest - v in reach:
                ok.add(v)
        result.append(ok)
    return rest, result


def assert_nothing_unique(total, cells, where):
    if not isinstance(total, int):
        return  # a hidden or small total gives no residual to subtract
    rest, options = feasible_values(total, cells)
    if rest == 0:
        return  # everything hidden is zero: allowed
    for opt in options:
        assert (
            opt is None or len(opt) >= 2
        ), f"{where}: a '<5' cell is the only value that fits: {cells} of {total}"


def check_cluster(ctx, cluster_id):
    d = cluster_detail(ctx, cluster_id)
    total = d["bites"]
    assert_nothing_unique(total, list(d["place_types"].values()), "place types")
    assert_nothing_unique(total, [w["bites"] for w in d["weekly_bites"]], "weekly bites")
    assert_nothing_unique(total, [w["bites"] for w in d["wards"]], "ward bites")
    for kind in ("school", "hospital", "bus_market"):
        wards = [w[kind] for w in d["wards"]]
        area = d["place_types"][kind]
        assert_nothing_unique(area, wards, f"ward {kind}")
        if area == WITHHELD:  # the area figure is itself withheld: the wards must not add up to it
            assert any(isinstance(v, str) for v in wards), f"ward {kind} add up to the withheld area figure"


def test_no_hidden_cell_in_the_seeded_demo_can_be_read_back(seeded):
    for c in seeded.clusters.all():
        check_cluster(seeded, c.id)


def test_hidden_ward_and_place_cells_in_hand_made_areas_cannot_be_read_back(sctx, add_reports):
    rng = random.Random(7)
    area = sctx.clusters.all()[0]
    kinds = ("home_street", "school", "hospital", "bus_market", "other", "no_answer")
    for _trial in range(30):
        with sctx.db.transaction() as conn:
            conn.execute("DELETE FROM bite_reports")
        for ward in area.ward_nos:
            for kind in kinds:
                n = rng.choice([0, 0, 1, 2, 3, 4, 5, 6, 9, 14, 25])
                if n:
                    add_reports(ward, "unnotched", n=n, place_type=kind)
        check_cluster(sctx, area.id)


def test_a_single_small_ward_is_hidden_with_the_next_smallest(sctx, add_reports):
    area = sctx.clusters.all()[0]
    sizes = [3, 12, 40, 25, 30, 18, 22, 35, 27, 31][: len(area.ward_nos)]
    for ward, n in zip(area.ward_nos, sizes, strict=True):
        add_reports(ward, "unnotched", n=n)
    wards = [w for w in ward_counts(sctx) if w.cluster_id == area.id]
    shown = {w.ward_no: w.bites for w in wards}
    assert shown[area.ward_nos[0]] == SMALL
    assert shown[area.ward_nos[1]] == WITHHELD  # 12 is the smallest other ward: 150 - the rest would give 3
    assert [shown[w] for w in area.ward_nos[2:]] == sizes[2:]


# ---------------------------------------------- the weekly file, the same way (T08)
def test_the_weekly_file_publishes_one_granularity(seeded):
    text = weekly_csv(seeded)
    assert "week" not in {line.split(",")[0] for line in text.splitlines() if not line.startswith("#")}
    assert "month" not in text.replace("period_type", "")


def test_the_weekly_file_rows_never_hand_back_a_hidden_cell(sctx, add_reports):
    import csv
    import io

    rng = random.Random(11)
    for _trial in range(20):
        with sctx.db.transaction() as conn:
            conn.execute("DELETE FROM bite_reports")
        for c in sctx.clusters.all():
            ward = c.ward_nos[0]
            for kind in ("school", "hospital", "bus_market", "home_street"):
                n = rng.choice([0, 1, 2, 3, 4, 6, 11, 30])
                if n:
                    add_reports(ward, rng.choice(["notched", "unnotched", "pet"]), n=n, place_type=kind)
        lines = [ln for ln in weekly_csv(sctx).splitlines() if not ln.startswith("#")]
        rows = list(csv.DictReader(io.StringIO("\n".join(lines))))
        city, areas = rows[0], rows[1:]
        for col in ("bites", "school", "hospital", "bus_market"):

            def cell(v):
                return int(v) if v.isdigit() else v

            assert_nothing_unique(cell(city[col]), [cell(r[col]) for r in areas], f"weekly {col}")


# ----------------------------------------- the oversight counts and the coverage totals (T09)
def test_two_alerts_and_two_unplaced_bites_read_less_than_five_everywhere(sctx, add_reports):
    ward = sctx.clusters.all()[0].ward_nos[0]
    add_reports(ward, "notched", n=30)
    ids = add_reports(ward, "unnotched", n=2, signs="yes")
    add_reports(None, "unnotched", n=2)
    with sctx.db.transaction() as conn:
        for rid in ids:
            alerts.from_report(sctx, conn, rid)
    cov = coverage(sctx)
    assert cov.could_not_place == SMALL
    # 34 bites: with the areas' bites and 2 unplaced shown, the total would give the 2 back
    assert cov.total_bites == WITHHELD
    s = oversight_summary(sctx)
    assert (s.alerts_total, s.alerts_acted) == (SMALL, SMALL)
    assert s.enrolled == SMALL and s.completed == SMALL
    assert s.coverage.could_not_place == SMALL


def test_oversight_counts_are_exact_only_from_five(sctx, add_reports):
    ward = sctx.clusters.all()[0].ward_nos[0]
    add_reports(ward, "notched", n=30)
    add_reports(None, "unnotched", n=7)
    cov = coverage(sctx)
    assert (cov.total_bites, cov.could_not_place) == (37, 7)
    s = oversight_summary(sctx)
    assert (s.bites_with_place, s.bites_inside_jmc) == (30, 30)


@pytest.mark.parametrize("acted", [0, 3, 8, 12])
def test_alerts_acted_never_gives_the_unacted_back(sctx, add_reports, acted):
    ward = sctx.clusters.all()[0].ward_nos[0]
    ids = add_reports(ward, "unnotched", n=12, signs="yes")
    with sctx.db.transaction() as conn:
        for rid in ids:
            alerts.from_report(sctx, conn, rid)
        conn.execute(
            "UPDATE alerts SET status = 'acted' WHERE id IN (SELECT id FROM alerts LIMIT ?)", (acted,)
        )
    s = oversight_summary(sctx)
    assert s.alerts_total == 12
    unacted = 12 - acted
    if 0 < unacted < LOW:
        assert s.alerts_acted == WITHHELD  # 12 minus the acted would be the unacted
    elif 0 < acted < LOW:
        assert s.alerts_acted == SMALL
    else:
        assert s.alerts_acted in (acted, SMALL)


def test_group_helper_matches_the_oversight_rule(ctx):
    cells, total = hide_group({"acted": 8, "not_acted": 4}, ctx.policy)
    assert (cells, total) == ({"acted": WITHHELD, "not_acted": SMALL}, 12)
