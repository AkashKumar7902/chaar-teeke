"""Helpers for the core lane: a small world builder and a fake call runner (a unique module name, because
tests/ has several conftest.py files)."""

from __future__ import annotations

import asyncio
import itertools
from datetime import date, timedelta

from chaarteeke import clock, enrol
from chaarteeke.models import EnrolIn


class World:
    """Builds clinics, courses and linked phones on the test context."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._numbers = itertools.count(10000)
        self._registers = itertools.count(1)

    def today(self) -> date:
        return clock.local_today(self.ctx.settings.local_tz)

    def number(self) -> str:
        return f"+919{next(self._numbers):09d}"

    def clinic(self, clinic_id="cln_a", name="Test clinic", demo=0) -> str:
        with self.ctx.db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO clinics(id, name, short_name, ward_no, default_regimen, default_lang,"
                " arv_status, is_demo) VALUES (?,?,?,?, 'id_trc', 'hi', 'confirmed', ?)",
                (clinic_id, name, name[:8], 3, demo),
            )
        return clinic_id

    def data(
        self, clinic_id="cln_a", regimen="id_trc", day0=None, lang="hi", register_no=None, **kw
    ) -> EnrolIn:
        return EnrolIn(
            clinic_id=clinic_id,
            register_no=register_no or f"R{next(self._registers)}",
            patient_type=kw.get("patient_type", "new"),
            regimen_id=regimen,
            lang=lang,
            day0=(day0 or self.today()).isoformat(),
            eligibility=kw.get("eligibility", "adult"),
        )

    def course(self, **kw) -> str:
        self.clinic(kw.get("clinic_id", "cln_a"))
        return enrol.create_course(self.ctx, self.data(**kw), "staff:test").id

    def active(self, number=None, consent=True, **kw) -> str:
        """A linked course: the missed call arrives, the clinic links it, the patient agrees to reminders."""
        cid = self.course(**kw)
        res = enrol.record_missed_call(self.ctx, number or self.number(), "ari", clock.now())
        assert res.status == "unlinked"
        enrol.link(self.ctx, res.missed_call_id, cid, "staff:test")
        if consent:
            with self.ctx.db.transaction() as conn:
                conn.execute(
                    "UPDATE courses SET consent_reminders_at=? WHERE id=?", (clock.iso(clock.now()), cid)
                )
        return cid

    def calls(self, course_id=None, kind=None):
        sql, params = "SELECT * FROM calls WHERE 1=1", []
        if course_id:
            sql += " AND course_id=?"
            params.append(course_id)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        return self.ctx.db.query(sql + " ORDER BY scheduled_for, attempt", tuple(params))

    def dose(self, course_id, dose_no):
        return self.ctx.db.one("SELECT * FROM doses WHERE course_id=? AND dose_no=?", (course_id, dose_no))

    def dose_date(self, course_id, dose_no) -> date:
        return date.fromisoformat(self.dose(course_id, dose_no)["due_date"])

    def settle(self) -> None:
        """Let the first call (a few seconds after linking) come due."""
        clock.current().advance(timedelta(seconds=self.ctx.policy["calls"]["first_call_delay_s"] + 1))

    def goto(self, day: date, hhmm: str, fixed_clock) -> None:
        fixed_clock.set(clock.at_local(day, hhmm, self.ctx.settings.local_tz))

    def audit_actions(self) -> list[str]:
        return [r["action"] for r in self.ctx.db.query("SELECT action FROM audit ORDER BY seq")]


class FakeRunner:
    """Stands in for the call runner. `outcome` decides what the call ends as; None leaves it dialing."""

    def __init__(self, ctx, outcome="completed", is_async=True, fail_on=()):
        self.ctx, self.outcome, self.is_async, self.fail_on = ctx, outcome, is_async, set(fail_on)
        self.placed: list[str] = []

    def _finish(self, call_id):
        self.placed.append(call_id)
        if call_id in self.fail_on:
            raise RuntimeError("the line is broken")
        if self.outcome:
            with self.ctx.db.transaction() as conn:
                conn.execute(
                    "UPDATE calls SET status=?, ended_at=? WHERE id=?",
                    (self.outcome, clock.iso(clock.now()), call_id),
                )

    def place(self, call_id):
        if self.is_async:

            async def go():
                await asyncio.sleep(0)
                self._finish(call_id)

            return go()
        self._finish(call_id)
        return None


def plus_days(d: date, n: int) -> date:
    return d + timedelta(days=n)
