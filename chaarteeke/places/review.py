"""From a recorded place to a ward: speech to text, the match, and the reviewer's queue.

A clip is first transcribed (or marked "speech model off"), then matched. Only a `likely` match places the
report by itself, and then the clip is deleted at once. Everything else waits in the queue for a person, who
listens, picks a ward (or "outside Jammu", or "can't tell") and may teach the system the name. A decision
deletes the clip and the transcript. Nothing here stands in for a missing step: a failed transcription is
shown as failed, with the reason, and the reviewer still hears the clip.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from .. import alerts, audit, clock
from ..context import AppContext
from ..models import Candidate, DecideIn, ReviewItem
from ..privacy import clip_path
from . import kws
from .matcher import MatchResult, match
from .normalise import alias_text

log = logging.getLogger(__name__)

ACTOR = "system:review"
MAX_OUTSIDE_LEN = 80
_BAD_CLIP = "This recording's file is outside the recordings folder, so it was not opened or deleted."
_NUMBERISH = re.compile(r"\+?\d[\d\s\-()]{6,}\d|sim:[\w-]+")
_PATHISH = re.compile(r"(?:/[\w.\-]+){2,}")
_DECIDED = ("decided", "outside_jmc", "cant_tell", "skipped", "no_place_heard")


class ReviewError(ValueError):
    """code: not_found, not_in_queue, already_decided, bad_decision, bad_ward, no_audio, bad_clip, no_name,
    read_only, forbidden, not_ready."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def scrub(text: str) -> str:
    """An error text safe to store and show: no phone numbers, no file paths, one short line."""
    one = " ".join(_PATHISH.sub("[path removed]", _NUMBERISH.sub("[number removed]", text)).split())
    return one[:300]


# ------------------------------------------------------------------------ after a call
def process_capture(ctx: AppContext, report_id: str) -> None:
    """Transcribe the saved clip, match it, and place the report or queue it. Runs in a worker thread."""
    cap = _capture(ctx, report_id)
    if cap is None or cap["asr_status"] != "pending" or cap["clip_deleted_at"] is not None:
        return
    if cap["location_status"] in _DECIDED:
        return
    transcript: str | None = None
    frames: Any = None
    model: str | None = None
    status, error = "pending", None
    audio: bytes | None = None
    try:
        audio = _read_clip(ctx, cap["audio_path"])
        if audio is None:
            status, error = "no_audio", "The recording file is missing."
    except ValueError:
        status, error = (
            "no_audio",
            _BAD_CLIP,
        )  # never opened, so nobody could hear it: it stays out of the queue
    except OSError as e:
        status, error = "no_audio", scrub(f"The recording could not be read ({type(e).__name__}).")
    if audio is not None:
        if ctx.transcriber is not None:
            try:
                heard = ctx.transcriber.transcribe(audio, _lang_of(ctx, cap))
                transcript, model, status, frames = heard.text, heard.model, "done", heard.frames
            except Exception as e:  # any failure of the speech step is shown, with its reason
                status, error = "failed", scrub(f"{type(e).__name__}: {e}")
        elif ctx.settings.asr == "off":
            status = "off"
        else:
            status, error = "failed", _model_not_ready(ctx)
    if status == "failed":
        log.error("speech to text failed for %s: %s", report_id, error)
    if (transcript or frames is not None) and ctx.gazetteer is None:
        result = MatchResult("none")
        status, error = "failed", "The list of place names is not loaded, so the words were not matched."
        log.error("place match skipped for %s: %s", report_id, error)
    else:
        try:
            result = _match(ctx, transcript, frames)
        except Exception as e:  # the keyword search failed: say so; nothing stands in for it
            result = MatchResult("none")
            status, error = (
                "failed",
                scrub(f"Searching the sound for place names failed: {type(e).__name__}: {e}"),
            )
            log.error("keyword spotting failed for %s: %s", report_id, error)
    if result.confidence == "likely" and ctx.clusters is None:
        result = MatchResult("possible", result.candidates, result.heard)
        status, error = "failed", "The analysis areas are not loaded, so the place was not set by itself."
        log.error("place not set for %s: %s", report_id, error)
    _store(ctx, report_id, cap, status, error, transcript, model, result)
    ctx.bus.publish("system", {"check": "speech", "ok": status in ("done", "off")})


def process_pending(ctx: AppContext) -> int:
    """Process every recording still waiting for its speech step: the server stopped between saving a clip and
    reading it. Call once at start-up, after the speech model has loaded. Returns how many were read."""
    rows = ctx.db.query(
        "SELECT pc.report_id FROM place_captures pc JOIN bite_reports r ON r.id = pc.report_id"
        " WHERE pc.asr_status = 'pending' AND pc.clip_deleted_at IS NULL ORDER BY r.created_at, r.id"
    )
    for r in rows:
        process_capture(ctx, r["report_id"])
    return len(rows)


