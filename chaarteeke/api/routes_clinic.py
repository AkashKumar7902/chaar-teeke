"""The clinic desk: today's lists, enrolling, linking a missed call, marking doses, stopping, the phone
reveal, the vaccine switch and the call-back requests from the vets.

A clinic person only ever gets their own clinic: the id in a request is checked against their login, never
trusted. The administrator reads a clinic's settings (`/api/clinic/config`) and no patient, and writes nothing
here. A phone-number reveal is limited per login (policy clinic.phone_reveals_per_hour). The one-tap writes
(a dose given, a link, a call-back closed) each have an audited undo, in reversals.py.
"""

# ruff: noqa: B008 (FastAPI reads `Depends(...)` in argument defaults)

from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .. import alerts, audit, clock, doses, enrol
from .. import regimens as regs
from ..ivr.prompts import PATIENT_LANGS
from ..models import (
    CallbackRequestOut,
    ClinicChoice,
    ClinicConfig,
    ClinicToday,
    CourseOut,
    DoseOut,
    EnrolIn,
    RegimenChoice,
)
from . import reversals
from .auth import AuthError, Principal
from .deps import course_clinic, get_ctx, own_clinic, reader, writer

router = APIRouter()
_REVEAL_LOCK = (
    threading.Lock()
)  # count and reveal together, so parallel requests cannot all pass the last place


class LinkIn(BaseModel):
    course_id: str = Field(min_length=1, max_length=64)


class DoseGivenIn(BaseModel):
    on: str | None = Field(default=None, max_length=10)  # YYYY-MM-DD, local date; today when absent


