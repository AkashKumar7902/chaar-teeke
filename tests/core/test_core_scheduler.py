"""Planning, job keys, the single writer, the calling window, retries and the loop."""

from __future__ import annotations

import asyncio
import copy
import sqlite3
from datetime import timedelta

import pytest
from core_support import FakeRunner

from chaarteeke import clock, doses, enrol
from chaarteeke.policy import Policy
from chaarteeke.scheduler import (
    Scheduler,
    in_window,
    next_call,
    next_call_at,
    next_window_time,
)

TZ = "Asia/Kolkata"


def local(day, hhmm):
    return clock.at_local(day, hhmm, TZ)


def keys(world, cid):
    return [c["job_key"] for c in world.calls(cid)]


# ------------------------------------------------------------------ planning
@pytest.mark.parametrize(
    ("regimen", "dose_nos"),
    [("id_trc", [2, 3, 4]), ("im_essen", [2, 3, 4, 5]), ("reexposure", [2])],
)
async def test_reminder_is_planned_for_each_dose_after_the_first(
    ctx, world, fixed_clock, runner, regimen, dose_nos
):
    cid = world.active(regimen=regimen)
    sched = Scheduler(ctx, "a")
    for n in dose_nos:
        due = world.dose_date(cid, n)
        world.goto(due - timedelta(days=1), "10:00", fixed_clock)
        await sched.tick()
        row = [c for c in world.calls(cid, "reminder") if c["dose_no"] == n]
        assert len(row) == 1, (regimen, n)
        assert row[0]["job_key"] == f"reminder:{cid}:{n}:{(due - timedelta(days=1)).isoformat()}"
        assert row[0]["scheduled_for"] == clock.iso(local(due - timedelta(days=1), "18:30"))
        assert row[0]["status"] == "scheduled"  # 10:00 is before 18:30
        # at 18:30 it is placed
        world.goto(due - timedelta(days=1), "18:30", fixed_clock)
        report = await sched.tick()
        assert report.dispatched >= 1
        assert row[0]["id"] in runner.placed
    # dose 1 never gets a reminder
    assert all(c["dose_no"] != 1 for c in world.calls(cid, "reminder"))


async def test_morning_after_is_planned_for_an_unmarked_dose_only(ctx, world, fixed_clock, runner):
    unmarked = world.active(day0=world.today() - timedelta(days=3), register_no="U")
    marked = world.active(day0=world.today() - timedelta(days=3), register_no="M")
    doses.mark_given(ctx, marked, 2, "clinic", world.today(), "staff:a")
    sched = Scheduler(ctx, "a")
    next_day = world.today() + timedelta(days=1)
    world.goto(next_day, "09:00", fixed_clock)
    await sched.tick()
    rows = world.calls(unmarked, "missed_dose")
    assert len(rows) == 1 and rows[0]["dose_no"] == 2
    assert rows[0]["job_key"] == f"missed_dose:{unmarked}:2:{next_day.isoformat()}"
    assert rows[0]["scheduled_for"] == clock.iso(local(next_day, "09:30"))
    assert rows[0]["status"] == "scheduled" and rows[0]["id"] not in runner.placed
    assert world.calls(marked, "missed_dose") == []
    world.goto(next_day, "09:30", fixed_clock)
    await sched.tick()
    assert rows[0]["id"] in runner.placed


async def test_no_reminders_without_consent_or_after_the_number_is_gone(ctx, world, fixed_clock, runner):
    no_consent = world.active(consent=False)
    gone = world.active()
    enrol.withdraw(ctx, gone, "patient:x", "key 9")
    world.goto(world.dose_date(no_consent, 2) - timedelta(days=1), "19:00", fixed_clock)
    report = await Scheduler(ctx, "a").tick()
    assert report.planned == 0
    assert world.calls(no_consent, "reminder") == [] and world.calls(gone, "reminder") == []


async def test_planning_ignores_other_courses_states(ctx, world, fixed_clock, runner):
    waiting = world.course()
    stopped = world.active()
    enrol.stop(ctx, stopped, "left", "staff:a")
    world.goto(world.today() + timedelta(days=2), "19:00", fixed_clock)
    assert (await Scheduler(ctx, "a").tick()).planned == 0
    assert world.calls(waiting) == []


async def test_job_keys_make_planning_idempotent(ctx, world, fixed_clock, runner):
    cid = world.active()
    world.goto(world.dose_date(cid, 2) - timedelta(days=1), "10:00", fixed_clock)
    sched = Scheduler(ctx, "a")
    first = await sched.tick()
    second = await sched.tick()
    third = await Scheduler(ctx, "a").tick()  # a restart: same holder, new object
    assert (first.planned, second.planned, third.planned) == (1, 0, 0)
    assert len(world.calls(cid, "reminder")) == 1
    assert len(set(keys(world, cid))) == len(keys(world, cid))


async def test_two_schedulers_only_one_runs(ctx, world, fixed_clock, runner):
    cid = world.active()
    world.goto(world.dose_date(cid, 2) - timedelta(days=1), "19:00", fixed_clock)
    a, b = Scheduler(ctx, "a"), Scheduler(ctx, "b")
    ra, rb = await a.tick(), await b.tick()
    assert (ra.ran, rb.ran) == (True, False)
    assert rb.planned == rb.dispatched == 0
    # the writer keeps the lock tick after tick, and the call went out exactly once
    assert (await a.tick()).ran and not (await b.tick()).ran
    assert len(runner.placed) == 2  # the first call and the reminder, each once
    assert len(set(runner.placed)) == 2
    assert ctx.db.one("SELECT holder FROM locks WHERE name='scheduler'")["holder"] == "a"


async def test_a_dead_writer_is_replaced_after_the_heartbeat_goes_stale(ctx, world, fixed_clock, runner):
    a, b = Scheduler(ctx, "a", lock_stale_seconds=30), Scheduler(ctx, "b", lock_stale_seconds=30)
    assert (await a.tick()).ran
    fixed_clock.advance(timedelta(seconds=29))
    assert not (await b.tick()).ran
    fixed_clock.advance(timedelta(seconds=2))
    assert (await b.tick()).ran
    assert not (await a.tick()).ran
    # a released lock is taken at once
    b.release()
    assert (await a.tick()).ran


