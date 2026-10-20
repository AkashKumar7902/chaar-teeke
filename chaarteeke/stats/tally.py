"""Reading bite reports into counts, shared by the coverage view and the exports."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import NamedTuple

from .. import clock
from ..context import AppContext
from .clusters import Clusters
from .errors import StatsError

STREET = ("notched", "unnotched", "unsure")  # animal values that mean a street dog
# animal values that say nothing about the notch. The call stores the animal only after the notch question,
# so a patient who chose "street dog" and then said nothing is stored as no_answer, the same as one who skipped
# every question. Either may be a street dog, so the Manski bounds count both as unknown.
UNANSWERED = ("animal_unknown", "no_answer")


class Window(NamedTuple):
    first_week: date  # Monday of the oldest week in the window
    last_week: date  # Monday of the week that holds `end`
    end: date
    weeks: int

    def week_list(self) -> list[date]:
        return [self.first_week + timedelta(weeks=i) for i in range(self.weeks)]


def window(ctx: AppContext, end: date | None, weeks: int) -> Window:
    end = end or clock.local_today(ctx.settings.local_tz)
    last = clock.week_start(end)
    return Window(last - timedelta(weeks=weeks - 1), last, end, weeks)


def coverage_window(ctx: AppContext, end: date | None = None) -> Window:
    return window(ctx, end, ctx.policy["stats"]["coverage_window_weeks"])


class Row(NamedTuple):
    ward_no: int | None
    week: date
    animal: str
    place_type: str
    location_status: str
    source: str
    n: int


def load_rows(ctx: AppContext, first_week: date, last_week: date) -> list[Row]:
    found = ctx.db.query(
        "SELECT ward_no, bite_week, animal, place_type, location_status, source, COUNT(*) AS n "
        "FROM bite_reports WHERE bite_week BETWEEN ? AND ? "
        "GROUP BY ward_no, bite_week, animal, place_type, location_status, source",
        (first_week.isoformat(), last_week.isoformat()),
    )
    return [
        Row(
            r["ward_no"],
            date.fromisoformat(r["bite_week"]),
            r["animal"],
            r["place_type"],
            r["location_status"],
            r["source"],
            r["n"],
        )
        for r in found
    ]


@dataclass
class Tally:
    bites: int = 0
    notched: int = 0
    unnotched: int = 0
    unsure: int = 0
    simulated: int = 0
    unanswered: int = 0  # animal not known or not answered: possibly a street dog, so unknown in the bounds
    school: int = 0
    hospital: int = 0
    bus_market: int = 0
    home_street: int = 0
    other: int = 0
    unknown: int = 0  # place type not said (no_answer), so the place types add up to bites

    def add(self, row: Row) -> None:
        self.bites += row.n
        if row.animal == "notched":
            self.notched += row.n
        elif row.animal == "unnotched":
            self.unnotched += row.n
        elif row.animal == "unsure":
            self.unsure += row.n
        elif row.animal in UNANSWERED:
            self.unanswered += row.n
        if row.source == "simulator":
            self.simulated += row.n
        if row.place_type in ("school", "hospital", "bus_market", "home_street", "other"):
            setattr(self, row.place_type, getattr(self, row.place_type) + row.n)
        else:
            self.unknown += row.n

    @property
    def street(self) -> int:
        return self.notched + self.unnotched + self.unsure


def clusters_of(ctx: AppContext) -> Clusters:
    if ctx.clusters is None:
        raise StatsError("clusters_not_loaded", "The analysis areas are not loaded")
    return ctx.clusters


def tally_by(ctx: AppContext, rows: list[Row]) -> dict[tuple[str, date], Tally]:
    """Reports that have a ward, as (cluster id, week) -> counts."""
    clusters = clusters_of(ctx)
    out: dict[tuple[str, date], Tally] = defaultdict(Tally)
    for row in rows:
        if row.ward_no is not None:
            out[(clusters.for_ward(row.ward_no), row.week)].add(row)
    return out


def sum_tallies(tallies) -> Tally:
    total = Tally()
    for t in tallies:
        for name in total.__dataclass_fields__:
            setattr(total, name, getattr(total, name) + getattr(t, name))
    return total


def is_demo(ctx: AppContext) -> bool:
    """True in demo mode, or whenever the database holds any demo-flagged report."""
    if ctx.settings.is_demo:
        return True
    return ctx.db.one("SELECT 1 FROM bite_reports WHERE is_demo = 1 LIMIT 1") is not None
