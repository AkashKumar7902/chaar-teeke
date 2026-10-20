"""The demo clock: where it stands, and the three jumps. The scheduler is replaced by a stub."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from chaarteeke import clock, scheduler
from chaarteeke.demo import controls


@pytest.fixture
def movable():
    """A demo clock that moves: the OffsetClock the app installs in demo mode."""
    with clock.use_clock(clock.OffsetClock()) as c:
        yield c


def stub_next_call(monkeypatch, at, kind="reminder"):
    monkeypatch.setattr(scheduler, "next_call_at", lambda ctx: at)
    monkeypatch.setattr(
        scheduler,
        "next_call",
        lambda ctx: None if at is None else SimpleNamespace(at=at, kind=kind, course_id="c", dose_no=2),
    )


def add_simulator_course(ctx, given=(1,)):
    day0 = clock.local_today(ctx.settings.local_tz)
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO clinics(id, name, short_name, default_regimen, default_lang, arv_status, is_demo) "
            "VALUES ('cln_demo_visitor','Demo visitor desk','Visitor desk','id_trc','hi','unverified',1)"
        )
        conn.execute(
            "INSERT INTO courses(id, clinic_id, register_no, regimen_id, day0, lang, status, eligibility, source, created_at, is_demo)"
            " VALUES ('crs_sim','cln_demo_visitor','J1','id_trc',?,'hi','active','adult','simulator',?,1)",
            (day0.isoformat(), clock.iso(clock.now())),
        )
        for n, off in enumerate((0, 3, 7, 28), start=1):
            conn.execute(
                "INSERT INTO doses(course_id, dose_no, offset_days, due_date, status, given_on, marked_by) VALUES (?,?,?,?,?,?,?)",
                (
                    "crs_sim",
                    n,
                    off,
                    (day0 + timedelta(days=off)).isoformat(),
                    "given" if n in given else "due",
                    day0.isoformat() if n in given else None,
                    "clinic" if n in given else None,
                ),
            )
    return day0


def test_state_on_the_moving_clock(ctx, movable, monkeypatch):
    soon = clock.now() + timedelta(hours=3)
    stub_next_call(monkeypatch, soon, "reminder")
    s = controls.state(ctx)
    assert s.offset_seconds == 0.0 and s.next_call_at == clock.iso(soon) and s.next_call_kind == "reminder"
    assert s.jump_targets == ["next_call", "evening_before_next_dose", "morning_after"]
    assert s.local_now.endswith("+05:30") and s.now.endswith("Z")


def test_state_with_nothing_scheduled(ctx, movable, monkeypatch):
    stub_next_call(monkeypatch, None)
    s = controls.state(ctx)
    assert s.next_call_at is None and s.next_call_kind is None


def test_state_on_a_fixed_clock_offers_no_jumps(ctx, monkeypatch):
    stub_next_call(monkeypatch, None)
    s = controls.state(ctx)
    assert s.offset_seconds == 0.0 and s.jump_targets == []


def test_jump_to_the_next_call_moves_the_clock_audits_and_announces(ctx, movable, monkeypatch):
    target = clock.now() + timedelta(hours=5)
    stub_next_call(monkeypatch, target)
    queue = ctx.bus.subscribe()
    s = controls.jump(ctx, "next_call")
    assert abs((clock.now() - target).total_seconds()) < 5
    assert s.offset_seconds == pytest.approx(5 * 3600, abs=5)
    ev = queue.get_nowait()
    assert ev.type == "clock" and set(ev.data) == {"now", "local_now"}
    assert ctx.db.one("SELECT 1 FROM audit WHERE action = 'demo.jump' AND actor = 'system:demo'")
    assert float(
        ctx.db.one("SELECT value FROM kv WHERE key = ?", (controls.KV_OFFSET,))["value"]
    ) == pytest.approx(5 * 3600, abs=5)


def test_a_call_already_due_does_not_move_the_clock(ctx, movable, monkeypatch):
    stub_next_call(monkeypatch, clock.now() - timedelta(minutes=1))
    controls.jump(ctx, "next_call")
    assert movable.offset < timedelta(seconds=5)


def test_nothing_scheduled_is_said_plainly(ctx, movable, monkeypatch):
    stub_next_call(monkeypatch, None)
    with pytest.raises(controls.DemoError) as e:
        controls.jump(ctx, "next_call")
    assert e.value.code == "nothing_scheduled"


def test_evening_before_the_next_dose_of_the_simulator_course(ctx, movable, monkeypatch):
    stub_next_call(monkeypatch, None)
    day0 = add_simulator_course(ctx)
    tz = ctx.settings.local_tz
    controls.jump(ctx, "evening_before_next_dose")  # dose 2 is day 3: the evening of day 2
    assert abs((clock.now() - clock.at_local(day0 + timedelta(days=2), "18:30", tz)).total_seconds()) < 5
    controls.jump(ctx, "evening_before_next_dose")  # that moment has passed, so dose 3 (day 7) is next
    assert abs((clock.now() - clock.at_local(day0 + timedelta(days=6), "18:30", tz)).total_seconds()) < 5


def test_morning_after_the_next_dose(ctx, movable, monkeypatch):
    stub_next_call(monkeypatch, None)
    day0 = add_simulator_course(ctx)
    controls.jump(ctx, "morning_after")
    assert (
        abs(
            (
                clock.now() - clock.at_local(day0 + timedelta(days=4), "09:30", ctx.settings.local_tz)
            ).total_seconds()
        )
        < 5
    )


def test_times_come_from_the_policy_not_the_code(ctx, movable, monkeypatch):
    stub_next_call(monkeypatch, None)
    day0 = add_simulator_course(ctx)
    ctx.policy.raw["calls"]["reminder_local_time"] = "17:15"
    controls.jump(ctx, "evening_before_next_dose")
    assert (
        abs(
            (
                clock.now() - clock.at_local(day0 + timedelta(days=2), "17:15", ctx.settings.local_tz)
            ).total_seconds()
        )
        < 5
    )


def test_the_clock_never_goes_back(ctx, movable, monkeypatch):
    stub_next_call(monkeypatch, None)
    add_simulator_course(ctx)
    controls.jump(ctx, "morning_after")
    before = clock.now()
    controls.jump(ctx, "evening_before_next_dose")
    assert clock.now() >= before


def test_no_simulator_course_or_no_dose_ahead(ctx, movable, monkeypatch):
    stub_next_call(monkeypatch, None)
    with pytest.raises(controls.DemoError) as e:
        controls.jump(ctx, "evening_before_next_dose")
    assert e.value.code == "no_simulator_course"
    add_simulator_course(ctx, given=(1, 2, 3, 4))
    with pytest.raises(controls.DemoError) as e:
        controls.jump(ctx, "morning_after")
    assert e.value.code == "no_dose_ahead"


def test_other_courses_do_not_count(ctx, movable, monkeypatch):
    stub_next_call(monkeypatch, None)
    add_simulator_course(ctx)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE courses SET source = 'demo'")
    with pytest.raises(controls.DemoError) as e:
        controls.jump(ctx, "morning_after")
    assert e.value.code == "no_simulator_course"


def test_refuses_a_clock_that_cannot_move(ctx, monkeypatch):
    stub_next_call(monkeypatch, clock.now() + timedelta(hours=1))
    with pytest.raises(controls.DemoError) as e:
        controls.jump(ctx, "next_call")
    assert e.value.code == "clock_fixed"


def test_refuses_pilot_mode(ctx, movable, monkeypatch):
    stub_next_call(monkeypatch, clock.now() + timedelta(hours=1))
    ctx.settings = ctx.settings.model_copy(update={"mode": "pilot"})
    for call in (
        lambda: controls.jump(ctx, "next_call"),
        lambda: controls.state(ctx),
        lambda: controls.reset_clock(ctx),
    ):
        with pytest.raises(controls.DemoError) as e:
            call()
        assert e.value.code == "not_demo"
    assert movable.offset == timedelta(0)


def test_refuses_an_unknown_target(ctx, movable):
    with pytest.raises(controls.DemoError) as e:
        controls.jump(ctx, "next_year")
    assert e.value.code == "bad_target"


def test_reset_clock_goes_back_to_real_time(ctx, movable, monkeypatch):
    stub_next_call(monkeypatch, clock.now() + timedelta(hours=4))
    controls.jump(ctx, "next_call")
    assert movable.offset > timedelta(hours=3)
    controls.reset_clock(ctx)
    assert movable.offset == timedelta(0)
    assert ctx.db.one("SELECT 1 FROM kv WHERE key = ?", (controls.KV_OFFSET,)) is None


def test_reset_demo_rebuilds_the_history_on_real_time(sctx, movable, monkeypatch):
    stub_next_call(monkeypatch, clock.now() + timedelta(hours=4))
    controls.jump(sctx, "next_call")
    report = controls.reset_demo(sctx)
    assert movable.offset == timedelta(0) and report.counts["clinics"] == 3
    assert sctx.db.one("SELECT COUNT(*) AS n FROM courses")["n"] == report.counts["courses"]