async def test_a_lock_stamped_in_the_future_is_taken_over_and_a_due_call_is_placed(
    ctx, world, fixed_clock, runner, caplog
):
    # a writer ran under a jumped clock; the clock then moved back, so its heartbeat is days ahead
    ahead = Scheduler(ctx, "ahead", lock_stale_seconds=30)
    jumped = fixed_clock.now()
    fixed_clock.advance(timedelta(days=10))
    assert (await ahead.tick()).ran
    fixed_clock.set(jumped)
    cid = world.active()
    world.settle()
    new = Scheduler(ctx, "new", lock_stale_seconds=30)
    with caplog.at_level("WARNING", logger="chaarteeke.scheduler"):
        report = await new.tick()
    assert report.ran and report.dispatched == 1
    assert runner.placed == [world.calls(cid, "first")[0]["id"]]
    assert ctx.db.one("SELECT holder FROM locks WHERE name='scheduler'")["holder"] == "new"
    assert "takes over the lock" in caplog.text
    assert "scheduler.takeover" in world.audit_actions()


async def test_a_heartbeat_a_little_ahead_is_a_live_writer_not_a_dead_one(ctx, world, fixed_clock, runner):
    a, b = Scheduler(ctx, "a", lock_stale_seconds=30), Scheduler(ctx, "b", lock_stale_seconds=30)
    fixed_clock.advance(timedelta(seconds=20))
    assert (await a.tick()).ran
    fixed_clock.advance(timedelta(seconds=-20))  # b's clock is 20 s behind a's: still inside the lock time
    assert not (await b.tick()).ran


async def test_a_takeover_never_plans_or_places_a_call_twice(ctx, world, fixed_clock, runner):
    cid = world.active()
    world.goto(world.dose_date(cid, 2) - timedelta(days=1), "19:00", fixed_clock)
    a, b = Scheduler(ctx, "a", lock_stale_seconds=30), Scheduler(ctx, "b", lock_stale_seconds=30)
    assert (await a.tick()).dispatched == 2  # the first call and the reminder
    fixed_clock.advance(timedelta(seconds=31))
    took = await b.tick()
    assert took.ran and took.planned == 0 and took.dispatched == 0
    assert len(runner.placed) == len(set(runner.placed)) == 2
    assert len(keys(world, cid)) == len(set(keys(world, cid))) == 2


async def test_a_new_writer_closes_calls_the_old_one_left_in_the_air(ctx, world, fixed_clock):
    cid = world.active()
    call = world.calls(cid, "first")[0]
    ctx.db.connect().execute("UPDATE calls SET status='ringing' WHERE id=?", (call["id"],))
    await Scheduler(ctx, "new").tick()
    row = ctx.db.one("SELECT status, error FROM calls WHERE id=?", (call["id"],))
    assert row["status"] == "failed" and "restarted" in row["error"]


async def test_scheduler_needs_one_retry_gap_per_retry(ctx):
    raw = copy.deepcopy(ctx.policy.raw)
    raw["calls"]["retry_gaps_min"] = [20]
    bad = AppContextWith(ctx, Policy(raw, "x"))
    with pytest.raises(ValueError, match="retry_gaps_min"):
        Scheduler(bad)


def AppContextWith(ctx, policy):  # noqa: N802
    from dataclasses import replace

    return replace(ctx, policy=policy)


# ------------------------------------------------------------ calling window
def test_window_boundaries(ctx, fixed_clock):
    day = clock.local_today(TZ)
    assert not in_window(ctx, local(day, "08:59"))
    assert in_window(ctx, local(day, "09:00"))
    assert in_window(ctx, local(day, "19:59"))
    assert not in_window(ctx, local(day, "20:00"))
    nxt = day + timedelta(days=1)
    assert next_window_time(ctx, local(day, "08:00")) == local(day, "09:00")
    assert next_window_time(ctx, local(day, "12:00")) == local(day, "12:00")
    assert next_window_time(ctx, local(day, "20:30")) == local(nxt, "09:00")


async def test_a_call_due_outside_the_window_waits_for_the_next_window_start(ctx, world, fixed_clock, runner):
    cid = world.active()
    first = world.calls(cid, "first")[0]
    sched = Scheduler(ctx, "a")
    day = world.today()
    # the first call came due at 20:30
    world.goto(day, "20:30", fixed_clock)
    report = await sched.tick()
    assert report.dispatched == 0 and runner.placed == []
    for hhmm in ("21:00", "23:59"):
        world.goto(day, hhmm, fixed_clock)
        await sched.tick()
    world.goto(day + timedelta(days=1), "08:59", fixed_clock)
    await sched.tick()
    assert runner.placed == []
    assert ctx.db.one("SELECT status FROM calls WHERE id=?", (first["id"],))["status"] == "scheduled"
    world.goto(day + timedelta(days=1), "09:00", fixed_clock)
    report = await sched.tick()
    assert runner.placed == [first["id"]] and report.dispatched == 1


# ---------------------------------------- the browser phone and the calling window
def with_policy(ctx, **calls):
    raw = copy.deepcopy(ctx.policy.raw)
    raw["calls"].update(calls)
    return AppContextWith(ctx, Policy(raw, "x"))


def sim_course(ctx, world, number="sim:visitor1"):
    """A course whose first call goes over the browser phone: in demo mode the missed call links itself, in
    pilot mode the clinic links it by hand. Returns its first call."""
    res = enrol.record_missed_call(ctx, number, "sim", clock.now())
    if res.status == "linked":
        return world.calls(res.course_id, "first")[0]
    cid = world.course(register_no="SIM")
    enrol.link(ctx, res.missed_call_id, cid, "staff:test")
    return world.calls(cid, "first")[0]


