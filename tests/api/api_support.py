"""Helpers for the API tests (a unique module name, because tests/ has several conftest.py files)."""

from __future__ import annotations

import io
import json
import math
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from chaarteeke import clock
from chaarteeke.api.auth import COOKIE, CSRF_HEADER
from chaarteeke.config import DATA_DIR
from chaarteeke.ivr.prompts import PATIENT_LANGS, PromptCatalogue


@dataclass(frozen=True)
class Who:
    token: str | None
    csrf: str | None

    @property
    def read(self) -> dict:
        return {"cookie": f"{COOKIE}={self.token}"} if self.token else {}

    @property
    def write(self) -> dict:
        return {**self.read, CSRF_HEADER: self.csrf} if self.token else {}


def read_logins(path: Path) -> dict[str, str]:
    """username -> password from the file `seed-demo` writes (comment lines start with #)."""
    rows = [ln.split() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    return {name: password for name, password in rows}


def wav_bytes(
    seconds: float = 0.1, rate: int = 8000, channels: int = 1, width: int = 2, loud: bool = True
) -> bytes:
    n = int(seconds * rate)
    amp = 8000 if loud else 0
    frames = b"".join(
        int(amp * math.sin(2 * math.pi * 300 * i / rate)).to_bytes(2, "little", signed=True) * channels
        for i in range(n)
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames[: n * channels * width] if width == 2 else b"\0" * n * channels * width)
    return buf.getvalue()


def ward_numbers(ctx) -> list[int]:
    return [w.ward_no for w in ctx.gazetteer.wards()]


def write_voice(audio_dir: Path, prompts_json: Path) -> None:
    """A tiny WAV for every file the voice must provide: all prompts, and slot clips for the clinics the
    demo uses, every ward and the demo visit time."""
    wards = json.loads((DATA_DIR / "wards.geojson").read_text())["features"]
    cat = PromptCatalogue(
        prompts_json,
        audio_dir,
        clinic_ids=["cln_gmc", "cln_demo2", "cln_demo_visitor"],
        ward_nos=[f["properties"]["ward_no"] for f in wards],
        times=["09:00"],
    )
    tiny = wav_bytes(0.01)
    for lang in PATIENT_LANGS:
        for item in cat.manifest(lang):
            path = audio_dir / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(tiny)


class AnchoredClock(clock.OffsetClock):
    """A clock that runs in real time from a fixed start, not from today's date.

    The app's sessions run on real time (auth.session_now: an OffsetClock minus its jumps). A plain
    OffsetClock(T0 - now) gives "real time" = today, so a session signed in at T0 would look expired once
    today is more than a session length after T0. Anchoring real time at T0 keeps the tests independent of
    the date they run on.
    """

    def __init__(self, start: datetime) -> None:
        super().__init__(timedelta(0))
        self._start = start
        self._began = time.monotonic()

    def now(self) -> datetime:
        return self._start + timedelta(seconds=time.monotonic() - self._began) + self.offset
