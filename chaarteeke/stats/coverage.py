"""The coverage view: city and cluster notch shares, look-here, ward counts, cluster detail, oversight."""

from __future__ import annotations

from datetime import date, timedelta

from .. import clock
from ..context import AppContext
from ..models import ClusterCoverage, Coverage, OversightSummary, Share, WardCounts
from . import lookhere
from .clusters import ward_table
from .errors import StatsError
from .exports import register_csv, weekly_csv  # noqa: F401  (MODULES lists them with coverage)
from .intervals import (  # noqa: F401
    WITHHELD,
    hide_across_areas,
    hide_cells,
    hide_group,
    hide_small,
    manski,
    share,
    street_total,
    wilson,
)
from .tally import (
    Tally,
    Window,
    clusters_of,
    coverage_window,
    is_demo,
    load_rows,
    sum_tallies,
    tally_by,
)

CLAIM_NOTE = "expected only if sterilised and unsterilised dogs bite equally"
# The band label as a stable i18n key in the common bundle, taking {lo} and {hi} in whole percent. The
# English sentence is also sent (claim_label), built from the same words.
CLAIM_LABEL_KEY = "share.claim"


def _share(t: Tally, ctx: AppContext) -> Share:
    return share(t.notched, t.unnotched, t.unsure, ctx.policy, unanswered=t.unanswered)


def _claim(ctx: AppContext) -> tuple[tuple[float, float], str]:
    lo, hi = ctx.policy["stats"]["claim_band"]
    return (lo, hi), f"{round(lo * 100)}-{round(hi * 100)}% claimed: {CLAIM_NOTE}"


def coverage(ctx: AppContext, window_end: date | None = None) -> Coverage:
    clusters = clusters_of(ctx)
    win = coverage_window(ctx, window_end)
    rows = load_rows(ctx, win.first_week, win.last_week)
    by = tally_by(ctx, rows)

    def over(cid: str, weeks: list[date]) -> Tally:
        return sum_tallies(by[(cid, w)] for w in weeks if (cid, w) in by)

    all_weeks = win.week_list()
    per_cluster = {c.id: over(c.id, all_weeks) for c in clusters.all()}
    city = sum_tallies(per_cluster.values())

    flags = _look_here(ctx, win, by, [c.id for c in clusters.all()])
    flagged = {f.cluster_id: f for f in flags}

    out: list[ClusterCoverage] = []
    for c in clusters.all():
        t = per_cluster[c.id]
        s = _share(t, ctx)
        street = street_total(t.notched, t.unnotched, t.unsure, ctx.policy)
        out.append(
            ClusterCoverage(
                id=c.id,
                name=c.name,
                ward_nos=list(c.ward_nos),
                bites=hide_small(t.bites, ctx.policy),
                street_dog_bites=street,
                share=s,
                look_here=c.id in flagged,
                look_here_reason=flagged[c.id].reason if c.id in flagged else None,
                look_here_reason_data=flagged[c.id].data if c.id in flagged else None,
                simulated_bites=t.simulated,
            )
        )
    hide_across_areas(
        {
            c.id: (per_cluster[c.id].notched, per_cluster[c.id].unnotched, per_cluster[c.id].unsure)
            for c in clusters.all()
        },
        {c.id: cc.share for c, cc in zip(clusters.all(), out, strict=True)},
        ctx.policy,
    )
    for cc in out:
        if cc.share.unsure == WITHHELD:  # street total minus notched and not notched would give it back
            cc.street_dog_bites = WITHHELD
    all_rows = ctx.db.query(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN ward_no IS NULL AND location_status IN ('outside_jmc','cant_tell','no_place_heard') "
        "THEN 1 ELSE 0 END) AS unplaced FROM bite_reports WHERE bite_week BETWEEN ? AND ?",
        (win.first_week.isoformat(), win.last_week.isoformat()),
    )[0]
    band, label = _claim(ctx)
    total_bites, could_not_place = _outward_totals(
        ctx, int(all_rows["total"] or 0), int(all_rows["unplaced"] or 0)
    )
    return Coverage(
        window_start=win.first_week.isoformat(),
        window_end=win.end.isoformat(),
        city=_share(city, ctx),
        claim_band=band,
        claim_label=label,
        claim_label_key=CLAIM_LABEL_KEY,
        clusters=out,
        look_here=sorted(flagged),
        total_bites=total_bites,
        could_not_place=could_not_place,
        is_demo=is_demo(ctx),
    )