async def test_in_demo_a_browser_phone_call_is_placed_outside_the_window_and_a_real_line_call_is_not(
    ctx, world, fixed_clock, runner
):
    real = world.calls(world.active(register_no="REAL"), "first")[0]  # the real line
    browser = sim_course(ctx, world)
    assert browser["transport"] == "sim" and real["transport"] == "ari"
    world.goto(world.today(), "22:30", fixed_clock)  # the window closed at 20:00
    assert not in_window(ctx, clock.now())
    report = await Scheduler(ctx, "a").tick()
    assert runner.placed == [browser["id"]] and report.dispatched == 1
    assert ctx.db.one("SELECT status FROM calls WHERE id=?", (real["id"],))["status"] == "scheduled"
    world.goto(world.today() + timedelta(days=1), "03:00", fixed_clock)  # and before it opens
    assert (await Scheduler(ctx, "a").tick()).dispatched == 0  # the real call still waits
    world.goto(world.today(), "09:00", fixed_clock)
    assert (await Scheduler(ctx, "a").tick()).dispatched == 1
    assert runner.placed == [browser["id"], real["id"]]


async def test_the_policy_can_make_the_window_bind_the_browser_phone_too(ctx, world, fixed_clock, runner):
    strict = with_policy(ctx, window_applies_to_simulator=True)
    browser = sim_course(strict, world)
    world.goto(world.today(), "22:30", fixed_clock)
    assert (await Scheduler(strict, "a").tick()).dispatched == 0 and runner.placed == []
    world.goto(world.today() + timedelta(days=1), "09:00", fixed_clock)
    assert (await Scheduler(strict, "a").tick()).dispatched == 1
    assert runner.placed == [browser["id"]]


async def test_in_pilot_the_window_binds_every_call_whatever_the_policy_says(ctx, world, fixed_clock, runner):
    ctx.settings = ctx.settings.model_copy(update={"mode": "pilot"})
    assert ctx.policy["calls"]["window_applies_to_simulator"] is False
    browser = sim_course(ctx, world)
    world.goto(world.today(), "22:30", fixed_clock)
    assert (await Scheduler(ctx, "a").tick()).dispatched == 0 and runner.placed == []
    world.goto(world.today() + timedelta(days=1), "09:00", fixed_clock)
    assert (await Scheduler(ctx, "a").tick()).dispatched == 1 and runner.placed == [browser["id"]]


def test_the_demo_clock_jump_to_the_next_call_does_not_wait_for_the_window_for_a_browser_phone(
    ctx, world, fixed_clock
):
    world.goto(world.today(), "22:30", fixed_clock)
    sim_course(ctx, world)
    assert next_call_at(ctx) == clock.now() + timedelta(seconds=ctx.policy["calls"]["first_call_delay_s"])
    world.active(register_no="REAL")
    ctx.db.connect().execute("UPDATE calls SET status='completed' WHERE transport='sim'")
    assert next_call_at(ctx) == local(world.today() + timedelta(days=1), "09:00")  # a real-line call waits


async def test_a_browser_phone_reminder_waits_for_its_hour_but_not_for_the_window(
    ctx, world, fixed_clock, runner
):
    """Only the window is lifted: the reminder is still planned at 18:30 the evening before the dose."""
    browser = sim_course(ctx, world)
    ctx.db.connect().execute("UPDATE calls SET status='completed' WHERE id=?", (browser["id"],))
    ctx.db.connect().execute(
        "UPDATE courses SET consent_reminders_at=? WHERE id=?", (clock.iso(clock.now()), browser["course_id"])
    )
    due = world.dose_date(browser["course_id"], 2)
    sched = Scheduler(ctx, "a")
    world.goto(due - timedelta(days=1), "10:00", fixed_clock)
    await sched.tick()
    reminder = world.calls(browser["course_id"], "reminder")[0]
    assert reminder["scheduled_for"] == clock.iso(local(due - timedelta(days=1), "18:30"))
    assert reminder["id"] not in runner.placed  # 10:00 is before 18:30
    world.goto(due - timedelta(days=1), "18:30", fixed_clock)
    await sched.tick()
    assert reminder["id"] in runner.placed


async def test_nothing_is_dialled_before_the_window_opens(ctx, world, fixed_clock, runner):
    world.active()
    world.goto(world.today(), "05:00", fixed_clock)
    assert (await Scheduler(ctx, "a").tick()).dispatched == 0


async def test_a_reminder_that_misses_the_evening_window_is_never_sent_on_the_dose_day(
    ctx, world, fixed_clock, runner
):
    """The words are "Tomorrow, <date>": said on the dose day itself they would tell the patient the dose is
    not today. So the call is cancelled with a reason, never placed the next morning."""
    cid = world.active()
    due = world.dose_date(cid, 2)
    sched = Scheduler(ctx, "a")
    world.goto(due - timedelta(days=1), "20:10", fixed_clock)
    assert (await sched.tick()).dispatched == 0  # after 20:00 the first call and the reminder both wait
    reminder = world.calls(cid, "reminder")[0]
    world.goto(due, "09:00", fixed_clock)
    report = await sched.tick()
    assert reminder["id"] not in runner.placed and report.cancelled >= 1
    row = ctx.db.one("SELECT status, error FROM calls WHERE id=?", (reminder["id"],))
    assert row["status"] == "cancelled" and "passed" in row["error"]


async def test_no_reminder_is_planned_on_the_dose_day_itself(ctx, world, fixed_clock, runner):
    cid = world.active()
    world.goto(world.dose_date(cid, 2), "10:00", fixed_clock)
    report = await Scheduler(ctx, "a").tick()
    assert report.planned == 0 and world.calls(cid, "reminder") == []


class LineRunner:
    """A runner that notes how many calls were in the air at once, per transport."""

    def __init__(self, ctx, hold=3):
        self.ctx, self.hold = ctx, hold
        self.live: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.placed: list[str] = []

    async def place(self, call_id):
        transport = self.ctx.db.one("SELECT transport FROM calls WHERE id=?", (call_id,))["transport"]
        self.placed.append(call_id)
        self.live[transport] = self.live.get(transport, 0) + 1
        self.peak[transport] = max(self.peak.get(transport, 0), self.live[transport])
        for _ in range(self.hold):
            await asyncio.sleep(0)
        self.live[transport] -= 1
        with self.ctx.db.transaction() as conn:
            conn.execute("UPDATE calls SET status='completed' WHERE id=?", (call_id,))