def _model_not_ready(ctx: AppContext) -> str:
    """Why there is no speech model although CT_ASR asks for one (the app loads it in the background)."""
    state = ctx.extras.get("asr_state")
    if state == "failed":
        return scrub(f"The speech model did not load: {ctx.extras.get('asr_error', 'no reason was kept')}")
    return "The speech model was still loading when this recording came in."


def _match(ctx: AppContext, transcript: str | None, frames: Any = None) -> MatchResult:
    """The transcript matcher alone when the transcriber gave no frames (a test double; the real model always
    does), otherwise the transcript matcher and the keyword spotter together (places/kws.py)."""
    text = match(ctx.gazetteer, transcript, ctx.policy) if transcript else MatchResult("none")
    if frames is None:
        return text
    cfg = ctx.policy["matcher"]
    found = kws.spotter_for(ctx.gazetteer).search(frames, cfg["kws_possible_min"])
    return kws.combine(text, found, cfg, transcript or "")


def _store(
    ctx: AppContext,
    report_id: str,
    cap: sqlite3.Row,
    status: str,
    error: str | None,
    transcript: str | None,
    model: str | None,
    result: MatchResult,
) -> None:
    cands = [
        Candidate(
            ward_no=c.ward_no,
            ward_name=ctx.gazetteer.ward_name(c.ward_no),
            label="likely" if result.confidence == "likely" and i == 0 else "possible",
            evidence=c.evidence,
        ).model_dump()
        for i, c in enumerate(result.candidates)
    ]
    now = clock.iso(clock.now())
    with ctx.db.transaction() as conn:
        live = conn.execute(
            "SELECT pc.asr_status, pc.clip_deleted_at, r.location_status, r.ward_no FROM place_captures pc"
            " JOIN bite_reports r ON r.id = pc.report_id WHERE pc.report_id = ?",
            (report_id,),
        ).fetchone()
        if live is None or live["asr_status"] != "pending" or live["clip_deleted_at"] is not None:
            return  # the clip went (a withdrawal, a purge) or was re-recorded while we worked: drop this result
        if live["location_status"] in _DECIDED:
            return
        if result.confidence == "likely":
            ward = result.candidates[0].ward_no
            cluster = _cluster(ctx, ward)
            if cap["audio_path"]:
                _delete_clip(ctx, cap["audio_path"])
            conn.execute(
                "UPDATE place_captures SET asr_status=?, asr_error=?, transcript=NULL, asr_model=?,"
                " candidates_json=NULL, confidence='likely', decided_by=?, decided_at=?, clip_deleted_at=?,"
                " audio_path=NULL WHERE report_id=?",
                (status, error, model, ACTOR, now, now, report_id),
            )
            conn.execute(
                "INSERT INTO purge_log(course_id, what, reason, purged_at) VALUES (?, 'clip', 'decided', ?)",
                (cap["course_id"], now),
            )
            conn.execute(
                "UPDATE bite_reports SET ward_no=?, cluster_id=?, outside_tehsil=NULL, location_status='matched'"
                " WHERE id=?",
                (ward, cluster, report_id),
            )
            audit.record(
                conn, ACTOR, "review.matched", "bite_report", report_id, {"ward_no": ward, "model": model}
            )
            if live["ward_no"] != ward:
                alerts.update_location(ctx, conn, report_id)
        else:
            conn.execute(
                "UPDATE place_captures SET asr_status=?, asr_error=?, transcript=?, asr_model=?,"
                " candidates_json=?, confidence=? WHERE report_id=?",
                (
                    status,
                    error,
                    transcript,
                    model,
                    json.dumps(cands, ensure_ascii=False),
                    result.confidence,
                    report_id,
                ),
            )
            # With no recording there is nothing for a person to listen to, so it does not enter the queue.
            where = "no_place_heard" if status == "no_audio" else "needs_review"
            conn.execute(
                "UPDATE bite_reports SET ward_no=NULL, cluster_id=NULL, location_status=? WHERE id=?",
                (where, report_id),
            )
            audit.record(
                conn,
                ACTOR,
                "review.queued" if where == "needs_review" else "review.no_recording",
                "bite_report",
                report_id,
                {"asr_status": status, "confidence": result.confidence},
            )
            if live["ward_no"] is not None:
                alerts.update_location(ctx, conn, report_id)
    _announce(ctx, report_id)


def _capture(ctx: AppContext, report_id: str) -> sqlite3.Row | None:
    return ctx.db.one(
        "SELECT pc.*, r.location_status, r.course_id, r.cluster_id FROM place_captures pc"
        " JOIN bite_reports r ON r.id = pc.report_id WHERE pc.report_id = ?",
        (report_id,),
    )


