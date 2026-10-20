"""The shared contract: enums and the JSON shapes the API returns. Pages code against these.

Change a model only when the task is to change the contract, and update MODULES.md in the same change.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Lang = Literal["hi", "doi", "ur"]
UiLang = Literal["en", "hi"]
Role = Literal["clinic", "abc", "vet", "oversight", "reviewer", "admin", "visitor"]
CourseStatus = Literal["awaiting_call", "active", "completed", "stopped"]
Eligibility = Literal["adult", "guardian_consented", "unconfirmed"]
DoseStatus = Literal["due", "given", "late_minor", "late_review", "not_needed"]
CallKind = Literal["first", "reminder", "missed_dose", "duplicate_notice", "withdrawn_notice"]
CallStatus = Literal[
    "scheduled", "dialing", "ringing", "answered", "completed", "no_answer", "failed", "cancelled"
]
PlaceType = Literal["home_street", "school", "hospital", "bus_market", "other", "no_answer"]
Animal = Literal["notched", "unnotched", "unsure", "pet", "other", "animal_unknown", "no_answer"]
YesNo = Literal["yes", "no", "no_answer"]
Signs = Literal["yes", "no", "dont_know", "no_answer"]
LocationStatus = Literal[
    "pending", "matched", "needs_review", "decided", "outside_jmc", "cant_tell", "no_place_heard", "skipped"
]
AlertStatus = Literal["new", "claimed", "acted", "not_acted"]
Confidence = Literal["likely", "possible", "none"]
Source = Literal["clinic", "simulator", "demo"]


class ApiError(BaseModel):
    error: str
    message: str


# ---------------------------------------------------------------- session
class ModeOut(BaseModel):
    """GET /api/mode: public, answers without a session and never with a 401."""

    mode: Literal["demo", "pilot"]
    basemap: Literal["none", "openfreemap"]
    public_url: str | None  # CT_PUBLIC_URL, or null when none is set
    signed_in: bool
    view: (
        Role | None
    )  # the role whose pages this browser sees (a visitor's chosen view), null when signed out


class Me(BaseModel):
    role: Role
    acting_as: Role | None = None  # a visitor's chosen view
    display_name: str
    clinic_id: str | None = None
    csrf: str
    mode: Literal["demo", "pilot"]
    can_write: bool


# ----------------------------------------------------------------- clinic
class DoseOut(BaseModel):
    dose_no: int
    offset_days: int
    due_date: str
    status: DoseStatus
    given_on: str | None = None
    marked_by: Literal["clinic", "patient"] | None = None


class CallOut(BaseModel):
    id: str
    kind: CallKind
    dose_no: int | None
    scheduled_for: str
    attempt: int
    status: CallStatus
    answered_at: str | None = None
    ended_at: str | None = None
    seconds_listened: float | None = None
    error: str | None = None


class ClinicRow(BaseModel):
    """One patient on the clinic's Today list. Never carries place or dog answers."""

    course_id: str
    register_no: str | None
    phone_last4: str | None  # None once purged
    regimen_id: str
    next_dose_no: int | None
    next_due_date: str | None
    state: Literal[
        "due_today",
        "reminded",
        "tried",
        "patient_says_taken",
        "marked_given",
        "late_minor",
        "late_review",
        "awaiting_call",
        "no_answer",
    ]
    attempts: int = 0
    last_call_status: CallStatus | None = None
    lang: Lang
    source: Source


class UnlinkedMissedCall(BaseModel):
    id: str
    at: str
    phone_last4: str | None
    transport: Literal["sim", "ari"]


class CallbackRequestOut(BaseModel):
    id: str
    course_id: str | None
    register_no: str | None
    ward_no: int | None
    requested_at: str
    status: Literal["open", "done", "declined", "closed_no_contact"]


class ClinicToday(BaseModel):
    clinic_id: str
    clinic_name: str
    local_date: str
    vaccine_out: bool
    vaccine_out_since: str | None
    counts: dict[str, int]  # due_today, missed, late_review, awaiting_call, unlinked, callbacks
    due_today: list[ClinicRow]
    missed: list[ClinicRow]
    awaiting_call: list[ClinicRow]
    unlinked: list[UnlinkedMissedCall]
    callbacks: list[CallbackRequestOut]
    next_due_date: str | None  # for the empty state: "No one is due today. Next due ..."


class RegimenChoice(BaseModel):
    id: str
    name: str
    route: str
    days: list[int]
    enabled: bool


class ClinicChoice(BaseModel):
    id: str
    name: str


class ClinicConfig(BaseModel):
    """GET /api/clinic/config: what the enrol form and the call-in instructions need for one clinic."""

    clinic_id: str
    clinic_name: str
    default_regimen: str
    default_lang: Lang
    regimens: list[RegimenChoice]  # enabled regimens only
    langs: list[Lang]  # languages whose prompt audio is complete for this clinic
    ct_number: str | None  # the Chaar Teeke missed-call number (CT_NUMBER); null in demo or when unset
    sim_phone: bool  # the browser phone is on (CT_TELEPHONY includes sim)
    clinics: list[ClinicChoice]  # demo clinics a visitor or the administrator may switch to; else empty


class EnrolIn(BaseModel):
    clinic_id: str
    register_no: str = Field(min_length=1, max_length=20)
    patient_type: Literal["new", "old"] = "new"
    regimen_id: str
    lang: Lang
    day0: str  # local date of the first dose
    eligibility: Literal["adult", "guardian_consented"]


