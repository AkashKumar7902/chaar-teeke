"""The prompt catalogue: the words of every call, the audio files they need, and how a line is composed.

`data/prompts/prompts.json` holds each prompt's text in en/hi/doi/ur with slots such as {date} or {clinic}.
A text is split at its slots into literal segments, so a line is spoken as recorded segments and slot
clips in the language's own word order:

    audio/<lang>/<id>.wav              a prompt with no slots
    audio/<lang>/<id>.<k>.wav          the k-th spoken literal segment (0-based, counting only segments
                                       that contain a word; a segment of only spaces or punctuation is
                                       not recorded)
    audio/<lang>/slot/<piece>.wav      slot clips: weekday_0-6 (Monday=0), day_1-31, month_1-12, num_0-9
                                       (dose numbers and phone digits), time_<HHMM>, clinic_<clinic_id>,
                                       ward_<n>

Nothing is invented: a file that is not there is reported by `missing()` and refused by `render()`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..context import AppContext
from ..telephony.sim import MEDIA_PREFIX, media_url
from .flow import REQUIRED_PROMPTS

PATIENT_LANGS = ("hi", "doi", "ur")
LANG_NAMES = {"hi": "Hindi", "doi": "Dogri", "ur": "Urdu", "en": "English"}
SLOT_DIR = "slot"
KNOWN_SLOTS = ("date", "n", "total", "time", "clinic", "ward", "clinic_phone", "register")
_SLOT = re.compile(r"\{(\w+)\}")
_WORD = re.compile(r"\w")
_FILE_SAFE = re.compile(r"[A-Za-z0-9_-]+")


class PromptError(Exception):
    """A prompt problem in plain words. `code` and `message` are what the API shows."""

    code = "prompt_error"
    status = 409

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownPrompt(PromptError):
    code = "unknown_prompt"


class MissingSlot(PromptError):
    code = "missing_slot"


class MissingAudio(PromptError):
    """Recorded voice that is not there. `files` are paths relative to the audio folder."""

    code = "missing_audio"

    def __init__(self, files: list[str]) -> None:
        super().__init__(
            f"Recorded voice is missing: {', '.join(files[:3])}" + (" ..." if len(files) > 3 else "")
        )
        self.files = files


class ClinicSetupError(PromptError):
    code = "clinic_setup"


@dataclass(frozen=True)
class _Literal:
    text: str
    k: int | None  # None: the prompt has no slots, so its one file is <id>.wav


@dataclass(frozen=True)
class _Slot:
    name: str


_Part = _Literal | _Slot


@dataclass(frozen=True)
class ManifestItem:
    """One audio file the voice has to provide."""

    lang: str
    path: str  # relative to the audio folder, e.g. hi/FC_CLOSE.0.wav or hi/slot/weekday_0.wav
    kind: str  # "segment" or "slot"
    prompt_id: str | None  # for a segment
    text: str | None  # the words to record, for a segment
    slot: str | None  # for a slot clip: weekday, day, month, num, time, clinic or ward
    key: str | None  # for a slot clip: which one (0-6, 1-31, a clinic id ...)


def hhmm(value: str) -> str:
    """'09:00' or '0900' -> '0900'."""
    digits = value.replace(":", "")
    if len(digits) != 4 or not digits.isdigit():
        raise MissingSlot(f"'{value}' is not a time of day (use HH:MM)")
    return digits


def visit_time(clinic: sqlite3.Row) -> str:
    """The time of day this clinic sees patients, spoken in reminders: clinics.visit_time as 'HHMM'."""
    value = clinic["visit_time"]
    if not value:
        raise ClinicSetupError("This clinic has no visit time set up, so the call cannot say when to come.")
    return hhmm(value)


def _speaks(text: str) -> bool:
    return bool(_WORD.search(text))


def _parse(prompt_id: str, lang: str, text: str) -> tuple[_Part, ...]:
    slots = list(_SLOT.finditer(text))
    if not slots:
        return (_Literal(text, None),)
    parts: list[_Part] = []
    k = 0
    pos = 0

    def literal(chunk: str) -> None:
        nonlocal k
        if _speaks(chunk):
            parts.append(_Literal(chunk, k))
            k += 1

    for m in slots:
        if m.group(1) not in KNOWN_SLOTS:
            raise PromptError(f"{prompt_id} ({lang}) uses an unknown slot {{{m.group(1)}}}")
        literal(text[pos : m.start()])
        parts.append(_Slot(m.group(1)))
        pos = m.end()
    literal(text[pos:])
    return tuple(parts)


class PromptCatalogue:
    def __init__(
        self,
        prompts_json: Path | str,
        audio_dir: Path | str,
        *,
        clinic_ids: Iterable[str] = (),
        ward_nos: Iterable[int] = (),
        times: Iterable[str] = (),
    ) -> None:
        raw = json.loads(Path(prompts_json).read_text(encoding="utf-8"))
        self.audio_dir = Path(audio_dir)
        self.version: str = raw["meta"]["version"]
        self._clinic_ids = tuple(clinic_ids)
        self._ward_nos = tuple(ward_nos)
        self._times = tuple(hhmm(t) for t in times)
        self._text: dict[str, dict[str, str]] = {}
        self._parts: dict[tuple[str, str], tuple[_Part, ...]] = {}
        for p in raw["prompts"]:
            pid = p["id"]
            self._text[pid] = {}
            for lang in (*PATIENT_LANGS, "en"):
                text = p["text"].get(lang)
                if text is None:
                    if lang == "en":
                        continue
                    raise PromptError(f"{pid} has no {LANG_NAMES[lang]} text")
                self._text[pid][lang] = text
                self._parts[(pid, lang)] = _parse(pid, lang, text)
        self._used_slots = {
            part.name for parts in self._parts.values() for part in parts if isinstance(part, _Slot)
        }

    # ------------------------------------------------------------- the texts
    def ids(self) -> list[str]:
        return list(self._text)

    def text(self, prompt_id: str, lang: str) -> str:
        try:
            return self._text[prompt_id][lang]
        except KeyError as e:
            raise UnknownPrompt(f"There is no {lang} text for prompt {prompt_id}.") from e

    def slots_of(self, prompt_id: str, lang: str) -> list[str]:
        return [p.name for p in self._parts_of(prompt_id, lang) if isinstance(p, _Slot)]

    def caption_frame(self, prompt_id: str, lang: str) -> tuple[str, list[list[str]]]:
        """How a line sits in the files it is spoken as, for the captions of the browser phone.

        Returns the name of its first spoken file (`<id>.0`, or `<id>` when it has no slots) and its slots
        grouped into runs: slots with no spoken word between them are one run, and the slot clips of a run
        play one after the other. A line that starts or ends with a slot has no first or last word file to
        hang its caption on, so it is refused here rather than captioned wrongly."""
        parts = self._parts_of(prompt_id, lang)
        if not any(isinstance(p, _Slot) for p in parts):
            return prompt_id, []
        if not isinstance(parts[0], _Literal) or not isinstance(parts[-1], _Literal):
            raise PromptError(
                f"{prompt_id} ({lang}) starts or ends with a slot, so its caption has no word to follow."
            )
        runs: list[list[str]] = []
        inside = False
        for p in parts:
            if isinstance(p, _Slot):
                if inside:
                    runs[-1].append(p.name)
                else:
                    runs.append([p.name])
                    inside = True
            else:
                inside = False
        return self._segment_name(prompt_id, 0), runs

    def undefined_required(self) -> list[str]:
        """Prompts the call flows play that prompts.json does not define. No text is invented for them."""
        return sorted(REQUIRED_PROMPTS - set(self._text))

    def _parts_of(self, prompt_id: str, lang: str) -> tuple[_Part, ...]:
        try:
            return self._parts[(prompt_id, lang)]
        except KeyError as e:
            raise UnknownPrompt(f"There is no {lang} text for prompt {prompt_id}.") from e

    # ------------------------------------------------------------ file names
    @staticmethod
    def _segment_name(prompt_id: str, k: int | None) -> str:
        return prompt_id if k is None else f"{prompt_id}.{k}"

    def _segment_files(self, lang: str, prompt_id: str) -> list[str]:
        return [
            f"{lang}/{self._segment_name(prompt_id, p.k)}.wav"
            for p in self._parts_of(prompt_id, lang)
            if isinstance(p, _Literal) and _speaks(p.text)
        ]

    def _slot_pieces(
        self, clinic_ids: Iterable[str], ward_nos: Iterable[int], times: Iterable[str]
    ) -> list[tuple[str, str, str]]:
        """(piece name, slot, key) for every slot clip the prompts need."""
        used = self._used_slots
        out: list[tuple[str, str, str]] = []
        if "date" in used:
            out += [(f"weekday_{i}", "weekday", str(i)) for i in range(7)]
            out += [(f"day_{i}", "day", str(i)) for i in range(1, 32)]
            out += [(f"month_{i}", "month", str(i)) for i in range(1, 13)]
        if used & {"n", "total", "clinic_phone", "register"}:
            out += [(f"num_{i}", "num", str(i)) for i in range(10)]
        if "time" in used:
            out += [(f"time_{t}", "time", t) for t in dict.fromkeys(hhmm(t) for t in times)]
        if "clinic" in used:
            out += [(f"clinic_{c}", "clinic", c) for c in dict.fromkeys(clinic_ids)]
        if "ward" in used:
            out += [(f"ward_{w}", "ward", str(w)) for w in dict.fromkeys(ward_nos)]
        return out

    def _has(self, rel: str) -> bool:
        return (self.audio_dir / rel).is_file()

    def has_audio(self, lang: str, prompt_id: str) -> bool:
        """True when every spoken segment of this prompt has its file (slot clips are not counted)."""
        return all(self._has(f) for f in self._segment_files(lang, prompt_id))

    def media_name(self, lang: str, prompt_id: str) -> str:
        """The PBX sound name of a prompt that has no slots: chaar/<lang>/<id>."""
        if self.slots_of(prompt_id, lang):
            raise ValueError(f"{prompt_id} has slots; use render()")
        return f"{MEDIA_PREFIX}/{lang}/{prompt_id}"

    def url(self, lang: str, prompt_id: str) -> str:
        return media_url(self.media_name(lang, prompt_id))

    # ------------------------------------------------------------- what is missing
    def _extent(self, clinic_ids: Iterable[str], ward_nos: Iterable[int], times: Iterable[str]):
        return (
            (*self._clinic_ids, *clinic_ids),
            (*self._ward_nos, *ward_nos),
            (*self._times, *times),
        )

    def missing(
        self,
        lang: str,
        *,
        clinic_ids: Iterable[str] = (),
        ward_nos: Iterable[int] = (),
        times: Iterable[str] = (),
    ) -> list[str]:
        """Every file this language still lacks, relative to the audio folder, plus prompts the flows
        need that prompts.json does not define. The clinics, wards and times given are added to those
        the catalogue was built with."""
        out = [f for pid in self._text for f in self._segment_files(lang, pid) if not self._has(f)]
        for name, _slot, _key in self._slot_pieces(*self._extent(clinic_ids, ward_nos, times)):
            rel = f"{lang}/{SLOT_DIR}/{name}.wav"
            if not self._has(rel):
                out.append(rel)
        out += [f"{pid} (not defined in prompts.json)" for pid in self.undefined_required()]
        return sorted(out)

    def manifest(
        self,
        lang: str | None = None,
        *,
        clinic_ids: Iterable[str] = (),
        ward_nos: Iterable[int] = (),
        times: Iterable[str] = (),
    ) -> list[ManifestItem]:
        """Every file the voice has to record, for one language or for hi, doi and ur."""
        clinics, wards, hours = self._extent(clinic_ids, ward_nos, times)
        out: list[ManifestItem] = []
        for lg in (lang,) if lang else PATIENT_LANGS:
            for pid in self._text:
                for p in self._parts_of(pid, lg):
                    if isinstance(p, _Literal) and _speaks(p.text):
                        name = self._segment_name(pid, p.k)
                        out.append(ManifestItem(lg, f"{lg}/{name}.wav", "segment", pid, p.text, None, None))
            for name, slot, key in self._slot_pieces(clinics, wards, hours):
                out.append(ManifestItem(lg, f"{lg}/{SLOT_DIR}/{name}.wav", "slot", None, None, slot, key))
        return out

    # ------------------------------------------------------------ saying things
    def say_number(self, lang: str, n: int | str) -> list[str]:
        """Media names that read a whole number digit by digit (a dose number, a phone number)."""
        digits = str(n)
        if not digits.isdigit():
            raise MissingSlot(f"'{n}' is not a number that can be read out")
        return [f"{MEDIA_PREFIX}/{lang}/{SLOT_DIR}/num_{d}" for d in digits]

    def say_date(self, lang: str, day: date) -> list[str]:
        """Weekday, day of month, month: the same order in hi, doi, ur and en."""
        base = f"{MEDIA_PREFIX}/{lang}/{SLOT_DIR}"
        return [f"{base}/weekday_{day.weekday()}", f"{base}/day_{day.day}", f"{base}/month_{day.month}"]

    def _slot_media(self, lang: str, prompt_id: str, name: str, value: Any) -> list[str]:
        base = f"{MEDIA_PREFIX}/{lang}/{SLOT_DIR}"
        if name == "date":
            return self.say_date(lang, value if isinstance(value, date) else date.fromisoformat(value))
        if name in ("n", "total", "clinic_phone", "register"):
            digits = name in (
                "clinic_phone",
                "register",
            )  # read digit by digit; letters and slashes are not read
            return self.say_number(lang, re.sub(r"\D", "", str(value)) if digits else value)
        if name == "time":
            return [f"{base}/time_{hhmm(str(value))}"]
        if not _FILE_SAFE.fullmatch(str(value)):
            raise MissingSlot(f"'{value}' cannot be used as a {name} name for a recorded clip")
        return [f"{base}/{name}_{value}"]

    def render(self, lang: str, prompt_id: str, slots: Mapping[str, Any] | None = None) -> list[str]:
        """The media names of one spoken line, in the language's own order. Raises MissingSlot when the
        line needs a value that was not given, and MissingAudio (naming every file) when a file is absent."""
        slots = slots or {}
        media: list[str] = []
        for p in self._parts_of(prompt_id, lang):
            if isinstance(p, _Literal):
                if _speaks(p.text):
                    media.append(f"{MEDIA_PREFIX}/{lang}/{self._segment_name(prompt_id, p.k)}")
            else:
                if p.name not in slots or slots[p.name] in (None, ""):
                    raise MissingSlot(f"{prompt_id} needs {p.name}, which was not given.")
                media += self._slot_media(lang, prompt_id, p.name, slots[p.name])
        absent = [
            m.removeprefix(f"{MEDIA_PREFIX}/") + ".wav"
            for m in media
            if not self._has(m.removeprefix(f"{MEDIA_PREFIX}/") + ".wav")
        ]
        if absent:
            raise MissingAudio(absent)
        return media


def required_extent(ctx: AppContext) -> dict[str, list]:
    """The clinics, wards and times a whole-system check must cover: pass it to `missing()`.
    A clinic without a visit time adds none: its calls are refused until one is set."""
    clinics = ctx.db.query("SELECT id, visit_time FROM clinics ORDER BY id")
    wards = json.loads((ctx.settings.data_dir / "wards.geojson").read_text(encoding="utf-8"))
    return {
        "clinic_ids": [r["id"] for r in clinics],
        "ward_nos": sorted(f["properties"]["ward_no"] for f in wards["features"]),
        "times": sorted({hhmm(r["visit_time"]) for r in clinics if r["visit_time"]}),
    }
