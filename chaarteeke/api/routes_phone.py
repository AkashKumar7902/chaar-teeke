"""The browser phone's HTTP and WebSocket routes (CT_TELEPHONY=sim or ari,sim).

POST /api/phone/sessions opens a phone session for the signed-in person (a visitor too: this is the one
write a visitor may make) and returns {session, ws_url}. WS /api/phone/ws?session= is the phone itself;
its messages are listed in chaarteeke/telephony/sim.py. The caps come from policy.visitor: phones per
visitor and in all, how long a new session has to be attached (phone_session_attach_minutes), and how many
missed calls one visitor may give a minute (phone_missed_calls_per_minute, kept by the simulator).

Who opens a phone decides where its missed call goes, and the server decides it from the login, never from
the page: a clinic login's phone is that clinic's desk, so its missed call waits in that clinic's Today to be
linked; a visitor's and any other login's links at once to the demo visitor desk.

GET /api/phone/captions?lang= is public: the words of every clip the page may be asked to play, so the
browser phone can show what is being said. The words are the ones the voice was made from. With `with=en`
the reply also carries the English words (see english_captions), so a viewer who does not read the call's
language can follow it.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request, WebSocket
from fastapi.responses import JSONResponse

from .. import audit
from ..context import AppContext
from ..ivr.prompts import SLOT_DIR, PromptCatalogue, PromptError, UnknownPrompt
from ..telephony.mux import MuxPort
from ..telephony.sim import SimPort
from .auth import COOKIE, CSRF_HEADER, AuthError, Principal, check_csrf, principal_for
from .deps import current, get_ctx

router = APIRouter()
WS_REFUSED = 4401
CAPTION_LANGS = ("hi", "doi", "ur")  # the languages a call is spoken in; English is asked for with `with=en`
WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS_EN = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
NUMBER_SLOTS = frozenset({"n", "total", "clinic_phone", "register"})  # spoken as digit clips


def sim_port(ctx: AppContext) -> SimPort | None:
    """The browser phone, whether it runs alone or beside the PBX; None when it is switched off."""
    if isinstance(ctx.telephony, SimPort):
        return ctx.telephony
    if isinstance(ctx.telephony, MuxPort) and isinstance(ctx.telephony.sim, SimPort):
        return ctx.telephony.sim
    return None


def _refuse(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def _clinic_for(who: Principal) -> str | None:
    """The clinic whose desk this phone is: a clinic login's own. Anyone else's phone has none."""
    if who.role == "visitor" or who.view != "clinic":
        return None
    if not who.clinic_id:
        raise AuthError("no_clinic", "This login is not linked to a clinic", 409)
    return who.clinic_id


@router.post("/api/phone/sessions")
async def open_session(request: Request) -> JSONResponse:
    ctx = get_ctx(request)
    who = current(request)
    if who is None:
        raise AuthError("signed_out", "Sign in to continue", 401)
    check_csrf(who, request.headers.get(CSRF_HEADER))
    sim = sim_port(ctx)
    if sim is None:
        return _refuse(409, "phone_off", "The browser phone is switched off on this server.")
    clinic_id = _clinic_for(who)
    caps = ctx.policy["visitor"]
    address = request.client.host if request.client else None
    sim.drop_older_than(timedelta(minutes=caps["phone_session_attach_minutes"]))
    if sim.sessions_owned_by(who.actor) >= caps["phone_sessions_per_visitor"]:
        return _refuse(429, "phone_sessions_full", "You already have the most phones open. Close one first.")
    if address and sim.sessions_at(address) >= caps["phone_sessions_per_address"]:
        return _refuse(
            429, "phone_sessions_address", "This network already has the most phones open. Close one first."
        )
    # Staff may use every place; visitors leave the reserved ones free, so a room full of visitors cannot
    # lock the presenter out of the phone.
    room = caps["phone_sessions_total"] - (0 if who.can_write else caps["phone_sessions_reserved"])
    if sim.session_count() >= room:
        return _refuse(429, "phone_sessions_busy", "All the demo phones are in use. Try again in a minute.")
    session = sim.create_session(
        who.actor,
        missed_calls_per_minute=caps["phone_missed_calls_per_minute"],
        address=address,
        clinic_id=clinic_id,
        idle_s=caps["phone_socket_idle_s"],
        max_age_s=caps["phone_socket_max_s"],
    )
    with ctx.db.transaction() as conn:
        audit.record(conn, who.actor, "phone.session", "phone_session", None)
    base = (ctx.settings.public_url or str(request.base_url)).rstrip("/")
    ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return JSONResponse({"session": session.id, "ws_url": f"{ws_base}/api/phone/ws?session={session.id}"})


@router.websocket("/api/phone/ws")
async def phone_socket(websocket: WebSocket, session: str = "") -> None:
    ctx: AppContext = websocket.app.state.ctx
    sim = sim_port(ctx)
    entry = sim.session(session) if sim else None
    who = principal_for(ctx, websocket.cookies.get(COOKIE))
    # The session id is a secret only its owner holds; the owner's cookie must match as well.
    if entry is None or entry.ws is not None or who is None or who.actor != entry.owner:
        await websocket.close(code=WS_REFUSED)
        return
    sim.drop_older_than(timedelta(minutes=ctx.policy["visitor"]["phone_session_attach_minutes"]))
    if sim.session(session) is None:
        await websocket.close(code=WS_REFUSED)  # too late to attach: the session is gone, ask for a new one
        return
    await websocket.accept()
    await sim.attach(entry.id, websocket)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _gazetteer_words(entry: dict, lang: str) -> str | None:
    """Dogri is written in Devanagari, so it shares the Hindi name; Urdu needs its own."""
    return entry.get("name_ur") if lang == "ur" else entry.get("name_hi")


def caption_texts(data_dir: Path, lang: str) -> dict[str, str]:
    """The words of each clip of one language, keyed by its path under <lang>/ without '.wav'
    (FC_CLOSE.2, SYS_BYE, slot/day_22). A clip with no words in the data is left out: nothing is guessed."""
    prompts_json = data_dir / "prompts" / "prompts.json"
    meta = _read(prompts_json)["meta"]
    sources = _read(data_dir / "prompts" / "slot_sources.json")
    entries = _read(data_dir / "gazetteer.json")["entries"]
    by_id = {e["id"]: e for e in entries}
    ward_entries = {
        e["ward_nos"][0]: e for e in entries if e.get("type") == "ward" and len(e.get("ward_nos", [])) == 1
    }
    overrides = sources.get("texts", {}).get(lang, {})
    texts: dict[str, str] = {}
    catalogue = PromptCatalogue(prompts_json, prompts_json.parent / "audio")
    for item in catalogue.manifest(lang):
        if item.kind == "segment" and item.text and item.text.strip():
            texts[item.path.removeprefix(f"{lang}/").removesuffix(".wav")] = item.text.strip()
    dates = meta["spoken_dates"]
    slot: dict[str, str] = {}
    for i, words in enumerate(dates["weekdays"][lang]):
        slot[f"weekday_{i}"] = words
    for kind in ("day", "month"):
        for n, words in dates[f"{kind}s"][lang].items():
            slot[f"{kind}_{n}"] = words
    for digit in range(10):
        slot[f"num_{digit}"] = meta["spoken_numbers"][lang][str(digit)]
    for hhmm, words in meta["spoken_times"].get(lang, {}).items():
        slot[f"time_{hhmm}"] = words
    for clinic_id, source in sources.get("clinics", {}).items():
        words = overrides.get(f"clinic_{clinic_id}")
        if not words and source:
            words = _gazetteer_words(by_id[source["gazetteer_id"]], lang)
        if words:
            slot[f"clinic_{clinic_id}"] = words
    for ward_no, entry in ward_entries.items():
        words = overrides.get(f"ward_{ward_no}") or _gazetteer_words(entry, lang)
        if words:
            slot[f"ward_{ward_no}"] = words
    for name, words in slot.items():
        texts[f"{SLOT_DIR}/{name}"] = words
    return texts


def _english_slot(piece: str, clinics: dict[str, str], wards: dict[int, str]) -> str | None:
    """The English words of one slot clip (`weekday_3`, `time_0900`, `clinic_cln_gmc`), None when the data
    has none: a clip with no English words is left out, nothing is guessed."""
    kind, _, key = piece.partition("_")
    if kind == "weekday":
        return WEEKDAYS_EN[int(key)]
    if kind == "month":
        return MONTHS_EN[int(key) - 1]
    if kind in ("day", "num"):
        return str(int(key))
    if kind == "time":
        hour, minute = int(key[:2]), key[2:]
        return f"{hour % 12 or 12}:{minute} {'am' if hour < 12 else 'pm'}"
    if kind == "clinic":
        return clinics.get(f"clinic_{key}")
    if kind == "ward":
        return wards.get(int(key))
    raise PromptError(f"There is no English for the slot clip {piece}.")


def english_captions(data_dir: Path, lang: str, native: dict[str, str]) -> dict[str, dict[str, str]]:
    """The English words for the clips of one call language, keyed by the same clip keys as `native`.

    `slots`: the English of each slot clip (`slot/weekday_3` -> "Thursday").
    `texts`: the English of each whole line, under the key of the line's first file in this language
    (`FC_CLOSE.0`, or `SYS_BYE` for a line with no slots). English puts words in another order from Hindi,
    Dogri and Urdu, so a line cannot be captioned piece by piece; the page shows the English line when the
    first file starts. Where the line has slots, the English names each as `{run.place}`: run is the
    position of the group of slot clips in the language's own line, place is the position of the value
    inside that group (a date is one value). The page fills them from the slot clips it was told to play.
    """
    prompts_json = data_dir / "prompts" / "prompts.json"
    catalogue = PromptCatalogue(prompts_json, prompts_json.parent / "audio")
    clinics = _read(data_dir / "prompts" / "slot_sources.json").get("texts", {}).get("en", {})
    wards = {
        e["ward_nos"][0]: e["name"]
        for e in _read(data_dir / "gazetteer.json")["entries"]
        if e.get("type") == "ward" and len(e.get("ward_nos", [])) == 1
    }
    slots: dict[str, str] = {}
    for key in native:
        if key.startswith(f"{SLOT_DIR}/"):
            words = _english_slot(key.removeprefix(f"{SLOT_DIR}/"), clinics, wards)
            if words:
                slots[key] = words
    texts: dict[str, str] = {}
    for prompt_id in catalogue.ids():
        try:
            english = catalogue.text(prompt_id, "en")
        except UnknownPrompt:
            continue
        first, runs = catalogue.caption_frame(prompt_id, lang)
        if first in native:
            texts[first] = _place_slots(prompt_id, lang, english, runs)
    return {"texts": texts, "slots": slots}


def _place_slots(prompt_id: str, lang: str, english: str, runs: list[list[str]]) -> str:
    where: dict[str, tuple[int, int]] = {}
    for r, run in enumerate(runs):
        if any(a in NUMBER_SLOTS and b in NUMBER_SLOTS for a, b in zip(run, run[1:], strict=False)):
            raise PromptError(
                f"{prompt_id} ({lang}) has two numbers side by side; their digits cannot be told apart."
            )
        where.update({name: (r, i) for i, name in enumerate(run)})

    def put(match: re.Match[str]) -> str:
        if match.group(1) not in where:
            raise PromptError(
                f"{prompt_id}: the English uses {{{match.group(1)}}}, which the {lang} line does not."
            )
        r, i = where[match.group(1)]
        return f"{{{r}.{i}}}"

    return re.sub(r"\{(\w+)\}", put, english)


@router.get("/api/phone/captions")
async def captions(
    request: Request, lang: str = "", with_language: str | None = Query(None, alias="with")
) -> JSONResponse:
    """Public: the words behind every clip of one call language, for the captions on the browser phone.
    `with=en` adds the English words (`en`) beside them."""
    if lang not in CAPTION_LANGS:
        return _refuse(
            400, "bad_request", "Ask for hi, doi or ur: those are the languages a call is spoken in."
        )
    if with_language not in (None, "en"):
        return _refuse(400, "bad_request", "The only language that can be asked for beside it is en.")
    ctx = get_ctx(request)
    cache: dict[str, dict] = ctx.extras.setdefault("phone_captions", {})
    key = f"{lang}+{with_language}"
    if key not in cache:
        data_dir = ctx.settings.data_dir
        meta = _read(data_dir / "prompts" / "prompts.json")["meta"]
        texts = caption_texts(data_dir, lang)
        body: dict[str, Any] = {
            "lang": lang,
            "review_status": meta["review_status_by_language"][lang],
            "texts": texts,
        }
        if with_language:
            body["en"] = english_captions(data_dir, lang, texts)
        cache[key] = body
    return JSONResponse(cache[key])