class StopIn(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class VaccineIn(BaseModel):
    out: bool


class CallbackDoneIn(BaseModel):
    note: str | None = Field(default=None, max_length=300)
    outcome: Literal["done", "declined", "closed_no_contact"] = "done"


def _day(text: str) -> date:
    try:
        day = date.fromisoformat(text)
    except ValueError:
        day = None
    if day is None or day.isoformat() != text:
        raise AuthError("bad_date", "That is not a valid date (use YYYY-MM-DD).", 400)
    return day


@router.get("/api/clinic/today")
def today(
    request: Request, clinic_id: str | None = None, who: Principal = Depends(reader("clinic"))
) -> ClinicToday:
    ctx = get_ctx(request)
    return enrol.clinic_today(ctx, own_clinic(ctx, who, clinic_id))


@router.get("/api/clinic/config")
def config(
    request: Request, clinic_id: str | None = None, who: Principal = Depends(reader("clinic_config"))
) -> ClinicConfig:
    """What the enrol form and the "tell the patient to ring" step need. Same ownership rule as today's list.

    `langs` lists only the languages whose prompt audio is complete for this clinic (its own clip and its
    visit time), the same check the call runner makes before it dials: a language with a file missing is not
    offered. `ct_number` is null in demo mode or when CT_NUMBER is not set; nothing stands in for it.
    """
    ctx = get_ctx(request)
    cid = own_clinic(ctx, who, clinic_id)
    clinic = ctx.db.one("SELECT * FROM clinics WHERE id = ?", (cid,))
    if clinic is None:
        raise AuthError("unknown_clinic", "There is no such clinic", 404)
    times = [clinic["visit_time"]] if clinic["visit_time"] else []
    langs = [lang for lang in PATIENT_LANGS if not ctx.prompts.missing(lang, clinic_ids=[cid], times=times)]
    switch = []
    if who.role in ("visitor", "admin"):
        found = ctx.db.query("SELECT id, name FROM clinics WHERE is_demo = 1 ORDER BY name")
        switch = [ClinicChoice(id=r["id"], name=r["name"]) for r in found]
    settings = ctx.settings
    return ClinicConfig(
        clinic_id=cid,
        clinic_name=clinic["name"],
        default_regimen=clinic["default_regimen"],
        default_lang=clinic["default_lang"],
        regimens=[
            RegimenChoice(id=r.id, name=r.name, route=r.route, days=list(r.days), enabled=r.enabled)
            for r in regs.regimens().values()
            if r.enabled
        ],
        langs=langs,
        ct_number=None if settings.is_demo or not settings.ct_number else settings.ct_number,
        sim_phone=settings.sim_enabled,
        clinics=switch,
    )


@router.post("/api/courses")
def enrol_patient(body: EnrolIn, request: Request, who: Principal = Depends(writer("clinic"))) -> CourseOut:
    ctx = get_ctx(request)
    own_clinic(ctx, who, body.clinic_id)
    return enrol.create_course(ctx, body, who.actor, source="clinic")


@router.post("/api/missed-calls/{missed_call_id}/link")
def link(
    missed_call_id: str, body: LinkIn, request: Request, who: Principal = Depends(writer("clinic"))
) -> CourseOut:
    ctx = get_ctx(request)
    course_clinic(ctx, who, body.course_id)
    return enrol.link(ctx, missed_call_id, body.course_id, who.actor)


@router.get("/api/courses/{course_id}")
def course(course_id: str, request: Request, who: Principal = Depends(reader("clinic"))) -> CourseOut:
    ctx = get_ctx(request)
    course_clinic(ctx, who, course_id)
    return enrol.get_course(ctx, course_id)


@router.post("/api/courses/{course_id}/doses/{dose_no}/given")
def dose_given(
    course_id: str,
    dose_no: int,
    request: Request,
    body: DoseGivenIn | None = None,
    who: Principal = Depends(writer("clinic")),
) -> DoseOut:
    ctx = get_ctx(request)
    course_clinic(ctx, who, course_id)
    on = _day(body.on) if body and body.on else clock.local_today(ctx.settings.local_tz)
    return doses.mark_given(ctx, course_id, dose_no, "clinic", on, who.actor)


@router.post("/api/courses/{course_id}/stop")
def stop(
    course_id: str, body: StopIn, request: Request, who: Principal = Depends(writer("clinic"))
) -> CourseOut:
    ctx = get_ctx(request)
    course_clinic(ctx, who, course_id)
    enrol.stop(ctx, course_id, body.reason.strip(), who.actor)
    return enrol.get_course(ctx, course_id)


def _reveal_allowed(ctx, who: Principal, course_id: str) -> None:
    """Refuse the (limit + 1)th reveal by one login in an hour. The refusal is audited as phone.reveal_limited
    (once a minute at most, so a loop of requests cannot fill the log) and the administrator's status page
    raises an alarm on it."""
    pol = ctx.policy["clinic"]["phone_reveals_per_hour"]
    now = clock.now()
    since = clock.iso(now - timedelta(hours=1))
    used = ctx.db.query(
        "SELECT at FROM audit WHERE action = 'phone.reveal' AND actor = ? AND at > ? ORDER BY seq",
        (who.actor, since),
    )
    if len(used) < pol:
        return
    retry = int((clock.parse_iso(used[0]["at"]) + timedelta(hours=1) - now).total_seconds()) + 1
    recent = ctx.db.one(
        "SELECT 1 FROM audit WHERE action = 'phone.reveal_limited' AND actor = ? AND at > ?",
        (who.actor, clock.iso(now - timedelta(minutes=1))),
    )
    if recent is None:
        with ctx.db.transaction() as conn:
            audit.record(conn, who.actor, "phone.reveal_limited", "course", course_id, {"limit": pol})
    raise AuthError(
        "rate_limited",
        f"You have read {pol} phone numbers in the last hour, which is the most one login may. "
        "Try again later, or ask an administrator.",
        429,
        retry_after=max(retry, 1),
    )


@router.post("/api/courses/{course_id}/reveal-phone")
def reveal_phone(
    course_id: str, request: Request, who: Principal = Depends(writer("clinic"))
) -> dict[str, str]:
    """The one place a whole number leaves the vault. enrol.reveal_phone audits the reveal (and a refusal)."""
    ctx = get_ctx(request)
    course_clinic(ctx, who, course_id)
    with _REVEAL_LOCK:
        _reveal_allowed(ctx, who, course_id)
        return {"phone": enrol.reveal_phone(ctx, course_id, who.actor)}


@router.post("/api/clinics/{clinic_id}/vaccine")
def vaccine(
    clinic_id: str, body: VaccineIn, request: Request, who: Principal = Depends(writer("clinic"))
) -> dict[str, object]:
    ctx = get_ctx(request)
    own_clinic(ctx, who, clinic_id)
    enrol.set_vaccine_out(ctx, clinic_id, body.out, who.actor)
    row = ctx.db.one("SELECT vaccine_out, vaccine_out_since FROM clinics WHERE id = ?", (clinic_id,))
    return {"vaccine_out": bool(row["vaccine_out"]), "vaccine_out_since": row["vaccine_out_since"]}


@router.get("/api/clinic/callbacks")
def callbacks(
    request: Request, clinic_id: str | None = None, who: Principal = Depends(reader("clinic"))
) -> list[CallbackRequestOut]:
    ctx = get_ctx(request)
    return alerts.callbacks_for_clinic(ctx, own_clinic(ctx, who, clinic_id), "open")


@router.post("/api/callbacks/{callback_id}/done")
def callback_done(
    callback_id: str,
    request: Request,
    body: CallbackDoneIn | None = None,
    who: Principal = Depends(writer("clinic")),
) -> CallbackRequestOut:
    ctx = get_ctx(request)
    body = body or CallbackDoneIn()
    return alerts.callback_done(ctx, callback_id, who.actor, body.note, body.outcome)


# ------------------------------------------------------------------ taking back
@router.post("/api/courses/{course_id}/doses/{dose_no}/unmark")
def dose_unmark(
    course_id: str, dose_no: int, request: Request, who: Principal = Depends(writer("clinic"))
) -> DoseOut:
    """Take back a dose the clinic marked given, within policy clinic.undo_dose_minutes."""
    ctx = get_ctx(request)
    course_clinic(ctx, who, course_id)
    return reversals.unmark_dose(ctx, course_id, dose_no, who.actor)


@router.post("/api/missed-calls/{missed_call_id}/unlink")
def unlink(missed_call_id: str, request: Request, who: Principal = Depends(writer("clinic"))) -> CourseOut:
    """Take back a link before the first call is placed. The course waits for a call again."""
    ctx = get_ctx(request)
    row = ctx.db.one("SELECT course_id FROM missed_calls WHERE id = ?", (missed_call_id,))
    if row is None:
        raise AuthError("not_found", "This missed call was not found.", 404)
    if row["course_id"]:
        course_clinic(ctx, who, row["course_id"])
    return enrol.get_course(ctx, reversals.unlink_missed_call(ctx, missed_call_id, who.actor))


@router.post("/api/callbacks/{callback_id}/reopen")
def callback_reopen(
    callback_id: str, request: Request, who: Principal = Depends(writer("clinic"))
) -> CallbackRequestOut:
    """Open a call-back again that was closed by mistake, within policy clinic.undo_callback_minutes."""
    ctx = get_ctx(request)
    return reversals.reopen_callback(ctx, callback_id, own_clinic(ctx, who), who.actor)