async def test_a_single_phone_line_carries_one_call_at_a_time(ctx, world, fixed_clock):
    """One dongle, one voice channel: six reminders due at 18:30 must not all be dialled together."""
    ctx.runner = runner = LineRunner(ctx)
    ids = [world.active(register_no=f"L{i}") for i in range(3)]
    world.settle()
    report = await Scheduler(ctx, "a").tick()
    assert report.dispatched == 3 and runner.peak == {"ari": 1}
    assert all(world.calls(i, "first")[0]["status"] == "completed" for i in ids)


async def test_waiting_calls_on_a_busy_line_stay_scheduled_for_the_next_pass(ctx, world, fixed_clock):
    ctx.runner = runner = LineRunner(ctx)
    ids = [world.active(register_no=f"L{i}") for i in range(2)]
    world.settle()
    sched = Scheduler(ctx, "a")
    first = await sched.tick(wait=False)
    assert first.dispatched == 1
    assert sorted(world.calls(i, "first")[0]["status"] for i in ids) == ["dialing", "scheduled"]
    await sched.wait_idle()
    assert (await sched.tick(wait=False)).dispatched == 1
    await sched.wait_idle()
    assert runner.peak == {"ari": 1} and len(runner.placed) == 2


async def test_browser_phones_are_not_held_back_by_each_other(ctx, world, fixed_clock):
    ctx.runner = runner = LineRunner(ctx)
    for i in range(3):
        assert enrol.record_missed_call(ctx, f"sim:s{i}", "sim", clock.now()).status == "linked"
    world.settle()
    report = await Scheduler(ctx, "a").tick()
    assert report.dispatched == 3 and runner.peak == {"sim": 3}


async def test_the_lock_is_kept_fresh_while_a_long_call_is_awaited(ctx, world, fixed_clock):
    gate = asyncio.Event()

    class Slow:
        async def place(self, call_id):
            await gate.wait()
            with ctx.db.transaction() as conn:
                conn.execute("UPDATE calls SET status='completed' WHERE id=?", (call_id,))

    ctx.runner = Slow()
    world.active()
    world.settle()
    sched = Scheduler(ctx, "a", loop_seconds=0.01, lock_stale_seconds=30)
    task = asyncio.create_task(sched.tick())  # wait=True: the tick lasts as long as the call
    await asyncio.sleep(0.05)
    fixed_clock.advance(timedelta(seconds=45))  # a long call: past the staleness limit
    await asyncio.sleep(0.05)
    assert not (await Scheduler(ctx, "b", lock_stale_seconds=30).tick()).ran  # still a's lock
    gate.set()
    await task


# --------------------------------------------------------------------- retries
async def test_no_answer_is_retried_with_the_policy_gaps_and_then_stops(ctx, world, fixed_clock):
    ctx.runner = FakeRunner(ctx, outcome="no_answer")
    cid = world.active()
    sched = Scheduler(ctx, "a")
    world.goto(world.today(), "10:05", fixed_clock)
    await sched.tick()  # the first attempt ends with no answer
    assert world.calls(cid, "first")[0]["status"] == "no_answer"
    await sched.tick()  # the next pass plans the retry as a new row, 20 minutes after the attempt ended
    rows = world.calls(cid, "first")
    assert [r["job_key"] for r in rows] == [f"first:{cid}", f"first:{cid}:a2"]
    assert rows[1]["attempt"] == 2 and rows[1]["status"] == "scheduled"
    assert rows[1]["scheduled_for"] == clock.iso(clock.now() + timedelta(minutes=20))
    fixed_clock.advance(timedelta(minutes=19))
    await sched.tick()
    assert len(ctx.runner.placed) == 1
    fixed_clock.advance(timedelta(minutes=1))
    await sched.tick()
    assert len(ctx.runner.placed) == 2
    await sched.tick()
    rows = world.calls(cid, "first")
    assert [r["job_key"] for r in rows][-1] == f"first:{cid}:a3"
    assert rows[2]["scheduled_for"] == clock.iso(clock.now() + timedelta(minutes=120))
    fixed_clock.advance(timedelta(minutes=119))
    await sched.tick()
    assert len(ctx.runner.placed) == 2
    fixed_clock.advance(timedelta(minutes=1))
    await sched.tick()
    assert len(ctx.runner.placed) == 3
    # three attempts is the limit
    for _ in range(3):
        fixed_clock.advance(timedelta(hours=1))
        await sched.tick()
    assert [r["attempt"] for r in world.calls(cid, "first")] == [1, 2, 3]
    assert len(ctx.runner.placed) == 3


async def test_a_reminder_gets_all_its_attempts_on_the_same_evening(ctx, world, fixed_clock):
    ctx.runner = FakeRunner(ctx, outcome="no_answer")
    cid = world.active()
    sched = Scheduler(ctx, "a")
    due = world.dose_date(cid, 2)
    evening = due - timedelta(days=1)
    world.goto(evening, "18:30", fixed_clock)
    for _ in range(12):
        await sched.tick()
        fixed_clock.advance(timedelta(minutes=10))
    rows = [r for r in world.calls(cid, "reminder") if r["dose_no"] == 2]
    assert [r["attempt"] for r in rows] == [1, 2, 3]
    assert [r["status"] for r in rows] == ["no_answer"] * 3
    window_end = local(evening, "20:00")
    assert all(clock.parse_iso(r["ended_at"]) < window_end for r in rows)
    # the gaps are the reminder's own, not the first call's 20 and 120 minutes
    assert clock.parse_iso(rows[2]["scheduled_for"]) - clock.parse_iso(rows[1]["ended_at"]) == timedelta(
        minutes=60
    )