def _outward_totals(ctx: AppContext, total: int, unplaced: int) -> tuple[int | str, int | str]:
    """All bites and the ones without a ward, for outward views. The clusters' bites, the ones without a
    ward and the ones still waiting for a place add up to the total, so when the unplaced are 1 to 4 and
    the total is shown, the total minus the areas' bites would give them back: the total reads "hidden"."""
    shown_unplaced = hide_small(unplaced, ctx.policy)
    low = ctx.policy["stats"]["suppress_below"]
    if 0 < unplaced < low and total >= low:
        return WITHHELD, shown_unplaced
    return hide_small(total, ctx.policy), shown_unplaced


def _look_here(ctx: AppContext, win: Window, by: dict, cluster_ids: list[str]) -> list[lookhere.Flag]:
    n = ctx.policy["stats"]["lookhere_window_weeks"]
    weeks = win.week_list()
    # A part-way week would make the newest window look small, so the window ends at the last full week.
    end = len(weeks) if win.end.weekday() == 6 else len(weeks) - 1
    if end < n:
        raise StatsError("window_too_short", "The coverage window is shorter than the look-here window")
    latest = weeks[end - n : end]

    def counts(cid: str | None) -> lookhere.Counts:
        ids = cluster_ids if cid is None else [cid]
        t = sum_tallies(by[(i, w)] for i in ids for w in latest if (i, w) in by)
        return lookhere.Counts(t.notched, t.unnotched)

    return lookhere.evaluate(ctx.policy, counts(None), {cid: counts(cid) for cid in cluster_ids})


# --------------------------------------------------------------------------- wards
def _alert_counts(ctx: AppContext, win: Window) -> dict[int, int]:
    found = ctx.db.query(
        "SELECT ward_no, COUNT(*) AS n FROM alerts WHERE ward_no IS NOT NULL AND local_day BETWEEN ? AND ? "
        "GROUP BY ward_no",
        (win.first_week.isoformat(), win.end.isoformat()),
    )
    return {r["ward_no"]: r["n"] for r in found}


PLACE_TYPES = ("home_street", "school", "hospital", "bus_market", "other", "unknown")


def _place_cells(t: Tally, ctx: AppContext) -> dict[str, int | str]:
    """The six place types of one area as shown. They add up to its bites; hide_cells keeps a single "<5"
    from being worked out."""
    return hide_cells({k: getattr(t, k) for k in PLACE_TYPES}, ctx.policy)


def _ward_column(cells: dict[int, int], area_cell: int | str | None, ctx: AppContext) -> dict[int, int | str]:
    """One column of ward rows (bites, school, ...) that sits beside the area's own figure. The wards add up
    to the area, so a hidden ward must not be recoverable from the area figure minus the wards shown
    (hide_group). When the area figure is itself withheld, the wards must not add up to it either, so one
    ward is hidden too."""
    shown, _ = hide_group({str(w): v for w, v in cells.items()}, ctx.policy)
    if area_cell == WITHHELD and not any(isinstance(v, str) for v in shown.values()) and shown:
        shown[min(shown, key=lambda k: (cells[int(k)], k))] = WITHHELD
    return {int(k): v for k, v in shown.items()}


