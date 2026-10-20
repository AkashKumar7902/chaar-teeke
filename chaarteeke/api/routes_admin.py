"""The administrator: clinics, people, the voice prompts and the retention report.

An administrator sets up the system. They see no recording, no transcript and no patient beyond what the
clinic desk shows. Passwords arrive in a request body, are hashed with auth.hash_password (the same scrypt
format the demo seed uses) and are never logged, audited or returned.
"""

# ruff: noqa: B008 (FastAPI reads `Depends(...)` in argument defaults)

from __future__ import annotations

import io
import json
import os
import re
import secrets
import wave
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .. import audit, clock, privacy, regimens
from ..context import AppContext
from ..ids import new_id
from ..ivr.prompts import PATIENT_LANGS, required_extent
from ..stats.clusters import ward_table
from . import auth
from .admin_setup import USERNAME
from .auth import MIN_PASSWORD_LENGTH, AuthError, Principal
from .deps import get_ctx, reader, writer

router = APIRouter()

LANG_NAMES = {"hi": "Hindi", "doi": "Dogri", "ur": "Urdu"}
VISIT_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Input rules for what an administrator types, not operating thresholds.
PHONE_DIGITS = (10, 13)
VOICE_KEY = "voice_upload:"  # kv rows that say which audio files an administrator uploaded


def _bad(
    code: str, message: str, status: int = 400, *, key: str, params: dict[str, str | int] | None = None
) -> AuthError:
    """A refusal. `key` names this exact sentence and `params` the values in it, so a page can translate it;
    the English `message` stays."""
    return AuthError(code, message, status, key=key, params=params or {})


# ----------------------------------------------------------------------- clinics
class ClinicIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    short_name: str = Field(min_length=1, max_length=30)
    ward_no: int | None = None
    default_regimen: str
    default_lang: Literal["hi", "doi", "ur"]
    arv_status: Literal["confirmed", "unverified"] = "unverified"
    visit_time: str | None = Field(default=None, max_length=5)  # HH:MM
    phone: str | None = Field(default=None, max_length=30)


class ClinicPatch(BaseModel):
    id: str
    name: str | None = Field(default=None, min_length=1, max_length=120)
    short_name: str | None = Field(default=None, min_length=1, max_length=30)
    ward_no: int | None = None
    default_regimen: str | None = None
    default_lang: Literal["hi", "doi", "ur"] | None = None
    arv_status: Literal["confirmed", "unverified"] | None = None
    visit_time: str | None = Field(default=None, max_length=5)
    phone: str | None = Field(default=None, max_length=30)


def _clinic_out(row) -> dict:
    gaps = []
    if not row["visit_time"]:
        gaps.append("No visit time: reminder calls for this clinic will fail until one is set")
    if not row["phone"]:
        gaps.append("No phone number: the vaccine-out reminder for this clinic will fail until one is set")
    return {
        "id": row["id"],
        "name": row["name"],
        "short_name": row["short_name"],
        "ward_no": row["ward_no"],
        "default_regimen": row["default_regimen"],
        "default_lang": row["default_lang"],
        "arv_status": row["arv_status"],
        "vaccine_out": bool(row["vaccine_out"]),
        "visit_time": row["visit_time"],
        "phone": row["phone"],
        "is_demo": bool(row["is_demo"]),
        "setup_gaps": gaps,
    }


