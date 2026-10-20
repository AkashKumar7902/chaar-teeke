"""The landing page's "Latest" list, read again after a reload (the live stream only brings what happens
while the page is open).

What is in it follows what each kind of row may show:

- the visitor's own phone (simulated calls, missed calls and bite reports) with the facts the live stream
  carries for them, newest first;
- an alert, simulated or real, as its summary only: area, ward, day, and the instant for a simulated one.
  A real alert is listed only for a view that may read alerts (`auth.READ["alerts"]`);
- never a real call, a real missed call or a real bite report, a phone number, a course or a clip.

The list is capped by policy `feed`: the newest `max_items` rows, of which at most `max_real_alerts` are real
alerts, so a long history of real alerts cannot push the visitor's own call out of sight.
"""

from __future__ import annotations

from . import alerts, clock
from .context import AppContext
from .models import FeedItem
from .stats.clusters import ward_table


def _day(ctx: AppContext, instant: str) -> str:
    return clock.parse_iso(instant).astimezone(clock.local_tz(ctx.settings.local_tz)).date().isoformat()


def recent(ctx: AppContext, *, may_read_alerts: bool) -> list[FeedItem]:
    cap = ctx.policy["feed"]["max_items"]
    wards = ward_table(ctx.settings.data_dir / "wards.geojson")
    rows: list[FeedItem] = []
    for r in ctx.db.query(
        "SELECT id, COALESCE(started_at, scheduled_for) AS at FROM calls"
        " WHERE transport = 'sim' AND status NOT IN ('scheduled', 'cancelled') ORDER BY at DESC, id DESC LIMIT ?",
        (cap,),
    ):
        rows.append(FeedItem(kind="call", id=r["id"], at=r["at"], day=_day(ctx, r["at"]), simulated=True))
    for r in ctx.db.query(
        "SELECT id, at FROM missed_calls WHERE transport = 'sim' ORDER BY at DESC, id DESC LIMIT ?", (cap,)
    ):
        rows.append(
            FeedItem(kind="missed_call", id=r["id"], at=r["at"], day=_day(ctx, r["at"]), simulated=True)
        )
    for r in ctx.db.query(
        "SELECT id, created_at, cluster_id, ward_no, animal, location_status FROM bite_reports"
        " WHERE source = 'simulator' ORDER BY created_at DESC, id DESC LIMIT ?",
        (cap,),
    ):
        ward = wards.get(r["ward_no"]) if r["ward_no"] else None
        rows.append(
            FeedItem(
                kind="report", id=r["id"], at=r["created_at"], day=_day(ctx, r["created_at"]),
                cluster_id=r["cluster_id"], ward_no=r["ward_no"], ward_name=ward.name if ward else None,
                animal=r["animal"], location_status=r["location_status"], simulated=True,
            )
        )  # fmt: skip
    if may_read_alerts:
        real = 0
        for a in alerts.list_alerts(ctx):  # newest first
            if not a.simulated:
                real += 1
                if real > ctx.policy["feed"]["max_real_alerts"]:
                    continue
            rows.append(
                FeedItem(
                    kind="alert", id=a.id, at=a.made_at, day=a.local_day, cluster_id=a.cluster_id,
                    ward_no=a.ward_no, ward_name=a.ward_name, location_status=a.location_status,
                    simulated=a.simulated,
                )
            )  # fmt: skip
    # Newest day first; within a day, the rows with an instant newest first, then the rows with a day only.
    rows.sort(key=lambda item: (item.day, item.at or ""), reverse=True)
    return rows[:cap]