class CourseOut(BaseModel):
    id: str
    clinic_id: str
    register_no: str | None
    patient_type: Literal["new", "old"]
    regimen_id: str
    regimen_name: str
    day0: str
    lang: Lang
    status: CourseStatus
    eligibility: Eligibility
    phone_last4: str | None
    phone_purged: bool
    consent_reminders_at: str | None
    doses: list[DoseOut]
    calls: list[CallOut]
    source: Source


# ------------------------------------------------------------------- team
class Share(BaseModel):
    """A notch share with everything that must be shown beside it.

    share, ci_low, ci_high, bound_low and bound_high are fractions from 0 to 1. needed is how many more
    answers are still needed before a share is shown (n + needed = the minimum). Counts may be the strings
    "<5" (suppressed) or "hidden" (a complement that would reveal a suppressed cell).
    """

    n: int  # notch seen + no notch seen
    notched: int | str  # int, "<5" when suppressed, or "hidden"
    unnotched: int | str
    unsure: int | str  # could not see
    status: Literal["ok", "bracketed", "too_few", "no_data"]
    share: float | None = None  # only when status ok or bracketed
    ci_low: float | None = None
    ci_high: float | None = None
    bound_low: float | None = None  # Manski bounds with could-not-see
    bound_high: float | None = None
    needed: int | None = None  # n still needed when too_few


class ClusterCoverage(BaseModel):
    id: str
    name: str
    ward_nos: list[int]
    bites: int | str
    street_dog_bites: int | str
    share: Share
    look_here: bool
    look_here_reason: str | None = None  # English sentence, kept for the API; pages translate from the data
    look_here_reason_data: dict[str, str | int] | None = None  # {kind: "strong", n, and the answers it used}
    simulated_bites: int = 0


class Coverage(BaseModel):
    window_start: str
    window_end: str
    city: Share
    claim_band: tuple[float, float]
    claim_label: str  # English sentence, kept for the API
    claim_label_key: str  # stable i18n key (common bundle) taking {lo} and {hi}, the band in whole percent
    clusters: list[ClusterCoverage]
    look_here: list[str]  # cluster ids, at most 3, no rank
    total_bites: int | str
    could_not_place: int | str  # reports without a ward (outside JMC, can't tell, no place heard)
    is_demo: bool


class WorkedAffected(BaseModel):
    """GET /api/worked/affected: an aggregate, "<5" when small."""

    courses_with_doses_ahead: int | str


class WardCounts(BaseModel):
    ward_no: int
    ward_name: str | None
    cluster_id: str
    bites: int | str
    school: int | str
    hospital: int | str
    bus_market: int | str
    alerts: int | str
    worked_on: str | None


class AlertOut(BaseModel):
    id: str
    ward_no: int | None
    ward_name: str | None
    cluster_id: str | None
    place_type: PlaceType
    local_day: str
    status: AlertStatus
    claimed_by: str | None
    claimed_at: str | None
    outcome_note: str | None
    created_at: str  # the local day: a real alert is served with its day only, never the second it was made
    made_at: str | None = None  # the instant, for a simulated alert only (the visitor's own phone)
    location_status: LocationStatus | None = None  # a ward stays an estimate until a reviewer decides it
    callback_status: Literal["open", "done", "declined", "closed_no_contact"] | None = None
    simulated: bool = False
    claimed_by_me: bool = False  # the person asking is the one who took it


class FeedItem(BaseModel):
    """One row of the landing page's "Latest" list: a simulated call, missed call or report, or an alert
    summary. Never a phone number, a course or a clip."""

    kind: Literal["call", "missed_call", "report", "alert"]
    id: str
    at: str | None  # the instant; none for a real alert, which is served with its day only
    day: str  # the local day
    cluster_id: str | None = None
    ward_no: int | None = None
    ward_name: str | None = None
    animal: Animal | None = None  # a simulated report only; a real report's answer is never sent
    location_status: LocationStatus | None = None
    simulated: bool


# ---------------------------------------------------------------- review
class Candidate(BaseModel):
    ward_no: int
    ward_name: str | None
    label: Literal["likely", "possible"]
    evidence: str  # the gazetteer name that matched


class ReviewItem(BaseModel):
    report_id: str
    heard_at: str
    transcript: str | None
    asr_status: Literal["pending", "done", "off", "failed", "no_audio"]
    asr_error: str | None
    audio_available: bool
    candidates: list[Candidate]
    remaining: int
    expires_at: str


class DecideIn(BaseModel):
    ward_no: int | None = None
    outside_tehsil: str | None = None
    cant_tell: bool = False
    remember_name: bool = False


# --------------------------------------------------------------- oversight
class OversightSummary(BaseModel):
    window_start: str
    window_end: str
    enrolled: int | str
    completed: int | str
    completion_rate: float | None
    completion_basis: str
    bites_with_place: int | str
    bites_inside_jmc: int | str
    alerts_total: int | str
    alerts_acted: int | str
    coverage: Coverage
    is_demo: bool


# ------------------------------------------------------------------ system
class Check(BaseModel):
    name: str
    ok: bool
    detail: str
    alarm: bool = False  # red only when True


class SystemStatus(BaseModel):
    mode: Literal["demo", "pilot"]
    telephony: str
    checks: list[Check]
    policy_version: str
    prompts_missing: dict[str, list[str]]


class DemoState(BaseModel):
    now: str
    local_now: str
    offset_seconds: float
    next_call_at: str | None
    next_call_kind: CallKind | None
    jump_targets: list[Literal["next_call", "evening_before_next_dose", "morning_after"]]