async def test_other_calls_keep_the_general_retry_gaps(ctx, world, fixed_clock):
    ctx.runner = FakeRunner(ctx, outcome="no_answer")
    cid = world.active()
    sched = Scheduler(ctx, "a")
    world.goto(world.today(), "10:05", fixed_clock)
    await sched.tick()
    await sched.tick()
    fixed_clock.advance(timedelta(minutes=20))
    await sched.tick()
    await sched.tick()
    third = world.calls(cid, "first")[2]
    assert clock.parse_iso(third["scheduled_for"]) == clock.now() + timedelta(minutes=120)


async def test_every_retry_gap_list_needs_a_gap_for_every_retry(ctx):
    raw = copy.deepcopy(ctx.policy.raw)
    raw["calls"]["retry_gaps_min_by_kind"] = {"reminder": [20]}
    with pytest.raises(ValueError, match="retry_gaps_min_by_kind|reminder"):
        Scheduler(AppContextWith(ctx, Policy(raw, "x")))


async def test_failed_calls_are_retried_too_and_completed_ones_are_not(ctx, world, fixed_clock):
    ctx.runner = FakeRunner(ctx, outcome="failed")
    failing = world.active(register_no="F")
    world.settle()
    sched = Scheduler(ctx, "a")
    await sched.tick()
    await sched.tick()
    assert len(world.calls(failing, "first")) == 2
    ctx.runner = FakeRunner(ctx, outcome="completed")
    ok = world.active(register_no="OK")
    world.settle()
    await sched.tick()
    await sched.tick()
    assert len(world.calls(ok, "first")) == 1


async def test_a_retry_after_the_window_waits_and_a_stopped_course_gets_none(ctx, world, fixed_clock):
    ctx.runner = FakeRunner(ctx, outcome="no_answer")
    cid = world.active()
    sched = Scheduler(ctx, "a")
    today = world.today()
    world.goto(today, "19:50", fixed_clock)
    await sched.tick()
    await sched.tick()
    retry = world.calls(cid, "first")[1]
    assert retry["scheduled_for"] == clock.iso(local(today, "20:10"))
    fixed_clock.advance(timedelta(minutes=30))
    await sched.tick()
    assert len(ctx.runner.placed) == 1  # 20:20 is outside the window
    world.goto(today + timedelta(days=1), "09:00", fixed_clock)
    await sched.tick()
    assert len(ctx.runner.placed) == 2

    other = world.active(register_no="S")
    ctx.runner.outcome = None
    stop_me = world.calls(other, "first")[0]
    ctx.db.connect().execute(
        "UPDATE calls SET status='no_answer', ended_at=? WHERE id=?", (clock.iso(clock.now()), stop_me["id"])
    )
    enrol.stop(ctx, other, "left", "staff:a")
    await sched.tick()
    await sched.tick()
    assert len(world.calls(other, "first")) == 1


async def test_retries_of_a_retry_keep_one_suffix(ctx, world, fixed_clock):
    ctx.runner = FakeRunner(ctx, outcome="no_answer")
    cid = world.active()
    sched = Scheduler(ctx, "a")
    world.goto(world.today(), "10:05", fixed_clock)
    await sched.tick()
    await sched.tick()
    fixed_clock.advance(timedelta(minutes=20))
    await sched.tick()
    await sched.tick()
    assert [r["job_key"] for r in world.calls(cid, "first")] == [
        f"first:{cid}",
        f"first:{cid}:a2",
        f"first:{cid}:a3",
    ]


# --------------------------------------------------- cancelling stale calls
async def test_a_call_for_a_dose_already_given_is_cancelled_not_placed(ctx, world, fixed_clock, runner):
    cid = world.active(day0=world.today() - timedelta(days=3))
    sched = Scheduler(ctx, "a")
    world.goto(world.today() + timedelta(days=1), "09:30", fixed_clock)
    await sched.tick()  # plans and places the morning-after call for dose 2, and the first call
    ctx.db.connect().execute("DELETE FROM calls")
    runner.placed.clear()
    # a call row exists for a dose that has since been marked given by hand in the register
    ctx.db.connect().execute(
        "UPDATE doses SET status='given', given_on=? WHERE course_id=? AND dose_no=2",
        (world.today().isoformat(), cid),
    )
    with ctx.db.transaction() as conn:
        enrol.new_call(
            conn, course_id=cid, kind="missed_dose", job_key="x:1", scheduled_for=clock.now(), dose_no=2
        )
    report = await sched.tick()
    assert report.cancelled == 1 and runner.placed == []
    row = world.calls(cid)[0]
    assert row["status"] == "cancelled" and "marked as given" in row["error"]


async def test_stale_and_closed_calls_are_cancelled_with_a_reason(ctx, world, fixed_clock, runner):
    cid = world.active(day0=world.today() - timedelta(days=3))
    with ctx.db.transaction() as conn:
        enrol.new_call(
            conn, course_id=cid, kind="reminder", job_key="old:1", scheduled_for=clock.now(), dose_no=2
        )
        conn.execute("UPDATE calls SET status='cancelled' WHERE kind='first'")
    world.goto(world.today() + timedelta(days=1), "10:00", fixed_clock)  # dose 2 was due yesterday
    await Scheduler(ctx, "a").tick()
    row = ctx.db.one("SELECT status, error FROM calls WHERE job_key='old:1'")
    assert row["status"] == "cancelled" and "passed" in row["error"]

    gone = world.active()
    world.settle()
    with ctx.db.transaction() as conn:
        conn.execute("DELETE FROM contacts WHERE course_id=?", (gone,))
    await Scheduler(ctx, "a").tick()
    assert [c["status"] for c in world.calls(gone, "first")] == ["cancelled"]
    stuck = {c["id"] for c in world.calls(gone)} | {c["id"] for c in world.calls(cid, "reminder")}
    assert not stuck & set(runner.placed)