def _visit_time(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not VISIT_TIME.match(value):
        raise _bad("bad_request", "The visit time is a time of day like 09:00.", key="clinic.bad_visit_time")
    return value


def _phone(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    digits = re.sub(r"\D", "", value)
    low, high = PHONE_DIGITS
    if not low <= len(digits) <= high or re.search(r"[^\d\s+()-]", value):
        raise _bad(
            "bad_request",
            f"Enter the clinic's phone number with its area code ({low} to {high} digits).",
            key="clinic.bad_phone",
            params={"low": low, "high": high},
        )
    return ("+" if value.strip().startswith("+") else "") + digits


def _check_clinic_fields(ctx: AppContext, regimen: str | None, ward_no: int | None) -> None:
    if regimen is not None:
        found = regimens.regimens().get(regimen)
        if found is None:
            raise _bad("unknown_regimen", "This vaccine schedule is not known.", key="regimen.unknown")
        if not found.enabled:
            raise _bad(
                "regimen_disabled", "This vaccine schedule is not switched on.", 409, key="regimen.disabled"
            )
    if ward_no is not None and ward_no not in ward_table(ctx.settings.data_dir / "wards.geojson"):
        raise _bad("unknown_ward", "That is not a ward number.", 404, key="ward.unknown")


def _language_ready(ctx: AppContext, lang: str) -> None:
    """SPEC section 7: a language with an audio file missing cannot be switched on for a clinic. This asks
    for the language-wide files (prompts, dates, numbers, wards); a clinic's own name clip comes after the
    clinic exists, and /api/system/status keeps reporting it until it is recorded."""
    cat = _catalogue(ctx)
    wards = sorted(ward_table(ctx.settings.data_dir / "wards.geojson"))
    missing = cat.missing(lang, ward_nos=wards)
    if missing:
        shown = ", ".join(missing[:3]) + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
        files = "1 audio file is" if len(missing) == 1 else f"{len(missing)} audio files are"
        raise _bad(
            "language_not_ready",
            f"{LANG_NAMES[lang]} cannot be switched on yet: {files} missing ({shown}). See the Prompts tab.",
            409,
            key="language.not_ready",
            params={"lang": lang, "count": len(missing), "shown": shown},
        )


@router.get("/api/admin/clinics")
def clinics(request: Request, who: Principal = Depends(reader("admin"))) -> list[dict]:
    ctx = get_ctx(request)
    return [_clinic_out(r) for r in ctx.db.query("SELECT * FROM clinics ORDER BY name, id")]


@router.get("/api/admin/regimens")
def regimen_list(request: Request, who: Principal = Depends(reader("admin"))) -> list[dict]:
    """Every vaccine schedule, switched on or not: the clinic form offers the enabled ones and says plainly
    when a clinic's own default has been switched off."""
    return [
        {"id": r.id, "name": r.name, "route": r.route, "days": list(r.days), "enabled": r.enabled}
        for r in regimens.regimens().values()
    ]


@router.post("/api/admin/clinics")
def clinic_add(body: ClinicIn, request: Request, who: Principal = Depends(writer("admin"))) -> dict:
    """Add a clinic. Not in the first API list, but a pilot has no other way to get a clinic in."""
    ctx = get_ctx(request)
    _check_clinic_fields(ctx, body.default_regimen, body.ward_no)
    _language_ready(ctx, body.default_lang)
    clinic_id = new_id("cln")
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO clinics(id, name, short_name, ward_no, default_regimen, default_lang, arv_status,"
            " vaccine_out, is_demo, visit_time, phone) VALUES (?,?,?,?,?,?,?,0,?,?,?)",
            (
                clinic_id,
                body.name.strip(),
                body.short_name.strip(),
                body.ward_no,
                body.default_regimen,
                body.default_lang,
                body.arv_status,
                1 if ctx.settings.is_demo else 0,
                _visit_time(body.visit_time),
                _phone(body.phone),
            ),
        )
        audit.record(conn, who.actor, "clinic.create", "clinic", clinic_id, {"name": body.name.strip()})
    return _clinic_out(ctx.db.one("SELECT * FROM clinics WHERE id = ?", (clinic_id,)))


@router.put("/api/admin/clinics")
def clinic_update(body: ClinicPatch, request: Request, who: Principal = Depends(writer("admin"))) -> dict:
    """Change the fields sent. A visit time, phone number or ward sent as null is cleared."""
    ctx = get_ctx(request)
    sent = body.model_fields_set - {"id"}
    _check_clinic_fields(ctx, body.default_regimen, body.ward_no)
    if body.default_lang is not None:
        have = ctx.db.one("SELECT default_lang FROM clinics WHERE id = ?", (body.id,))
        if have is not None and have["default_lang"] != body.default_lang:
            _language_ready(ctx, body.default_lang)
    values: dict[str, object] = {}
    for field in sent:
        value = getattr(body, field)
        if field == "visit_time":
            value = _visit_time(value)
        elif field == "phone":
            value = _phone(value)
        elif isinstance(value, str):
            value = value.strip()
        if value is None and field not in ("visit_time", "phone", "ward_no"):
            raise _bad("bad_request", f"{field} cannot be empty.", key="field.empty", params={"field": field})
        values[field] = value
    with ctx.db.transaction() as conn:
        if conn.execute("SELECT 1 FROM clinics WHERE id = ?", (body.id,)).fetchone() is None:
            raise _bad("unknown_clinic", "This clinic is not in the system.", 404, key="clinic.unknown")
        if values:
            conn.execute(
                f"UPDATE clinics SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                (*values.values(), body.id),
            )
            audit.record(conn, who.actor, "clinic.update", "clinic", body.id, {"fields": sorted(values)})
    return _clinic_out(ctx.db.one("SELECT * FROM clinics WHERE id = ?", (body.id,)))


# ------------------------------------------------------------------------ people
class PersonIn(BaseModel):
    username: str = Field(max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    role: Literal["clinic", "abc", "vet", "oversight", "reviewer", "admin"]
    clinic_id: str | None = None
    password: str = Field(max_length=256)


class PersonPatch(BaseModel):
    id: str
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    role: Literal["clinic", "abc", "vet", "oversight", "reviewer", "admin"] | None = None
    clinic_id: str | None = None
    active: bool | None = None
    password: str | None = Field(default=None, max_length=256)


def _person_out(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "clinic_id": row["clinic_id"],
        "clinic_name": row["clinic_name"],
        "active": bool(row["active"]),
        "is_demo": bool(row["is_demo"]),
    }


_PERSON_SELECT = "SELECT s.*, c.name AS clinic_name FROM staff s LEFT JOIN clinics c ON c.id = s.clinic_id "


def _password(value: str) -> str:
    if len(value) < MIN_PASSWORD_LENGTH:
        raise _bad(
            "weak_password",
            f"A password needs at least {MIN_PASSWORD_LENGTH} characters.",
            key="password.weak",
            params={"min": MIN_PASSWORD_LENGTH},
        )
    return auth.hash_password(value)


def _clinic_for_role(ctx: AppContext, role: str, clinic_id: str | None) -> str | None:
    """A clinic login belongs to one clinic; every other role belongs to none."""
    if role != "clinic":
        return None
    if not clinic_id:
        raise _bad("bad_request", "A clinic login needs the clinic it belongs to.", key="person.needs_clinic")
    if ctx.db.one("SELECT 1 FROM clinics WHERE id = ?", (clinic_id,)) is None:
        raise _bad("unknown_clinic", "This clinic is not in the system.", 404, key="clinic.unknown")
    return clinic_id


@router.get("/api/admin/people")
def people(request: Request, who: Principal = Depends(reader("admin"))) -> list[dict]:
    ctx = get_ctx(request)
    return [_person_out(r) for r in ctx.db.query(_PERSON_SELECT + "ORDER BY s.role, s.username")]


@router.post("/api/admin/people")
def person_add(body: PersonIn, request: Request, who: Principal = Depends(writer("admin"))) -> dict:
    ctx = get_ctx(request)
    username = body.username.strip()
    if not USERNAME.match(username):
        raise _bad(
            "bad_request",
            "A username is 3 to 32 lower-case letters, digits, dots, dashes or underscores.",
            key="person.bad_username",
        )
    clinic_id = _clinic_for_role(ctx, body.role, body.clinic_id)
    pw_hash = _password(body.password)
    staff_id = new_id("stf")
    with ctx.db.transaction() as conn:
        if conn.execute("SELECT 1 FROM staff WHERE username = ?", (username,)).fetchone():
            raise _bad(
                "username_taken", "Someone already has that username.", 409, key="person.username_taken"
            )
        conn.execute(
            "INSERT INTO staff(id, username, display_name, role, clinic_id, password_hash, active, is_demo)"
            " VALUES (?,?,?,?,?,?,1,?)",
            (
                staff_id,
                username,
                body.display_name.strip(),
                body.role,
                clinic_id,
                pw_hash,
                1 if ctx.settings.is_demo else 0,
            ),
        )
        audit.record(
            conn, who.actor, "staff.create", "staff", staff_id, {"username": username, "role": body.role}
        )
    return _person_out(ctx.db.one(_PERSON_SELECT + "WHERE s.id = ?", (staff_id,)))


@router.put("/api/admin/people")
def person_update(body: PersonPatch, request: Request, who: Principal = Depends(writer("admin"))) -> dict:
    """Change the fields sent. A new password ends that person's logins, as does a change of role or clinic
    or switching them off. The last working administrator cannot be switched off or given another role."""
    ctx = get_ctx(request)
    with ctx.db.transaction() as conn:
        row = conn.execute("SELECT * FROM staff WHERE id = ?", (body.id,)).fetchone()
        if row is None:
            raise _bad("not_found", "This person is not in the system.", 404, key="person.unknown")
        role = body.role or row["role"]
        active = row["active"] if body.active is None else int(body.active)
        clinic_id = _clinic_for_role(
            ctx, role, body.clinic_id if "clinic_id" in body.model_fields_set else row["clinic_id"]
        )
        loses_admin = row["role"] == "admin" and row["active"] and (role != "admin" or not active)
        if loses_admin:
            others = conn.execute(
                "SELECT COUNT(*) FROM staff WHERE role = 'admin' AND active = 1 AND id != ?", (body.id,)
            ).fetchone()[0]
            if not others:
                raise _bad(
                    "last_admin",
                    "There must be one working administrator. Add another first.",
                    409,
                    key="person.last_admin",
                )
        changes: dict[str, object] = {"role": role, "active": active, "clinic_id": clinic_id}
        if body.display_name is not None:
            changes["display_name"] = body.display_name.strip()
        if body.password is not None:
            changes["password_hash"] = _password(body.password)
        conn.execute(
            f"UPDATE staff SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
            (*changes.values(), body.id),
        )
        ends_logins = (
            body.password is not None or role != row["role"] or clinic_id != row["clinic_id"] or not active
        )
        if ends_logins:
            auth.revoke_sessions(conn, body.id)
        audit.record(
            conn,
            who.actor,
            "staff.update",
            "staff",
            body.id,
            {
                "fields": sorted(k for k in changes if k != "password_hash"),
                "password_reset": body.password is not None,
            },
        )
    return _person_out(ctx.db.one(_PERSON_SELECT + "WHERE s.id = ?", (body.id,)))


# ---------------------------------------------------------------------- prompts
def _prompt_texts(ctx: AppContext) -> dict[str, dict]:
    raw = json.loads((ctx.settings.data_dir / "prompts" / "prompts.json").read_text(encoding="utf-8"))
    return {p["id"]: p for p in raw["prompts"]}


def _uploaded(ctx: AppContext) -> dict[str, dict]:
    return {
        r["key"].removeprefix(VOICE_KEY): json.loads(r["value"])
        for r in ctx.db.query("SELECT key, value FROM kv WHERE key LIKE ?", (VOICE_KEY + "%",))
    }


def _catalogue(ctx: AppContext):
    if ctx.prompts is None:
        raise _bad("not_available", "The prompt catalogue is not loaded.", 409, key="prompts.not_loaded")
    return ctx.prompts


@router.get("/api/admin/prompts")
def prompts(request: Request, who: Principal = Depends(reader("admin"))) -> dict:
    """Per language: every prompt with its words, whether its audio is there, its review status and whether
    the voice is the machine draft or a recording an administrator uploaded; and the files still missing."""
    ctx = get_ctx(request)
    cat = _catalogue(ctx)
    extent = required_extent(ctx)
    texts = _prompt_texts(ctx)
    uploaded = _uploaded(ctx)
    languages = []
    for lang in PATIENT_LANGS:
        segments: dict[str, list[str]] = {}
        for item in cat.manifest(lang, **extent):
            if item.kind == "segment":
                segments.setdefault(item.prompt_id, []).append(item.path)
        rows = []
        for pid in cat.ids():
            meta = texts.get(pid, {})
            present = cat.has_audio(lang, pid)
            files = segments.get(pid, [])
            rows.append(
                {
                    "id": pid,
                    "call": meta.get("call"),
                    "text": cat.text(pid, lang),
                    "has_audio": present,
                    "review_status": (meta.get("review_status") or {}).get(lang),
                    "voice": None
                    if not present
                    else "recording uploaded"
                    if files and all(f in uploaded for f in files)
                    else "machine voice draft",
                }
            )
        missing = cat.missing(lang, **extent)
        languages.append(
            {
                "lang": lang,
                "name": LANG_NAMES[lang],
                "ready": not missing,
                "missing": missing,
                "prompts": rows,
            }
        )
    return {"version": cat.version, "languages": languages}


def _target(ctx: AppContext, lang: str, name: str) -> tuple[str, os.PathLike]:
    """The one file an upload may replace: a file the catalogue says the voice must provide."""
    if lang not in PATIENT_LANGS:
        raise _bad("unknown_prompt", "There is no such language.", 404, key="prompt.unknown_language")
    cat = _catalogue(ctx)
    rel = f"{lang}/{name}.wav"
    if rel not in {item.path for item in cat.manifest(lang, **required_extent(ctx))}:
        raise _bad("unknown_prompt", "There is no such prompt file.", 404, key="prompt.unknown_file")
    return rel, cat.audio_dir / rel


def check_wav(data: bytes) -> None:
    """Accept only a readable WAV: RIFF/WAVE, 16-bit PCM, mono, with sound in it."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise _bad("bad_wav", "This is not a WAV file.", key="wav.not_wav")
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, frames = w.getnchannels(), w.getsampwidth(), w.getnframes()
    except (wave.Error, EOFError) as e:
        raise _bad(
            "bad_wav", "This WAV file cannot be read (it must be plain PCM).", key="wav.unreadable"
        ) from e
    if channels != 1 or width != 2:
        raise _bad("bad_wav", "The recording must be mono, 16-bit.", key="wav.format")
    if frames == 0:
        raise _bad("bad_wav", "The recording is empty.", key="wav.empty")


async def _body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise _bad("too_large", "The recording is too large.", 413, key="wav.too_large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _bad("too_large", "The recording is too large.", 413, key="wav.too_large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/api/admin/prompts/{lang}/{name:path}")
async def prompt_upload(
    lang: str, name: str, request: Request, who: Principal = Depends(writer("admin"))
) -> dict:
    """Replace one audio file with a recording: the request body is the WAV itself. `name` is the file's
    path under the language folder without .wav (FC_HELLO, FC_CLOSE.0, slot/day_3), as the missing list
    shows it."""
    ctx = get_ctx(request)
    rel, path = _target(ctx, lang, name)
    data = await _body(request, ctx.policy["limits"]["upload_body_max_bytes"])
    check_wav(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (
                VOICE_KEY + rel,
                json.dumps({"by": who.actor, "at": clock.iso(clock.now()), "bytes": len(data)}),
            ),
        )
        audit.record(conn, who.actor, "prompt.upload", "prompt", rel, {"bytes": len(data)})
    return {"ok": True, "path": rel, "bytes": len(data), "voice": "recording uploaded"}


# ---------------------------------------------------------------------- retention
@router.get("/api/admin/retention")
def retention(request: Request, who: Principal = Depends(reader("admin"))) -> dict:
    return privacy.retention_status(get_ctx(request))


# ------------------------------------------------------------------------ audit
AUDIT_PAGE_MAX = 200
# The actions that touch a patient's phone number come first: they are what an audit is mostly read for.
PHONE_ACTIONS = ("phone.reveal", "phone.reveal_refused", "phone.reveal_limited")


def _audit_row(r) -> dict:
    return {
        "seq": r["seq"],
        "at": r["at"],
        "actor": r["actor"],
        "action": r["action"],
        "object_type": r["object_type"],
        "object_id": r["object_id"],
        "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
    }


@router.get("/api/admin/audit")
def audit_log(
    request: Request,
    action: str | None = Query(default=None, max_length=64),
    object_type: str | None = Query(default=None, max_length=32),
    object_id: str | None = Query(default=None, max_length=64),
    actor: str | None = Query(default=None, max_length=64),
    phone_first: bool = False,
    limit: int = Query(default=50, ge=1, le=AUDIT_PAGE_MAX),
    offset: int = Query(default=0, ge=0),
    who: Principal = Depends(reader("admin")),
) -> dict:
    """The audit log, newest first, a page at a time. `action` matches exactly, or as a prefix when it ends
    with a dot (`phone.`). With `phone_first` the phone-number actions come before everything else. The log
    holds no phone number; `actions` lists every action seen, with how often, for a filter menu."""
    ctx = get_ctx(request)
    where, params = [], []
    if action:
        if action.endswith("."):
            where.append("substr(action, 1, ?) = ?")
            params += [len(action), action]
        else:
            where.append("action = ?")
            params.append(action)
    for column, value in (("object_type", object_type), ("object_id", object_id), ("actor", actor)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    marks = ", ".join("?" * len(PHONE_ACTIONS))
    order = f"(action IN ({marks})) DESC, seq DESC" if phone_first else "seq DESC"
    rows = ctx.db.query(
        f"SELECT * FROM audit{clause} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, *(PHONE_ACTIONS if phone_first else ()), limit, offset),
    )
    total = ctx.db.one(f"SELECT COUNT(*) AS n FROM audit{clause}", tuple(params))["n"]
    seen = ctx.db.query("SELECT action, COUNT(*) AS n FROM audit GROUP BY action ORDER BY action")
    actions = sorted(
        ({"action": r["action"], "count": r["n"]} for r in seen),
        key=lambda a: (a["action"] not in PHONE_ACTIONS, a["action"]),
    )
    return {
        "rows": [_audit_row(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "actions": actions,
    }


# ------------------------------------------------------------- sign-in locks
class SigninClearIn(BaseModel):
    username: str | None = Field(default=None, max_length=64)
    address: str | None = Field(default=None, max_length=64)


@router.get("/api/admin/signin-locks")
def signin_locks(request: Request, who: Principal = Depends(reader("admin"))) -> list[dict]:
    """Who is being made to wait after wrong passwords: the username and address (or an address alone), how
    many wrong tries and how many seconds are left. Signed-in administrators only: it lists addresses."""
    if who.role != "admin":
        raise AuthError("forbidden", "Your role can't see this", 403)
    return auth.signin_locks(get_ctx(request))


@router.post("/api/admin/signin-locks/clear")
def signin_locks_clear(
    body: SigninClearIn, request: Request, who: Principal = Depends(writer("admin"))
) -> dict:
    """Let a username (from every address), an address, or one username at one address try again."""
    if not body.username and not body.address:
        raise _bad("bad_request", "Say which username or address to clear.", key="signin.clear_who")
    ctx = get_ctx(request)
    cleared = auth.clear_signin_locks(ctx, body.username, body.address)
    with ctx.db.transaction() as conn:
        audit.record(
            conn,
            who.actor,
            "signin.lock_clear",
            "signin",
            None,
            {"username": body.username, "address": body.address, "cleared": cleared},
        )
    return {"cleared": cleared}