def ward_counts(ctx: AppContext) -> list[WardCounts]:
    """Counts only, never shares, for each ward in an analysis area over the coverage window. In each area
    the ward rows are hidden together with the area's own figures (bites, place types), so no "<5" row can
    be read back from the area total minus the wards that are shown."""
    clusters = clusters_of(ctx)
    win = coverage_window(ctx)
    per_ward: dict[int, Tally] = {}
    for row in load_rows(ctx, win.first_week, win.last_week):
        if row.ward_no is not None:
            per_ward.setdefault(row.ward_no, Tally()).add(row)
    alerts = _alert_counts(ctx, win)
    worked = {
        r["ward_no"]: r["last"]
        for r in ctx.db.query("SELECT ward_no, MAX(worked_on) AS last FROM worked_wards GROUP BY ward_no")
    }
    table = ward_table(ctx.settings.data_dir / "wards.geojson")
    out: list[WardCounts] = []
    for c in clusters.all():
        tallies = {w: per_ward.get(w, Tally()) for w in c.ward_nos}
        area = _place_cells(sum_tallies(tallies.values()), ctx)
        bites = _ward_column({w: t.bites for w, t in tallies.items()}, None, ctx)
        by_type = {
            k: _ward_column({w: getattr(t, k) for w, t in tallies.items()}, area[k], ctx)
            for k in ("school", "hospital", "bus_market")
        }
        for w in c.ward_nos:
            info = table.get(w)
            out.append(
                WardCounts(
                    ward_no=w,
                    ward_name=info.name if info else None,
                    cluster_id=c.id,
                    bites=bites[w],
                    school=by_type["school"][w],
                    hospital=by_type["hospital"][w],
                    bus_market=by_type["bus_market"][w],
                    alerts=hide_small(alerts.get(w, 0), ctx.policy),
                    worked_on=worked.get(w),
                )
            )
    out.sort(key=lambda r: r.ward_no)
    return out


def cluster_detail(ctx: AppContext, cluster_id: str) -> dict:
    cluster = clusters_of(ctx).get(cluster_id)
    win = coverage_window(ctx)
    rows = load_rows(ctx, win.first_week - timedelta(weeks=8), win.last_week)
    in_window = [r for r in rows if r.week >= win.first_week]
    members = set(cluster.ward_nos)
    t = Tally()
    for r in in_window:
        if r.ward_no in members:
            t.add(r)
    bites_shown = hide_small(t.bites, ctx.policy)
    per_10k = None
    if isinstance(bites_shown, int) and cluster.population > 0:
        per_10k = round(t.bites / cluster.population * 10_000, 1)
    table = ward_table(ctx.settings.data_dir / "wards.geojson")
    return {
        "id": cluster.id,
        "name": cluster.name,
        "ward_nos": list(cluster.ward_nos),
        "window_start": win.first_week.isoformat(),
        "window_end": win.end.isoformat(),
        "population": cluster.population,
        "wards_without_population": list(cluster.wards_without_population),
        "bites": bites_shown,
        "bites_per_10000": per_10k,
        "place_types": _place_cells(t, ctx),
        "weekly_bites": _weekly(ctx, win, in_window, members),
        "share": _share(t, ctx).model_dump(),
        "worked_wards": _worked(ctx, win, rows, members, table),
        "wards": [w.model_dump() for w in ward_counts(ctx) if w.cluster_id == cluster_id],
    }


def _weekly(ctx: AppContext, win: Window, rows, members: set[int]) -> list[dict]:
    """Bites per week of the coverage window in one area: counts only. The weeks add up to the area's bites,
    so hide_cells keeps one small week from being worked out from the total."""
    by_week = {w: 0 for w in win.week_list()}
    for r in rows:
        if r.ward_no in members and r.week in by_week:
            by_week[r.week] += r.n
    keys = {w.isoformat(): n for w, n in by_week.items()}
    shown = hide_cells(keys, ctx.policy)
    # the newest week is part-way unless the window ends on a Sunday, so a page can mark it
    part_way = win.last_week.isoformat() if win.end.weekday() != 6 else None
    return [{"week_start": week, "bites": shown[week], "partial": week == part_way} for week in keys]