# ------------------------------------------------------------- the runner
@pytest.mark.parametrize("is_async", [True, False])
async def test_runner_may_be_async_or_plain(ctx, world, is_async):
    ctx.runner = FakeRunner(ctx, is_async=is_async)
    cid = world.active()
    world.settle()
    report = await Scheduler(ctx, "a").tick()
    assert report.dispatched == 1 and ctx.runner.placed == [world.calls(cid, "first")[0]["id"]]
    assert world.calls(cid, "first")[0]["status"] == "completed"


async def test_one_bad_call_is_marked_failed_and_the_rest_still_go_out(ctx, world, fixed_clock):
    bad = world.active(register_no="BAD")
    good = world.active(register_no="GOOD")
    world.settle()
    bad_call = world.calls(bad, "first")[0]["id"]
    ctx.runner = FakeRunner(ctx, fail_on={bad_call})
    report = await Scheduler(ctx, "a").tick()
    assert report.ran and report.dispatched == 2 and report.failed == 1
    row = ctx.db.one("SELECT status, error FROM calls WHERE id=?", (bad_call,))
    assert (
        row["status"] == "failed" and row["error"] == "The call stopped on an internal error (RuntimeError)."
    )
    assert "broken" not in row["error"]  # the exception's own text can carry a number: it stays in the log
    assert world.calls(good, "first")[0]["status"] == "completed"
    assert "call.failed" in world.audit_actions()


async def test_no_runner_is_said_out_loud_and_nothing_is_claimed(ctx, world):
    cid = world.active()
    world.settle()
    report = await Scheduler(ctx, "a").tick()
    assert report.dispatched == 0 and any("No call runner" in e for e in report.errors)
    assert world.calls(cid, "first")[0]["status"] == "scheduled"


async def test_a_database_error_in_one_tick_does_not_stop_the_next(ctx, world, runner, monkeypatch):
    world.active()
    world.settle()
    sched = Scheduler(ctx, "a")
    monkeypatch.setattr(sched, "_plan", lambda conn, work: (_ for _ in ()).throw(RuntimeError("disk full")))
    report = await sched.tick()
    assert report.ran and "disk full" in report.errors[0]
    monkeypatch.undo()
    assert (await sched.tick()).dispatched == 1


async def test_a_failure_taking_the_lock_does_not_end_the_loop(ctx, world, runner, monkeypatch):
    world.active()
    world.settle()
    sched = Scheduler(ctx, "a", loop_seconds=0.01)
    real = sched._acquire
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real()

    monkeypatch.setattr(sched, "_acquire", flaky)
    report = await sched.tick()
    assert not report.ran and "database is locked" in report.errors[0]
    task = asyncio.create_task(sched.run_forever())
    for _ in range(200):
        if runner.placed:
            break
        await asyncio.sleep(0.01)
    sched.stop()
    await asyncio.wait_for(task, 5)
    assert len(runner.placed) == 1


async def test_a_call_whose_number_is_past_its_keep_time_is_cancelled_even_before_the_purge_runs(
    ctx, world, fixed_clock, runner
):
    cid = world.active(regimen="reexposure")
    ctx.db.connect().execute(
        "UPDATE calls SET scheduled_for=? WHERE course_id=?", (clock.iso(clock.now()), cid)
    )
    purge_after = clock.parse_iso(ctx.db.one("SELECT purge_after FROM contacts")["purge_after"])
    world.goto(purge_after.astimezone(clock.local_tz(TZ)).date(), "10:00", fixed_clock)
    sched = Scheduler(ctx, "a", purge_every_seconds=10**9)
    ctx.db.connect().execute(
        "INSERT INTO kv(key, value) VALUES ('last_purge_run', ?)", (clock.iso(clock.now()),)
    )
    report = await sched.tick()
    row = world.calls(cid, "first")[0]
    assert report.dispatched == 0 and runner.placed == []
    assert row["status"] == "cancelled" and "keep time" in row["error"]


async def test_tick_runs_the_privacy_jobs_on_its_own_pace(ctx, world, fixed_clock, runner):
    cid = world.active(regimen="reexposure")
    sched = Scheduler(ctx, "a", purge_every_seconds=60)
    first = await sched.tick()
    assert first.purge is not None and first.purge.phones == 0
    fixed_clock.advance(timedelta(days=11))
    again = await sched.tick()
    assert again.purge is not None and again.purge.phones == 1
    assert enrol.get_course(ctx, cid).phone_purged
    fixed_clock.advance(timedelta(seconds=30))
    assert (await sched.tick()).purge is None


async def test_a_purge_stamped_in_the_future_is_run_again_at_once(ctx, world, fixed_clock, runner):
    sched = Scheduler(ctx, "a", purge_every_seconds=60)
    real = fixed_clock.now()
    fixed_clock.advance(timedelta(days=10))  # the demo clock jumps; the purge job stamps its run
    assert (await sched.tick()).purge is not None
    fixed_clock.set(real)  # and goes back (Reset): the stamp is ten days ahead
    assert (await sched.tick()).purge is not None
    stamp = clock.parse_iso(ctx.db.one("SELECT value FROM kv WHERE key='last_purge_run'")["value"])
    assert stamp == real
    fixed_clock.advance(timedelta(seconds=30))
    assert (await sched.tick()).purge is None


def test_resetting_the_demo_clock_and_the_demo_data_forget_a_purge_stamped_ahead(ctx):
    from chaarteeke.demo import controls
    from chaarteeke.demo.seed import _wipe
    from chaarteeke.privacy import run_purges

    with clock.use_clock(clock.OffsetClock(timedelta(days=10))):
        run_purges(ctx)
        assert ctx.db.one("SELECT 1 FROM kv WHERE key='last_purge_run'")
        controls.reset_clock(ctx)
    assert ctx.db.one("SELECT 1 FROM kv WHERE key='last_purge_run'") is None
    run_purges(ctx)
    with ctx.db.transaction() as conn:
        _wipe(conn)
    assert ctx.db.one("SELECT 1 FROM kv WHERE key='last_purge_run'") is None