def _clip_file(ctx: AppContext, audio_path: str) -> tuple[Path, bool]:
    """The file behind a stored clip path, and whether this system owns it. Recordings of calls live in
    var/clips and are ours to delete. The demo's example recordings ship in data/demo/clips: they can be
    heard, but they are shared by every demo and are never deleted (the row simply lets go of them)."""
    try:
        return clip_path(ctx, audio_path), True
    except ValueError:
        shipped = (ctx.settings.data_dir / "demo" / "clips").resolve()
        p = Path(audio_path)
        resolved = (p if p.is_absolute() else ctx.settings.data_dir.parent / p).resolve()
        if resolved.is_relative_to(shipped):
            return resolved, False
        raise


def _delete_clip(ctx: AppContext, audio_path: str) -> None:
    path, owned = _clip_file(ctx, audio_path)
    if owned:
        path.unlink(missing_ok=True)


def _read_clip(ctx: AppContext, audio_path: str | None) -> bytes | None:
    if not audio_path:
        return None
    path, _ = _clip_file(ctx, audio_path)
    return path.read_bytes() if path.is_file() else None


def _lang_of(ctx: AppContext, cap: sqlite3.Row) -> str:
    row = ctx.db.one("SELECT lang FROM courses WHERE id = ?", (cap["course_id"],))
    if row is None:
        raise ReviewError("not_found", "The call's language is not known, because the course is gone.")
    return row["lang"]


def _cluster(ctx: AppContext, ward_no: int) -> str:
    if ctx.clusters is None:
        raise ReviewError("not_ready", "The analysis areas are not loaded.")
    return ctx.clusters.for_ward(ward_no)


def _announce(ctx: AppContext, report_id: str) -> None:
    r = ctx.db.one(
        "SELECT cluster_id, animal, location_status, source FROM bite_reports WHERE id = ?", (report_id,)
    )
    ctx.bus.publish(
        "report",
        {
            "report_id": report_id,
            "cluster_id": r["cluster_id"],
            "animal": r["animal"],
            "location_status": r["location_status"],
            "simulated": r["source"] == "simulator",
        },
    )
    ctx.bus.publish("review", {"remaining": remaining(ctx)})


# -------------------------------------------------------------------------- the queue
_QUEUE = (
    "FROM bite_reports r JOIN place_captures pc ON pc.report_id = r.id"
    " WHERE r.location_status = 'needs_review' AND pc.clip_deleted_at IS NULL"
)


def remaining(ctx: AppContext) -> int:
    return ctx.db.one(f"SELECT COUNT(*) AS n {_QUEUE}")["n"]


def next_item(ctx: AppContext, skip: int = 0) -> ReviewItem | None:
    """The oldest report waiting for a person (skip 0), the one after it (skip 1), and so on; None when
    fewer than skip + 1 wait. `remaining` is the whole queue either way."""
    r = ctx.db.one(f"SELECT r.id {_QUEUE} ORDER BY r.created_at, r.id LIMIT 1 OFFSET ?", (skip,))
    return None if r is None else _item(ctx, r["id"])


def get_item(ctx: AppContext, report_id: str) -> ReviewItem:
    r = ctx.db.one(f"SELECT r.id {_QUEUE} AND r.id = ?", (report_id,))
    if r is None:
        raise ReviewError("not_in_queue", "That place is not waiting for review")
    return _item(ctx, report_id)


def _item(ctx: AppContext, report_id: str) -> ReviewItem:
    r = ctx.db.one(
        "SELECT r.created_at, pc.* FROM bite_reports r JOIN place_captures pc ON pc.report_id = r.id WHERE r.id = ?",
        (report_id,),
    )
    raw: list[dict[str, Any]] = json.loads(r["candidates_json"]) if r["candidates_json"] else []
    cands = [
        Candidate(
            ward_no=c["ward_no"],
            ward_name=ctx.gazetteer.ward_name(c["ward_no"]),
            label=c["label"],
            evidence=c["evidence"],
        )
        for c in raw
    ]
    return ReviewItem(
        report_id=report_id,
        heard_at=r["created_at"],
        transcript=r["transcript"],
        asr_status=r["asr_status"],
        asr_error=r["asr_error"],
        audio_available=_has_audio(ctx, r["audio_path"]),
        candidates=cands,
        remaining=remaining(ctx),
        expires_at=r["expires_at"],
    )


def _has_audio(ctx: AppContext, audio_path: str | None) -> bool:
    if not audio_path:
        return False
    try:
        return _clip_file(ctx, audio_path)[0].is_file()
    except ValueError:
        return False  # a path outside the recordings folders is never opened; one bad row must not hide the queue


