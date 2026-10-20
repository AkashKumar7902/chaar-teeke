"""System status for the admin page (and the frame's fault banner), and the QR code for the phone page.

Red (`alarm`) is for real system faults only: a transport that is down, a speech model that failed to load,
a scheduler nobody is running, a language that is in use but cannot be spoken, a sick-dog alert nobody has
looked at. A switched-off optional part (CT_ASR=off, CT_SCHEDULER off) is said plainly and is not red.
"""

# ruff: noqa: B008 (FastAPI reads `Depends(...)` in argument defaults)

from __future__ import annotations

import io
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import segno
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field

from .. import alerts, audit, clock
from ..config import Settings
from ..context import AppContext
from ..ivr.prompts import PATIENT_LANGS, required_extent
from ..models import Check, SystemStatus
from ..scheduler import LOCK_NAME
from .app import connect_telephony, public_origin
from .auth import AuthError, Principal
from .deps import get_ctx, reader, writer

router = APIRouter()


class CodedCheck(Check):
    """A check that also says which sentence it is: `code` is stable and `params` are the values the English
    `detail` names, so a page can translate it. (models.Check is a contract file; a contract_request asks
    for these two fields to move there.)"""

    code: str
    params: dict[str, str | int] = Field(default_factory=dict)


class CodedStatus(SystemStatus):
    checks: list[CodedCheck]


LANG_NAMES = {"hi": "Hindi", "doi": "Dogri", "ur": "Urdu"}
QR_URL_MAX = 300  # a link to this server is far shorter; a bound on the work one request can ask for
SHOW_MISSING = 5  # how many missing files one line names before it says "and n more"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _age(then: str) -> timedelta:
    return clock.now() - clock.parse_iso(then)


