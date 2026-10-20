"""Fixtures for the stats lane: a context with the analysis clusters, a report factory, a seeded demo."""

from __future__ import annotations

from datetime import timedelta

import pytest
from stats_helpers import T0, build_ctx

from chaarteeke import clock
from chaarteeke.ids import new_id
from chaarteeke.stats.clusters import Clusters


@pytest.fixture
def sctx(ctx):
    """The shared ctx fixture plus the analysis clusters and one test clinic."""
    ctx.clusters = Clusters.load()
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO clinics(id, name, short_name, ward_no, default_regimen, default_lang, arv_status, is_demo) "
            "VALUES ('cln_t','Test clinic','Test',NULL,'id_trc','hi','confirmed',1)"
        )
    return ctx


@pytest.fixture
def add_reports(sctx):
    """add(ward_no, animal, n=1, weeks_ago=0, **overrides): insert n bite reports into the current ctx."""

    def add(ward_no, animal, n=1, weeks_ago=0, **kw):
        week = clock.week_start(clock.local_today()) - timedelta(weeks=weeks_ago)
        cluster = sctx.clusters.for_ward(ward_no) if ward_no is not None else None
        row = dict(
            clinic_id="cln_t",
            bite_week=week.isoformat(),
            place_type="home_street",
            animal=animal,
            signs="no",
            ward_no=ward_no,
            location_status="matched" if ward_no is not None else "no_place_heard",
            cluster_id=cluster,
            source="demo",
            created_at=clock.iso(clock.now()),
            is_demo=1,
        )
        row.update(kw)
        ids = []
        with sctx.db.transaction() as conn:
            for _ in range(n):
                row["id"] = new_id("rep")
                cols = ", ".join(row)
                conn.execute(
                    f"INSERT INTO bite_reports({cols}) VALUES ({', '.join('?' * len(row))})",
                    tuple(row.values()),
                )
                ids.append(row["id"])
        return ids

    return add


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """A context holding the default demo seed on the fixed clock. Read-only: tests must not change it."""
    from chaarteeke.demo import seed as seeder

    with clock.use_clock(clock.FixedClock(T0)):
        ctx = build_ctx(tmp_path_factory.mktemp("seeded"))
        ctx.report = seeder.seed(ctx)
        yield ctx
        ctx.db.close()