def audio(ctx: AppContext, report_id: str) -> bytes:
    """The clip's bytes, for the reviewer's player. Gone once the report is decided or the clip expires."""
    r = ctx.db.one("SELECT audio_path, clip_deleted_at FROM place_captures WHERE report_id = ?", (report_id,))
    if r is None:
        raise ReviewError("not_found", "There is no such recording")
    try:
        data = None if r["clip_deleted_at"] else _read_clip(ctx, r["audio_path"])
    except ValueError:
        raise ReviewError("bad_clip", _BAD_CLIP) from None
    if data is None:
        raise ReviewError("no_audio", "This recording has been deleted")
    return data


# ------------------------------------------------------------------------- a decision
def _reviewer(ctx: AppContext, actor: str) -> None:
    if actor.startswith("visitor:"):
        raise ReviewError("read_only", "Your role can't do this")
    row = (
        ctx.db.one("SELECT role FROM staff WHERE username = ? AND active = 1", (actor[len("staff:") :],))
        if actor.startswith("staff:")
        else None
    )
    if row is None or row["role"] != "reviewer":
        raise ReviewError("forbidden", "Your role can't do this")


def decide(ctx: AppContext, report_id: str, data: DecideIn, actor: str) -> int:
    """Place a waiting report. Exactly one of ward_no, outside_tehsil, cant_tell. Deletes the clip and the
    transcript. Returns how many reports are still waiting."""
    _reviewer(ctx, actor)
    outside = " ".join((data.outside_tehsil or "").split())
    chosen = [data.ward_no is not None, bool(outside), data.cant_tell]
    if sum(chosen) != 1:
        raise ReviewError("bad_decision", "Choose one: a ward, outside Jammu, or can't tell")
    if len(outside) > MAX_OUTSIDE_LEN:
        raise ReviewError("bad_decision", f"Keep the place name under {MAX_OUTSIDE_LEN} letters")
    if outside and scrub(outside) != outside:
        raise ReviewError("bad_decision", "Write only the name of the place, with no number or file path")
    if data.ward_no is not None and not ctx.gazetteer.has_ward(data.ward_no):
        raise ReviewError("bad_ward", "That is not a ward of Jammu")
    if data.remember_name and data.ward_no is None:
        raise ReviewError("bad_decision", "A name can only be remembered for a ward")
    cluster = _cluster(ctx, data.ward_no) if data.ward_no is not None else None
    now = clock.iso(clock.now())
    with ctx.db.transaction() as conn:
        row = conn.execute(
            "SELECT r.location_status, r.ward_no, r.course_id, pc.transcript, pc.audio_path, pc.clip_deleted_at"
            " FROM bite_reports r JOIN place_captures pc ON pc.report_id = r.id WHERE r.id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ReviewError("not_found", "There is no such place to review")
        if row["location_status"] in _DECIDED or row["location_status"] == "matched":
            raise ReviewError("already_decided", "This place has already been decided")
        if row["location_status"] != "needs_review":
            raise ReviewError("not_in_queue", "That place is not waiting for review")
        alias = ""
        if data.remember_name:
            alias = alias_text(row["transcript"] or "")
            if not alias:
                raise ReviewError("no_name", "There is no place name in what was said to remember")
        if row["audio_path"]:
            try:
                _delete_clip(ctx, row["audio_path"])  # if this fails, nothing is decided
            except ValueError:
                raise ReviewError("bad_clip", _BAD_CLIP) from None
        if data.ward_no is not None:
            status, ward, tehsil = "decided", data.ward_no, None
        elif outside:
            status, ward, tehsil = "outside_jmc", None, outside
        else:
            status, ward, tehsil = "cant_tell", None, None
        conn.execute(
            "UPDATE bite_reports SET ward_no=?, cluster_id=?, outside_tehsil=?, location_status=? WHERE id=?",
            (ward, cluster, tehsil, status, report_id),
        )
        conn.execute(
            "UPDATE place_captures SET decided_by=?, decided_at=?, clip_deleted_at=?, audio_path=NULL,"
            " transcript=NULL, candidates_json=NULL WHERE report_id=?",
            (actor, now, now, report_id),
        )
        conn.execute(
            "INSERT INTO purge_log(course_id, what, reason, purged_at) VALUES (?, 'clip', 'decided', ?)",
            (row["course_id"], now),
        )
        if alias:
            conn.execute(
                "INSERT OR IGNORE INTO aliases(text_norm, ward_no, added_by, added_at) VALUES (?,?,?,?)",
                (alias, data.ward_no, actor, now),
            )
        audit.record(
            conn,
            actor,
            "review.decide",
            "bite_report",
            report_id,
            {"decision": status, "ward_no": ward, "remembered": bool(alias)},
        )
        if ward != row["ward_no"]:
            alerts.update_location(ctx, conn, report_id)
    _announce(ctx, report_id)
    return remaining(ctx)