def _count(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def _database(ctx: AppContext) -> Check:
    try:
        ctx.db.one("SELECT 1")
    except Exception as e:  # a database that cannot answer is a fault, whatever the reason
        return CodedCheck(
            name="database",
            code="database.down",
            params={"error": type(e).__name__},
            ok=False,
            alarm=True,
            detail=f"The database does not answer ({type(e).__name__})",
        )
    return CodedCheck(name="database", code="database.ok", ok=True, detail="The database answers")


def _scheduler(ctx: AppContext) -> CodedCheck:
    if not ctx.settings.scheduler:
        return CodedCheck(
            name="scheduler",
            code="scheduler.off",
            ok=False,
            detail="Reminder calls are switched off on this server",
        )
    row = ctx.db.one("SELECT holder, heartbeat_at FROM locks WHERE name = ?", (LOCK_NAME,))
    stale = timedelta(seconds=ctx.policy["scheduler"]["lock_stale_s"])
    if row is None:
        return CodedCheck(
            name="scheduler",
            code="scheduler.never_started",
            ok=False,
            alarm=True,
            detail="No scheduler has started: no reminder will be placed",
        )
    age = _age(row["heartbeat_at"])
    seconds = int(age.total_seconds())
    own = getattr(ctx.scheduler, "holder", None)
    if abs(age) <= stale and row["holder"] != own:
        return CodedCheck(
            name="scheduler",
            code="scheduler.foreign",
            params={"seconds": abs(seconds)},
            ok=False,
            alarm=True,
            detail="Another server on this database holds the scheduler; calls from this server will not be placed",
        )
    if abs(age) > stale:
        return CodedCheck(
            name="scheduler",
            code="scheduler.stopped",
            params={"seconds": seconds},
            ok=False,
            alarm=True,
            detail=f"The scheduler stopped {_count(seconds, 'second')} ago: no reminder will be placed",
        )
    return CodedCheck(
        name="scheduler",
        code="scheduler.running",
        params={"seconds": seconds},
        ok=True,
        detail=f"The scheduler is running (last beat {seconds} s ago)",
    )


def _telephony(ctx: AppContext, detailed: bool = True) -> CodedCheck:
    """`detailed` is for the administrator: the reason names the line's host and port, and the failure's class.
    Everyone else reads that the line is down and the kind of failure."""
    names = {
        "sim": "browser phone (SIMULATOR)",
        "ari": "phone line",
        "ari,sim": "phone line and browser phone (SIMULATOR)",
    }
    what = names[ctx.settings.telephony]
    transport = ctx.settings.telephony
    down = getattr(ctx.runner, "_down", None) if ctx.extras.get("telephony_up") else None
    reason = ctx.extras.get("telephony_error") or (str(down) if down is not None else None)
    if reason:
        failure = ctx.extras.get("telephony_error_class") or (
            type(down).__name__ if down is not None else "TelephonyError"
        )
        params: dict[str, str | int] = {"transport": transport, "failure": failure}
        if detailed:
            params["reason"] = str(reason)
        return CodedCheck(
            name="telephony",
            code="telephony.down",
            params=params,
            ok=False,
            alarm=True,
            detail=f"The {what} is down: {reason}" if detailed else f"The {what} is down ({failure})",
        )
    if not ctx.extras.get("telephony_up") or ctx.runner is None:
        return CodedCheck(
            name="telephony",
            code="telephony.not_started",
            params={"transport": transport},
            ok=False,
            detail=f"The {what} is not started",
        )
    return CodedCheck(
        name="telephony",
        code="telephony.up",
        params={"transport": transport},
        ok=True,
        detail=f"The {what} is up",
    )


def _asr(ctx: AppContext, detailed: bool = True) -> CodedCheck:
    state = ctx.extras.get("asr_state")
    if ctx.settings.asr == "off" or state == "off":
        return CodedCheck(
            name="speech",
            code="speech.off",
            ok=False,
            detail="No transcript: speech model off (CT_ASR=off)",
        )
    if state == "ready":
        return CodedCheck(name="speech", code="speech.ready", ok=True, detail="The speech model is loaded")
    if state == "loading":
        return CodedCheck(
            name="speech", code="speech.loading", ok=False, detail="The speech model is loading"
        )
    if state == "failed":
        reason = str(ctx.extras.get("asr_error", "no reason given"))
        failure = reason.partition(":")[0]  # the class; the rest can carry a file path
        return CodedCheck(
            name="speech",
            code="speech.failed",
            params={"reason": reason} if detailed else {"failure": failure},
            ok=False,
            alarm=True,
            detail=f"The speech model failed to load: {reason}"
            if detailed
            else f"The speech model failed to load ({failure})",
        )
    return CodedCheck(
        name="speech",
        code="speech.not_started",
        ok=False,
        detail="The speech model has not been started",
    )


def languages_in_use(ctx: AppContext) -> set[str]:
    rows = ctx.db.query(
        "SELECT DISTINCT lang FROM courses WHERE status IN ('awaiting_call','active')"
        " UNION SELECT DISTINCT default_lang FROM clinics"
    )
    return {r[0] for r in rows}


def missing_by_language(ctx: AppContext) -> dict[str, list[str]]:
    if ctx.prompts is None:
        return {}
    extent = required_extent(ctx)
    return {lang: ctx.prompts.missing(lang, **extent) for lang in PATIENT_LANGS}


def _prompts(ctx: AppContext, missing: dict[str, list[str]]) -> list[CodedCheck]:
    in_use = languages_in_use(ctx)
    out = []
    for lang, files in missing.items():
        name = LANG_NAMES[lang]
        if not files:
            out.append(
                CodedCheck(
                    name=f"voice_{lang}",
                    code="voice.complete",
                    params={"lang": lang},
                    ok=True,
                    detail=f"{name}: every prompt has its audio",
                )
            )
            continue
        shown = ", ".join(files[:SHOW_MISSING]) + (
            f" and {len(files) - SHOW_MISSING} more" if len(files) > SHOW_MISSING else ""
        )
        out.append(
            CodedCheck(
                name=f"voice_{lang}",
                code="voice.missing_in_use" if lang in in_use else "voice.missing",
                params={"lang": lang, "count": len(files), "shown": shown},
                ok=False,
                alarm=lang in in_use,
                detail=f"{name}: {_plural(len(files), 'audio file is', 'audio files are')} missing ({shown})"
                + (" and patients use this language" if lang in in_use else ""),
            )
        )
    return out


def _behind_for_long(ctx: AppContext, behind: bool) -> bool:
    """True once the deletion job has been seen overdue for more than two of its rounds (policy scheduler
    purge_every_s) of real time. The demo clock jumps hours at a press, which leaves the job overdue until
    its next round: that is not a fault. A job that stays overdue is."""
    if not behind:
        ctx.extras.pop("purge_behind_since", None)
        return False
    since = ctx.extras.setdefault("purge_behind_since", time.monotonic())
    return time.monotonic() - since > 2 * ctx.policy["scheduler"]["purge_every_s"]


def _last_purge(ctx: AppContext) -> CodedCheck:
    row = ctx.db.one("SELECT value FROM kv WHERE key = 'last_purge_run'")
    if row is None:
        return CodedCheck(
            name="purge",
            code="purge.never_ran",
            ok=False,
            alarm=ctx.settings.scheduler and _behind_for_long(ctx, True),
            detail="The deletion job has not run yet",
        )
    last = clock.parse_iso(row["value"])
    on_time = last >= _latest_purge_time(ctx)
    n, unit = _ago(last)
    return CodedCheck(
        name="purge",
        code="purge.ran" if on_time else "purge.overdue",
        params={"n": n, "unit": unit},
        ok=on_time,
        alarm=ctx.settings.scheduler and _behind_for_long(ctx, not on_time),
        detail=f"The deletion job last ran {_count(n, unit)} ago"
        + ("" if on_time else ", before its daily time"),
    )


def _latest_purge_time(ctx: AppContext) -> datetime:
    """The most recent moment the daily deletion time (policy retention.purge_local_time) has passed."""
    tz = ctx.settings.local_tz
    today = clock.local_today(tz)
    at = clock.at_local(today, ctx.policy["retention"]["purge_local_time"], tz)
    return (
        at
        if at <= clock.now()
        else clock.at_local(today - timedelta(days=1), ctx.policy["retention"]["purge_local_time"], tz)
    )


def _ago(then: datetime) -> tuple[int, str]:
    """How long ago, as a whole number and its unit (second, minute or hour)."""
    seconds = int((clock.now() - then).total_seconds())
    if seconds < 90:
        return max(seconds, 0), "second"
    if seconds < 5400:
        return seconds // 60, "minute"
    return seconds // 3600, "hour"


def _alerts(ctx: AppContext) -> CodedCheck:
    waiting = alerts.unseen(ctx)
    minutes = ctx.policy["alerts"]["unseen_by_vet_minutes"]
    if waiting:
        return CodedCheck(
            name="alerts",
            code="alerts.unseen",
            params={"count": len(waiting), "minutes": minutes},
            ok=False,
            alarm=True,
            detail=f"{_plural(len(waiting), 'sick-dog alert has', 'sick-dog alerts have')} waited over {minutes} minutes with no vet looking",
        )
    return CodedCheck(
        name="alerts", code="alerts.none_unseen", ok=True, detail="No sick-dog alert is waiting unseen"
    )


def _reveals(ctx: AppContext) -> CodedCheck:
    """A login that reached its hourly limit for reading phone numbers is a burst worth a person looking at
    (the audit log has who and which patients)."""
    since = clock.iso(clock.now() - timedelta(hours=1))
    row = ctx.db.one(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT actor) AS who FROM audit WHERE action = 'phone.reveal_limited' AND at > ?",
        (since,),
    )
    if row["n"]:
        return CodedCheck(
            name="phone_reveals",
            code="reveals.burst",
            params={"count": row["n"], "logins": row["who"]},
            ok=False,
            alarm=True,
            detail=f"{_plural(row['who'], 'login has', 'logins have')} reached the hourly limit for reading phone numbers "
            f"({_plural(row['n'], 'request', 'requests')} refused in the last hour): see the audit log",
        )
    return CodedCheck(
        name="phone_reveals",
        code="reveals.none_limited",
        ok=True,
        detail="No login has reached the hourly limit for reading phone numbers",
    )