def _worked(ctx: AppContext, win: Window, rows, members: set[int], table) -> list[dict]:
    """Bites in the lookhere-window weeks before and after each worked ward: an early signal only."""
    n = ctx.policy["stats"]["lookhere_window_weeks"]
    out = []
    found = ctx.db.query(
        "SELECT ward_no, MAX(worked_on) AS worked_on FROM worked_wards WHERE worked_on >= ? "
        "GROUP BY ward_no ORDER BY ward_no",
        ((win.first_week - timedelta(weeks=n)).isoformat(),),
    )
    for rec in found:
        ward = rec["ward_no"]
        if ward not in members:
            continue
        worked_week = clock.week_start(date.fromisoformat(rec["worked_on"]))
        before = sum(
            r.n
            for r in rows
            if r.ward_no == ward and worked_week - timedelta(weeks=n) <= r.week < worked_week
        )
        after_weeks = min(n, (win.last_week - worked_week).days // 7)
        after = sum(
            r.n
            for r in rows
            if r.ward_no == ward and worked_week < r.week <= worked_week + timedelta(weeks=n)
        )
        info = table.get(ward)
        out.append(
            {
                "ward_no": ward,
                "ward_name": info.name if info else None,
                "worked_on": rec["worked_on"],
                "bites_before": hide_small(before, ctx.policy),
                "before_weeks": n,
                "bites_after": hide_small(after, ctx.policy) if after_weeks > 0 else None,
                "after_weeks": after_weeks,
                "label": "early signal",
            }
        )
    return out


# ---------------------------------------------------------------------- oversight
def oversight_summary(ctx: AppContext) -> OversightSummary:
    win = coverage_window(ctx)
    start, end = win.first_week.isoformat(), win.end.isoformat()
    enrolled = ctx.db.one("SELECT COUNT(*) AS n FROM courses WHERE day0 BETWEEN ? AND ?", (start, end))["n"]
    ended = ctx.db.query(
        "SELECT c.status AS status FROM courses c "
        "JOIN (SELECT course_id, MAX(due_date) AS last_due FROM doses GROUP BY course_id) d "
        "ON d.course_id = c.id WHERE c.day0 BETWEEN ? AND ? AND d.last_due <= ?",
        (start, end, end),
    )
    completed = sum(1 for r in ended if r["status"] == "completed")
    cfg = ctx.policy["stats"]
    min_n = cfg["min_n_for_share"]
    # A percentage needs the same minimum number of courses as a notch share does.
    rate_shown = len(ended) >= min_n
    rate = round(completed / len(ended), 4) if rate_shown else None
    still = enrolled - len(ended)
    # The courses that have reached their last dose date and those still running add up to the enrolled.
    # With a rate beside them the first and the completed count are given by the rate, so they are shown.
    parts, enrolled_shown = hide_group(
        {"ended": len(ended), "still": still}, ctx.policy, open_cells={"ended"} if rate_shown else set()
    )
    completed_shown = completed if rate_shown else hide_small(completed, ctx.policy)
    if rate_shown:
        low, high = wilson(completed, len(ended), cfg["z"])
        basis = (
            f"{completed_shown} of {len(ended)} courses whose last dose date has passed "
            f"were marked complete by the clinic (95% interval {low * 100:.0f}% to {high * 100:.0f}%); "
            f"{parts['still']} still running are left out. No register total is held to compare with."
        )
    elif ended:
        basis = (
            f"n = {parts['ended']} of {min_n} needed: too few courses have reached their "
            "last dose date for a completion rate. No register total is held to compare with."
        )
    else:
        basis = "No course has reached its last dose date yet. No register total is held to compare with."
    placed = ctx.db.one(
        "SELECT SUM(CASE WHEN ward_no IS NOT NULL OR location_status = 'outside_jmc' THEN 1 ELSE 0 END) AS placed, "
        "SUM(CASE WHEN ward_no IS NOT NULL THEN 1 ELSE 0 END) AS inside FROM bite_reports "
        "WHERE bite_week BETWEEN ? AND ?",
        (start, win.last_week.isoformat()),
    )
    alerts = ctx.db.one(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'acted' THEN 1 ELSE 0 END) AS acted "
        "FROM alerts WHERE local_day BETWEEN ? AND ?",
        (start, end),
    )
    inside = int(placed["inside"] or 0)
    place_cells, with_place = hide_group(
        {"inside": inside, "outside": int(placed["placed"] or 0) - inside}, ctx.policy
    )
    acted = int(alerts["acted"] or 0)
    alert_cells, alerts_total = hide_group(
        {"acted": acted, "not_acted": int(alerts["total"] or 0) - acted}, ctx.policy
    )
    return OversightSummary(
        window_start=start,
        window_end=end,
        enrolled=enrolled_shown,
        completed=completed_shown,
        completion_rate=rate,
        completion_basis=basis,
        bites_with_place=with_place,
        bites_inside_jmc=place_cells["inside"],
        alerts_total=alerts_total,
        alerts_acted=alert_cells["acted"],
        coverage=coverage(ctx, win.end),
        is_demo=is_demo(ctx),
    )