# --------------------------------------------------------------- next call
def test_next_call_at_for_the_demo_clock(ctx, world, fixed_clock):
    assert next_call_at(ctx) is None
    cid = world.active(day0=world.today())
    first = world.calls(cid, "first")[0]
    # the first call is due in 8 seconds
    assert next_call_at(ctx) == clock.now() + timedelta(seconds=8)
    assert next_call(ctx).kind == "first"
    ctx.db.connect().execute("UPDATE calls SET status='completed' WHERE id=?", (first["id"],))
    # then the reminder for dose 2 the evening before it
    due = world.dose_date(cid, 2)
    assert next_call_at(ctx) == local(due - timedelta(days=1), "18:30")
    nxt = next_call(ctx)
    assert (nxt.kind, nxt.course_id, nxt.dose_no) == ("reminder", cid, 2)


def test_next_call_at_waits_for_the_window(ctx, world, fixed_clock):
    world.active()
    world.goto(world.today(), "21:00", fixed_clock)
    assert next_call_at(ctx) == local(world.today() + timedelta(days=1), "09:00")


def test_next_call_at_counts_the_morning_after_and_skips_unconsented(ctx, world, fixed_clock):
    cid = world.active(day0=world.today() - timedelta(days=3))
    ctx.db.connect().execute("UPDATE calls SET status='completed'")
    world.goto(world.today(), "21:00", fixed_clock)
    # dose 2 is due today (past reminder time); the next call is the morning after, or the dose 3 reminder
    assert next_call_at(ctx) == local(world.today() + timedelta(days=1), "09:30")
    ctx.db.connect().execute("UPDATE courses SET consent_reminders_at=NULL WHERE id=?", (cid,))
    assert next_call_at(ctx) is None


# ------------------------------------------------------------------ the loop
async def test_run_forever_places_calls_and_releases_the_lock(ctx, world, runner):
    cid = world.active()
    world.settle()
    sched = Scheduler(ctx, "a", loop_seconds=0.01)
    task = asyncio.create_task(sched.run_forever())
    for _ in range(200):
        if runner.placed:
            break
        await asyncio.sleep(0.01)
    sched.stop()
    await asyncio.wait_for(task, 5)
    assert runner.placed == [world.calls(cid, "first")[0]["id"]]
    assert ctx.db.one("SELECT 1 FROM locks WHERE name='scheduler'") is None


async def test_run_forever_survives_a_bad_row(ctx, world):
    bad = world.active(register_no="BAD")
    good = world.active(register_no="GOOD")
    world.settle()
    bad_call = world.calls(bad, "first")[0]["id"]
    ctx.runner = FakeRunner(ctx, fail_on={bad_call})
    sched = Scheduler(ctx, "a", loop_seconds=0.01)
    task = asyncio.create_task(sched.run_forever())
    for _ in range(200):
        if ctx.db.one("SELECT status FROM calls WHERE id=?", (bad_call,))["status"] == "failed" and (
            world.calls(good, "first")[0]["status"] == "completed"
        ):
            break
        await asyncio.sleep(0.01)
    sched.stop()
    await asyncio.wait_for(task, 5)
    assert ctx.db.one("SELECT error FROM calls WHERE id=?", (bad_call,))["error"] == (
        "The call stopped on an internal error (RuntimeError)."
    )
    assert world.calls(good, "first")[0]["status"] == "completed"


async def test_notice_calls_go_out_and_an_unanswered_notice_is_asked_again_only_by_a_new_missed_call(
    ctx, world, fixed_clock
):
    ctx.runner = FakeRunner(ctx, outcome="completed")
    cid = world.active(number="+919876543210")
    world.settle()
    sched = Scheduler(ctx, "a")
    await sched.tick()
    enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    world.settle()
    await sched.tick()
    notice = world.calls(cid, "duplicate_notice")[0]
    assert notice["status"] == "completed" and notice["id"] in ctx.runner.placed
    # a missed call after the notice plays it again; nothing is withdrawn, whoever answers
    ctx.runner.outcome = "no_answer"
    enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    world.settle()
    await sched.tick()
    assert [c["status"] for c in world.calls(cid, "duplicate_notice")][-1] == "no_answer"
    assert enrol.get_course(ctx, cid).status == "active"


async def test_a_patient_who_never_agrees_does_not_keep_a_number_on_file(ctx, world, fixed_clock, events):
    ctx.runner = FakeRunner(ctx, outcome="no_answer")
    cid = world.active(consent=False, register_no="N1")
    sched = Scheduler(ctx, "a")
    world.goto(world.today(), "10:05", fixed_clock)
    for _ in range(3):
        await sched.tick()
        await sched.tick()
        fixed_clock.advance(timedelta(minutes=121))
    await sched.tick()
    assert [r["attempt"] for r in world.calls(cid, "first")] == [1, 2, 3]
    course = enrol.get_course(ctx, cid)
    assert course.status == "awaiting_call" and course.phone_last4 is None
    assert ctx.db.one("SELECT * FROM contacts WHERE course_id=?", (cid,)) is None
    log = ctx.db.one("SELECT reason FROM purge_log WHERE course_id=? AND what='phone'", (cid,))
    assert log["reason"] == "no consent"
    [note] = enrol.ended_by_patient(ctx, "cln_a")
    assert (note.course_id, note.kind, note.needs_new_number) == (cid, "no_consent", True)


async def test_a_patient_who_agreed_keeps_their_number_whatever_happens_to_the_calls(ctx, world, fixed_clock):
    ctx.runner = FakeRunner(ctx, outcome="no_answer")
    cid = world.active(consent=True)
    sched = Scheduler(ctx, "a")
    world.goto(world.today(), "10:05", fixed_clock)
    for _ in range(8):
        await sched.tick()
        fixed_clock.advance(timedelta(minutes=121))
        await sched.tick()
    assert ctx.db.one("SELECT 1 FROM contacts WHERE course_id=?", (cid,)) is not None