def _secret(ctx: AppContext) -> list[CodedCheck]:
    if not ctx.extras.get("generated_secret"):
        return []
    return [
        CodedCheck(
            name="session_key",
            code="session_key.kept",
            ok=True,
            detail="The session key was made on first start and is kept in var/demo_secret.key, so people stay signed in across a restart",
        )
    ]


def build_status(ctx: AppContext, detailed: bool = False) -> CodedStatus:
    """`detailed` is for the administrator alone: it adds the host and port of the phone line, the speech
    model's own error text and the check on phone-number reveals."""
    missing = missing_by_language(ctx)
    checks = [
        _database(ctx),
        _scheduler(ctx),
        _telephony(ctx, detailed),
        _asr(ctx, detailed),
        *_prompts(ctx, missing),
        _last_purge(ctx),
        _alerts(ctx),
        *_secret(ctx),
        *([_reveals(ctx)] if detailed else []),
    ]
    return CodedStatus(
        mode=ctx.settings.mode,
        telephony=ctx.settings.telephony,
        checks=checks,
        policy_version=ctx.policy.version,
        prompts_missing=missing,
    )


@router.get("/api/system/status")
def status(request: Request, who: Principal = Depends(reader("system"))) -> CodedStatus:
    return build_status(get_ctx(request), detailed=who.role == "admin")


