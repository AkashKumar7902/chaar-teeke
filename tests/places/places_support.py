"""Helpers for the places tests: a small gazetteer, a fake transcriber, a world with reports and clips."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from chaarteeke import clock
from chaarteeke.config import DATA_DIR
from chaarteeke.context import AppContext
from chaarteeke.ids import new_id
from chaarteeke.places.gazetteer import Gazetteer
from chaarteeke.speech.asr import Transcript

# Two squares that overlap on x 1..2, so a point at x 1.5 lies in both wards.
SLIVER_GEOJSON: dict[str, Any] = {
    "features": [
        {
            "properties": {"ward_no": 1, "ward_name": "Left Ward"},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 1], [0, 1], [0, 0]]]},
        },
        {
            "properties": {"ward_no": 2, "ward_name": "Right Ward"},
            "geometry": {"type": "Polygon", "coordinates": [[[1, 0], [3, 0], [3, 1], [1, 1], [1, 0]]]},
        },
        {
            "properties": {"ward_no": 3, "ward_name": "Far Ward"},
            "geometry": {"type": "Polygon", "coordinates": [[[5, 0], [6, 0], [6, 1], [5, 1], [5, 0]]]},
        },
    ]
}


def entry(name: str, wards: list[int], kind: str = "landmark", **more: Any) -> dict[str, Any]:
    return {
        "id": f"t_{name}",
        "name": name,
        "type": kind,
        "ward_nos": wards,
        "name_hi": None,
        "name_ur": None,
        "aliases_latin": [],
        "aliases_devanagari": [],
        "lat": None,
        "lon": None,
        **more,
    }


def real_gazetteer(db=None) -> Gazetteer:
    return Gazetteer.load(DATA_DIR, db)


def wav_bytes(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00" * int(seconds * rate))
    return buf.getvalue()


@dataclass
class FakeTranscriber:
    """Says whatever it was told to; records how it was called. Used only inside tests."""

    text: str = ""
    error: Exception | None = None
    model_id: str = "test double"
    calls: list[tuple[int, str]] = field(default_factory=list)
    during: Any = None  # a callable run in the middle of transcribing (to simulate a race)
    frames: Any = None  # asr.Frames handed back with the text (None: the double gives none)

    def transcribe(self, wav: bytes, lang: str) -> Transcript:
        self.calls.append((len(wav), lang))
        if self.during:
            self.during()
        if self.error:
            raise self.error
        return Transcript(text=self.text, model=self.model_id, seconds=1.0, frames=self.frames)


class World:
    """A clinic, a course and bite reports with recorded place clips, on a test context."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        with ctx.db.transaction() as conn:
            conn.execute(
                "INSERT INTO clinics(id, name, short_name, ward_no, default_regimen, default_lang, arv_status, is_demo)"
                " VALUES ('cln_t','Test clinic','Test',NULL,'id_trc','hi','confirmed',1)"
            )
            conn.execute(
                "INSERT INTO staff(id, username, display_name, role, clinic_id, password_hash, active, is_demo)"
                " VALUES ('stf_rev','rev','Reviewer','reviewer',NULL,'x',1,1),"
                " ('stf_vet','vet','Vet','vet',NULL,'x',1,1)"
            )
        self.clips = ctx.settings.var_dir / "clips"
        self.clips.mkdir(parents=True, exist_ok=True)

    def report(
        self, *, lang: str = "hi", signs: str = "no", clip: bytes | None = None, source: str = "simulator"
    ) -> str:
        """A report waiting for its place to be processed (asr pending)."""
        ctx = self.ctx
        cid, rid = new_id("crs"), new_id("rep")
        now = clock.iso(clock.now())
        expires = clock.iso(clock.now() + timedelta(days=7))
        path = None
        if clip is not None:
            path = self.clips / f"{rid}.wav"
            path.write_bytes(clip)
        with ctx.db.transaction() as conn:
            conn.execute(
                "INSERT INTO courses(id, clinic_id, regimen_id, day0, lang, status, eligibility, source, created_at, is_demo)"
                " VALUES (?, 'cln_t', 'id_trc', '2026-10-19', ?, 'active', 'adult', ?, ?, 1)",
                (cid, lang, source, now),
            )
            conn.execute(
                "INSERT INTO bite_reports(id, course_id, clinic_id, bite_week, signs, location_status, source, created_at, is_demo)"
                " VALUES (?, ?, 'cln_t', '2026-10-19', ?, 'pending', ?, ?, 1)",
                (rid, cid, signs, source, now),
            )
            conn.execute(
                "INSERT INTO place_captures(report_id, recording_name, audio_path, asr_status, expires_at)"
                " VALUES (?, 'rec', ?, 'pending', ?)",
                (rid, str(path) if path else None, expires),
            )
        return rid

    def row(self, rid: str) -> dict[str, Any]:
        r = self.ctx.db.one(
            "SELECT r.*, pc.transcript, pc.asr_status, pc.asr_error, pc.asr_model, pc.candidates_json, pc.confidence,"
            " pc.decided_by, pc.decided_at, pc.clip_deleted_at, pc.audio_path"
            " FROM bite_reports r JOIN place_captures pc ON pc.report_id = r.id WHERE r.id = ?",
            (rid,),
        )
        return dict(r)

    def clip_path(self, rid: str) -> Path:
        return self.clips / f"{rid}.wav"