async def test_our_own_failures_never_cost_a_patient_their_number(ctx, world, fixed_clock):
    ctx.runner = FakeRunner(
        ctx, outcome="failed"
    )  # a missing voice file, a broken line: not the patient's doing
    cid = world.active(consent=False)
    sched = Scheduler(ctx, "a")
    world.goto(world.today(), "10:05", fixed_clock)
    for _ in range(8):
        await sched.tick()
        fixed_clock.advance(timedelta(minutes=121))
    assert len(world.calls(cid, "first")) == 3
    assert ctx.db.one("SELECT 1 FROM contacts WHERE course_id=?", (cid,)) is not None


async def test_a_first_call_to_a_closed_browser_phone_is_not_retried_and_the_number_goes(
    ctx, world, fixed_clock
):
    ctx.runner = FakeRunner(ctx, outcome="no_answer")
    cid = world.active(consent=False)
    ctx.db.connect().execute("UPDATE calls SET transport='sim' WHERE course_id=?", (cid,))
    world.settle()
    sched = Scheduler(ctx, "a")
    await sched.tick()
    ctx.db.connect().execute(
        "UPDATE calls SET hangup_cause='The browser phone is not connected' WHERE course_id=?", (cid,)
    )
    await sched.tick()
    assert len(world.calls(cid, "first")) == 1  # no second ring to a page that is not there
    assert enrol.get_course(ctx, cid).status == "awaiting_call"


async def test_a_person_who_hangs_up_in_the_notice_every_time_counts_as_not_agreeing(ctx, world, fixed_clock):
    ctx.runner = FakeRunner(ctx, outcome=None)
    cid = world.active(consent=False)
    sched = Scheduler(ctx, "a")
    world.goto(world.today(), "10:05", fixed_clock)
    for _ in range(3):
        await sched.tick()
        ctx.db.connect().execute(
            "UPDATE calls SET status='failed', ended_at=?, error=? WHERE course_id=? AND status IN ('dialing','scheduled')"
            " AND kind='first'",
            (clock.iso(clock.now()), enrol.PERSON_HUNG_UP, cid),
        )
        await sched.tick()
        fixed_clock.advance(timedelta(minutes=121))
    await sched.tick()
    assert enrol.get_course(ctx, cid).status == "awaiting_call"


def test_no_wall_clock_in_the_core_lane():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "chaarteeke"
    for name in ("doses", "enrol", "scheduler", "privacy"):
        assert "datetime.now(" not in (root / f"{name}.py").read_text(), name


# ------------------------------------------------------------ demo history and policy timings
def make_demo(ctx, cid):
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE courses SET source='demo' WHERE id=?", (cid,))


async def test_demo_seeded_courses_are_never_planned_dialled_or_retried(ctx, world, fixed_clock, runner):
    cid = world.active()
    due = world.dose_date(cid, 2)
    make_demo(ctx, cid)
    world.goto(due - timedelta(days=1), "18:30", fixed_clock)
    report = await Scheduler(ctx, "a").tick()
    assert report.dispatched == 0 and runner.placed == []
    assert [c["kind"] for c in world.calls(cid)] == ["first"]  # no reminder was planned
    assert world.calls(cid)[0]["status"] == "scheduled"  # and the first call is never handed over
    assert next_call(ctx) is None and next_call_at(ctx) is None
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE calls SET status='no_answer' WHERE course_id=?", (cid,))
    await Scheduler(ctx, "a").tick()
    assert len(world.calls(cid)) == 1  # a failed call of demo history gets no retry


async def test_a_real_course_beside_demo_history_is_still_called(ctx, world, fixed_clock, runner):
    demo = world.active(register_no="D1")
    real = world.active(register_no="R1")
    make_demo(ctx, demo)
    world.settle()
    await Scheduler(ctx, "a").tick()
    assert runner.placed == [world.calls(real, "first")[0]["id"]]


async def test_the_loop_lock_and_purge_timings_come_from_policy(ctx):
    sched = Scheduler(ctx, "a")
    timing = ctx.policy["scheduler"]
    assert sched.loop_seconds == timing["loop_s"]
    assert sched.lock_stale == timedelta(seconds=timing["lock_stale_s"])
    assert sched.purge_every == timedelta(seconds=timing["purge_every_s"])
    assert Scheduler(ctx, "b", loop_seconds=0.5).loop_seconds == 0.5


async def test_the_line_carries_as_many_calls_as_policy_allows(ctx, world, fixed_clock):
    ctx.policy.raw["calls"]["max_parallel_ari"] = 2
    ctx.runner = runner = LineRunner(ctx)
    for i in range(3):
        world.active(register_no=f"P{i}")
    world.settle()
    sched = Scheduler(ctx, "a")
    assert (await sched.tick(wait=False)).dispatched == 2
    await sched.wait_idle()
    assert (await sched.tick(wait=False)).dispatched == 1
    await sched.wait_idle()
    assert runner.peak == {"ari": 2} and len(runner.placed) == 3


def test_the_phone_is_purged_at_the_policy_local_time(ctx, world):
    ctx.policy.raw["retention"]["purge_local_time"] = "03:30"
    cid = world.active()
    last = max(world.dose_date(cid, n) for n in (1, 2, 3, 4))
    days = ctx.policy["retention"]["phone_purge_days_after_last_dose"]
    expected = clock.iso(local(last + timedelta(days=days), "03:30"))
    assert ctx.db.one("SELECT purge_after FROM contacts WHERE course_id=?", (cid,))["purge_after"] == expected


async def test_a_failed_call_never_puts_a_number_in_the_log_or_on_the_screen(ctx, world, fixed_clock, caplog):
    cid = world.active(register_no="N")
    world.settle()

    echoed = (
        "+91987654" + "3210 on sim:" + "abc123"
    )  # built here so the source line of the raise has no number

    class Leaky:
        async def place(self, call_id):
            raise RuntimeError(f"could not dial {echoed}")

    ctx.runner = Leaky()
    await Scheduler(ctx, "a").tick()
    row = world.calls(cid, "first")[0]
    assert row["status"] == "failed" and "9876543210" not in row["error"] and "sim:" not in row["error"]
    assert "9876543210" not in caplog.text and "abc123" not in caplog.text
    assert "RuntimeError" in caplog.text  # the class and the code path stay, for the operator