@router.post("/api/system/telephony/reconnect")
async def reconnect_telephony(request: Request, who: Principal = Depends(writer("admin"))) -> CodedCheck:
    """Try the telephone link again after it failed to start or dropped. It answers with the telephony
    check as it stands afterwards; a link that is up is left alone."""
    ctx = get_ctx(request)
    if ctx.telephony is None:
        raise AuthError(
            "not_available", "No telephone link is set up on this server", 404, key="telephony.not_set_up"
        )
    await connect_telephony(ctx)
    check = _telephony(ctx, detailed=True)
    with ctx.db.transaction() as conn:
        audit.record(conn, who.actor, "telephony.reconnect", "system", None, {"ok": check.ok})
    return check


LOOPBACK = ("127.0.0.1", "localhost", "[::1]")


def qr_hosts(settings: Settings) -> set[str]:
    """The addresses a QR code may point to: CT_PUBLIC_URL's, and the one this server is configured to listen
    on (every name of this computer when that is the loopback or all addresses). Never the Host header the
    request arrived with: a request can say anything there."""
    hosts = set()
    if origin := public_origin(settings):
        hosts.add(urlsplit(origin).netloc)
    names = LOOPBACK if settings.host in (*LOOPBACK, "0.0.0.0", "::", "") else (settings.host,)  # noqa: S104
    hosts.update(f"{name}:{settings.port}" for name in names)
    return hosts


@router.get("/api/phone/qr.svg")
def qr(request: Request, url: str = Query(max_length=QR_URL_MAX)) -> Response:
    """A QR code for a link to this server (or to CT_PUBLIC_URL), so a visitor can open the phone page on a
    real phone. Nothing else is drawn: a link to anywhere else is refused."""
    ctx = get_ctx(request)
    allowed = qr_hosts(ctx.settings)
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or parts.netloc not in allowed
        or parts.username
        or parts.password
    ):
        raise AuthError("bad_request", "That link is not on this server", 400)
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=8, border=2, xmldecl=False, svgns=True, nl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml")
