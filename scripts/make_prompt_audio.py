#!/usr/bin/env python3
"""Make the machine-voice draft of every prompt audio file (and the demo place clips) with local MMS-TTS.

Two stages, because the speech models live in a different Python from the app:

    plan      run with the app's env:   uv run python scripts/make_prompt_audio.py plan
              Reads PromptCatalogue(...).manifest() and writes data/prompts/audio/PLAN.json: every file,
              the words to speak, and the files that cannot be made because their words do not exist yet.
    generate  run with the voice env:   <voice-env>/bin/python \
                                          scripts/make_prompt_audio.py generate
              Speaks every file of PLAN.json that is not on disk yet (resumable), converts it to 8 kHz mono
              16-bit PCM WAV, normalises loudness with ffmpeg loudnorm and writes audio/MANIFEST.json.
    check     run with the app's env:   uv run python scripts/make_prompt_audio.py check
              Prints PromptCatalogue.missing(lang) for hi, doi and ur and a duration spot-check. A prompt over 25 s is
              listed as long; only FC_NOTICE_FULL (heard only on key 0) may be.

    clips-plan / generate --target clips   the same for the demo place clips in data/demo/clips.

Nothing is invented. A file whose words are not in prompts.json (meta), data/gazetteer.json or
data/prompts/slot_sources.json is reported as unavailable and is not made. A failed file is reported and
the run ends non-zero; no other voice stands in for it.

MMS-TTS is licensed CC-BY-NC 4.0: prototype use only. Every file is a machine voice draft.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROMPTS_JSON = DATA / "prompts" / "prompts.json"
AUDIO_DIR = DATA / "prompts" / "audio"
SOURCES_JSON = DATA / "prompts" / "slot_sources.json"
CLIPS_DIR = DATA / "demo" / "clips"
VOICE_MODELS = Path(os.environ.get("CT_VOICE_MODELS", Path.home() / "chaar-teeke-build" / "voice" / "models"))

LANGS = ("hi", "doi", "ur")
MODELS = {"hi": "mms-tts-hin", "doi": "mms-tts-dgo", "ur": "mms-tts-urd-script_arabic"}
DEFAULT_CLINICS = ("cln_gmc", "cln_demo2", "cln_demo_visitor")  # chaarteeke/demo/seed.py
REVIEW_STATUS = "machine voice draft"
SENTENCE_END = re.compile(r"(?<=[।.?!۔؟])\s+")
SOFT_BREAK = re.compile(r"(?<=[,،;:])\s+")
PUNCT = re.compile(r"[,،;:।.?!۔؟\"“”‘’()\[\]{}—–/]")
MAX_CHARS = 110  # about 10 s of speech; MMS stays steadier on short input (research/VOICE.md 5.2)
LONG_PROMPT_S = (
    25.0  # one prompt a person sits through before a key is asked for (the short notice is the longest)
)
LONG_BY_DESIGN = frozenset({"FC_NOTICE_FULL"})  # opt-in: key 0 at the consent question
PAUSE_SENTENCE_S = 0.35
PAUSE_SOFT_S = 0.15
NUMBER = re.compile(r"\d+")


class PlanError(Exception):
    """Something the plan cannot do without inventing words."""


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


# ======================================================================== plan
def speakable(lang: str, text: str, numbers: dict[str, str], where: str) -> str:
    """Digits become the words prompts.json meta.spoken_numbers gives; a number with no words is an error."""

    def word(m: re.Match) -> str:
        if m.group() not in numbers:
            raise PlanError(
                f"{where} ({lang}) has the number {m.group()} but prompts.json meta.spoken_numbers.{lang} has no words for it"
            )
        return numbers[m.group()]

    return NUMBER.sub(word, text)


def _gazetteer(data_dir: Path) -> dict[str, dict]:
    return {e["id"]: e for e in read_json(data_dir / "gazetteer.json")["entries"]}


def _ward_entries(data_dir: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for e in read_json(data_dir / "gazetteer.json")["entries"]:
        if e.get("type") == "ward" and len(e.get("ward_nos", [])) == 1:
            out[e["ward_nos"][0]] = e
    return out


def _slot_words(lang: str, item, meta: dict, sources: dict, gaz: dict, wards: dict[int, dict]):
    """(words, source, reason). words is None when they do not exist yet; reason says what is needed."""
    name = Path(item.path).stem  # weekday_0, day_5, num_3, time_0900, clinic_cln_gmc, ward_7
    override = sources.get("texts", {}).get(lang, {}).get(name)
    if override:
        return override, "data/prompts/slot_sources.json texts", None
    if item.slot == "weekday":
        words = meta["spoken_dates"]["weekdays"][lang][int(item.key)]
        return words, "prompts.json meta.spoken_dates.weekdays", None
    if item.slot == "day":
        return meta["spoken_dates"]["days"][lang][item.key], "prompts.json meta.spoken_dates.days", None
    if item.slot == "month":
        return meta["spoken_dates"]["months"][lang][item.key], "prompts.json meta.spoken_dates.months", None
    if item.slot == "num":
        return meta["spoken_numbers"][lang][item.key], "prompts.json meta.spoken_numbers", None
    if item.slot == "time":
        words = meta["spoken_times"].get(lang, {}).get(item.key)
        if words:
            return words, "prompts.json meta.spoken_times", None
        return None, None, f"words for the time {item.key} in prompts.json meta.spoken_times.{lang}"
    if item.slot == "clinic":
        entry = (sources.get("clinics") or {}).get(item.key)
        if not entry:
            return None, None, f"words for clinic {item.key} in {lang} (data/prompts/slot_sources.json)"
        return _gazetteer_name(gaz[entry["gazetteer_id"]], lang, f"clinic {item.key}")
    if item.slot == "ward":
        e = wards.get(int(item.key))
        if not e:
            return None, None, f"ward {item.key} has no gazetteer ward entry"
        return _gazetteer_name(e, lang, f"ward {item.key}")
    raise PlanError(f"unknown slot kind {item.slot}")


def _gazetteer_name(entry: dict, lang: str, what: str):
    if lang in ("hi", "doi"):  # Dogri is written in Devanagari: the same name_hi
        words = entry.get("name_hi")
        if words:
            return words, f"data/gazetteer.json {entry['id']} name_hi ({entry.get('name_hi_source')})", None
        return None, None, f"Devanagari name for {what}"
    words = entry.get("name_ur")
    if words:
        return words, f"data/gazetteer.json {entry['id']} name_ur", None
    return None, None, f"Urdu-script name for {what} (gazetteer has no name_ur)"


def cmd_plan(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(ROOT))
    from chaarteeke.ivr.prompts import PromptCatalogue

    raw = read_json(PROMPTS_JSON)
    meta = raw["meta"]
    sources = read_json(SOURCES_JSON)
    data_dir = Path(args.data_dir)
    gaz = _gazetteer(data_dir)
    wards = _ward_entries(data_dir)
    geo = read_json(data_dir / "wards.geojson")
    ward_nos = sorted(f["properties"]["ward_no"] for f in geo["features"])
    clinics = list(args.clinics or DEFAULT_CLINICS)
    times = list(args.times or meta["spoken_times"]["hi"])
    cat = PromptCatalogue(PROMPTS_JSON, AUDIO_DIR, clinic_ids=clinics, ward_nos=ward_nos, times=times)
    items, unavailable = [], []
    for it in cat.manifest():
        numbers = meta["spoken_numbers"][it.lang]
        if it.kind == "segment":
            items.append(
                dict(
                    lang=it.lang,
                    path=it.path,
                    kind="segment",
                    source=f"prompts.json {it.prompt_id}",
                    text=it.text,
                    speak=speakable(it.lang, it.text, numbers, it.prompt_id),
                )
            )
            continue
        words, source, reason = _slot_words(it.lang, it, meta, sources, gaz, wards)
        if words is None:
            unavailable.append(dict(lang=it.lang, path=it.path, kind="slot", needs=reason))
        else:
            items.append(
                dict(lang=it.lang, path=it.path, kind="slot", source=source, text=words, speak=words)
            )
    plan = dict(
        prompts_sha256=sha256(PROMPTS_JSON),
        prompts_version=cat.version,
        made_at=now_utc(),
        clinics=clinics,
        times=times,
        ward_count=len(ward_nos),
        items=items,
        unavailable=unavailable,
    )
    write_json(AUDIO_DIR / "PLAN.json", plan)
    by_lang = {lg: sum(1 for i in items if i["lang"] == lg) for lg in LANGS}
    print(f"plan: {len(items)} files to speak {by_lang}; {len(unavailable)} unavailable")
    kinds: dict[str, int] = {}
    for u in unavailable:
        key = f"{u['lang']} {Path(u['path']).stem.split('_')[0]}"
        kinds[key] = kinds.get(key, 0) + 1
    for key, n in sorted(kinds.items()):
        print(f"  unavailable: {key} x{n}")
    return 0


# ================================================================== clips plan
def cmd_clips_plan(args: argparse.Namespace) -> int:
    """Run the demo seed on the pinned test clock into a scratch database and read the transcripts of the
    twelve review-queue captures it makes. Each becomes the Hindi words a person would say."""
    from datetime import UTC, datetime

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests" / "stats"))
    from stats_helpers import T0, build_ctx  # the same fixed clock tests/conftest.py uses

    from chaarteeke import clock
    from chaarteeke.demo import seed as seeder

    assert T0 == datetime(2026, 10, 19, 4, 30, tzinfo=UTC)
    hindi_wards = _ward_entries(DATA)
    latin = {
        "{n} ke paas": "{hi} के पास",
        "{n} market ke samne": "{hi} मार्केट के सामने",
        "mera ghar {n} mein hai": "मेरा घर {hi} में है",
        "{n} chowk par": "{hi} चौक पर",
    }
    vague = {
        "wahan school ke saamne": "वहाँ स्कूल के सामने",
        "bus adde ke paas": "बस अड्डे के पास",
        "mandir ke peeche gali mein": "मंदिर के पीछे गली में",
        "hospital ke bahar": "अस्पताल के बाहर",
    }
    with clock.use_clock(clock.FixedClock(T0)):
        ctx = build_ctx(Path(tempfile.mkdtemp()))
        seeder.seed(ctx)
        rows = ctx.db.query("SELECT recording_name, transcript, candidates_json FROM place_captures")
        ctx.db.close()
    items = []
    for r in rows:
        k = int(r["recording_name"].rsplit("-", 1)[1])
        text = r["transcript"]
        cands = json.loads(r["candidates_json"])
        true_ward = cands[0]["ward_no"] if cands else None
        if text in vague:
            speak = vague[text]
        elif re.search(r"[ऀ-ॿ]", text):
            speak = text
        else:
            speak = None
            hi = hindi_wards[true_ward]["name_hi"] if true_ward in hindi_wards else None
            for pattern, template in latin.items():
                head, _, tail = pattern.partition("{n}")
                if text.startswith(head) and text.endswith(tail):
                    if hi is None:
                        raise PlanError(f"clip {k}: ward {true_ward} has no Devanagari name")
                    speak = template.format(hi=hi)
            if speak is None:
                raise PlanError(f"clip {k}: no Hindi form for the seed transcript {text!r}")
        items.append(
            dict(
                lang="hi",
                path=f"{k}.wav",
                kind="clip",
                source=f"demo seed transcript for place-{k} on the test clock (2026-10-19 10:00 IST)",
                text=text,
                speak=speak,
                ward_no=true_ward,
            )
        )
    items.sort(key=lambda i: int(i["path"].split(".")[0]))
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    write_json(
        CLIPS_DIR / "PLAN.json",
        dict(prompts_sha256=sha256(PROMPTS_JSON), made_at=now_utc(), items=items, unavailable=[]),
    )
    print(f"clips-plan: {len(items)} clips")
    for i in items:
        print(f"  {i['path']:7} {i['text']!r:45} -> {i['speak']}")
    return 0


# ================================================================== generate
class Speaker:
    """One MMS-TTS model, loaded once for a language."""

    def __init__(self, lang: str, models_dir: Path, threads: int) -> None:
        import torch
        from transformers import AutoTokenizer, VitsModel

        torch.set_num_threads(threads)
        path = models_dir / MODELS[lang]
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(str(path))
        self.vocab = set(self.tok.get_vocab())
        self.model = VitsModel.from_pretrained(str(path)).eval()
        self.rate = self.model.config.sampling_rate

    def pieces(self, text: str) -> tuple[list[tuple[str, float]], list[str]]:
        """Sentence and soft-break pieces with the pause after each, and the characters the model cannot say."""
        text = unicodedata.normalize("NFC", text)
        out: list[tuple[str, float]] = []
        for sentence in SENTENCE_END.split(text):
            parts = [sentence]
            if len(sentence) > MAX_CHARS:
                parts = []
                for chunk in SOFT_BREAK.split(sentence):
                    if parts and len(parts[-1]) + len(chunk) < MAX_CHARS:
                        parts[-1] += " " + chunk
                    else:
                        parts.append(chunk)
            for n, part in enumerate(parts):
                out.append((part, PAUSE_SENTENCE_S if n == len(parts) - 1 else PAUSE_SOFT_S))
        dropped: set[str] = set()
        cleaned: list[tuple[str, float]] = []
        for part, pause in out:
            part = re.sub(r"\s+", " ", PUNCT.sub(" ", part)).strip()
            dropped |= {c for c in part if c not in self.vocab and not c.isspace()}
            part = "".join(c for c in part if c in self.vocab)
            if part.strip():
                cleaned.append((part.strip(), pause))
        return cleaned, sorted(dropped)

    def speak(self, text: str, seed_key: str):
        import numpy as np

        pieces, dropped = self.pieces(text)
        if not pieces:
            raise RuntimeError(f"nothing speakable in {text!r}")
        waves = []
        for n, (piece, pause) in enumerate(pieces):
            self.torch.manual_seed(zlib.crc32(f"{seed_key}:{n}".encode()))
            inputs = self.tok(piece, return_tensors="pt")
            with self.torch.no_grad():
                wave = self.model(**inputs).waveform[0].numpy()
            waves.append(wave)
            if n < len(pieces) - 1:
                waves.append(np.zeros(int(self.rate * pause), dtype=wave.dtype))
        return np.concatenate(waves), dropped


FILTER = (
    "silenceremove=start_periods=1:start_silence=0.03:start_threshold=-45dB:detection=peak,"
    "areverse,"
    "silenceremove=start_periods=1:start_silence=0.03:start_threshold=-45dB:detection=peak,"
    "areverse,"
    "loudnorm=I=-20:TP=-2:LRA=7,"
    "aresample=8000,"
    "apad=pad_dur=0.1"
)


def to_phone_wav(wave, rate: int, out: Path, scratch: Path) -> None:
    """16-bit source -> trim silence -> loudnorm -> 8 kHz mono 16-bit PCM WAV, written atomically."""
    import numpy as np
    from scipy.io import wavfile

    src = scratch / (out.stem + ".src.wav")
    wavfile.write(str(src), rate, (np.clip(wave, -1, 1) * 32767).astype(np.int16))
    tmp = out.with_name(out.stem + ".part.wav")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-af", FILTER]
            + ["-ac", "1", "-ar", "8000", "-c:a", "pcm_s16le", str(tmp)],
            check=True,
        )
        tmp.replace(out)
    finally:
        src.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


def wav_seconds(path: Path) -> float:
    import wave

    with wave.open(str(path), "rb") as w:
        return round(w.getnframes() / w.getframerate(), 2)


def cmd_generate(args: argparse.Namespace) -> int:
    base = CLIPS_DIR if args.target == "clips" else AUDIO_DIR
    plan_path = base / "PLAN.json"
    if not plan_path.is_file():
        print(f"No {plan_path}. Run the plan stage first (see the top of this file).", file=sys.stderr)
        return 2
    plan = read_json(plan_path)
    if plan["prompts_sha256"] != sha256(PROMPTS_JSON):
        print("prompts.json changed since the plan was made. Run the plan stage again.", file=sys.stderr)
        return 2
    manifest_path = base / "MANIFEST.json"
    old = read_json(manifest_path) if manifest_path.is_file() else {"files": []}
    entries = {e["file"]: e for e in old["files"]}
    scratch = Path(tempfile.mkdtemp(prefix="chaarteeke-audio-"))
    langs = args.langs or LANGS
    failed: list[str] = []
    made = 0
    for lang in langs:
        todo = [
            i
            for i in plan["items"]
            if i["lang"] == lang
            and not (base / i["path"]).is_file()
            and (not args.only or args.only in i["path"])
        ]
        if args.limit:
            todo = todo[: args.limit]
        have = sum(1 for i in plan["items"] if i["lang"] == lang) - len(todo)
        print(f"[{now_utc()}] {lang}: {len(todo)} to make, {have} already there", flush=True)
        if not todo:
            continue
        speaker = Speaker(lang, Path(args.models_dir), args.threads)
        for n, item in enumerate(todo, start=1):
            started = time.time()
            out = base / item["path"]
            try:
                wave, dropped = speaker.speak(item["speak"], item["path"])
                to_phone_wav(wave, speaker.rate, out, scratch)
            except Exception as e:
                failed.append(f"{item['path']}: {e}")
                print(f"  FAILED {item['path']}: {e}", flush=True)
                continue
            entries[item["path"]] = dict(
                file=item["path"],
                lang=lang,
                text=item["text"],
                spoken_text=item["speak"],
                source=item["source"],
                model=f"facebook/{MODELS[lang]}",
                generated_at=now_utc(),
                seconds=wav_seconds(out),
                characters_not_spoken=dropped,
                review_status=REVIEW_STATUS,
            )
            made += 1
            if dropped:
                print(f"  note {item['path']}: characters the model cannot say: {dropped}", flush=True)
            print(
                f"  [{n}/{len(todo)}] {item['path']} {entries[item['path']]['seconds']}s "
                f"(took {time.time() - started:.1f}s)",
                flush=True,
            )
            write_json(manifest_path, manifest_doc(entries, args.target))
    write_json(manifest_path, manifest_doc(entries, args.target))
    print(f"[{now_utc()}] done: {made} made, {len(failed)} failed", flush=True)
    for line in failed:
        print("  FAILED", line)
    return 1 if failed else 0


def manifest_doc(entries: dict[str, dict], target: str) -> dict:
    return dict(
        note=(
            "Machine voice drafts made by scripts/make_prompt_audio.py with Meta MMS-TTS (CC-BY-NC 4.0, "
            "prototype only). Not read or approved by a native speaker. Replace with human recordings."
        ),
        target=target,
        format="8 kHz mono 16-bit PCM WAV, loudness-normalised with ffmpeg loudnorm",
        files=sorted(entries.values(), key=lambda e: e["file"]),
    )


# ======================================================================= check
def cmd_check(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(ROOT))
    from chaarteeke.ivr.prompts import PromptCatalogue

    plan_path = AUDIO_DIR / "PLAN.json"
    plan = read_json(plan_path) if plan_path.is_file() else {}
    meta = read_json(PROMPTS_JSON)["meta"]
    ward_nos = sorted(f["properties"]["ward_no"] for f in read_json(DATA / "wards.geojson")["features"])
    clinics = plan.get("clinics") or list(DEFAULT_CLINICS)
    times = plan.get("times") or list(meta["spoken_times"]["hi"])
    cat = PromptCatalogue(PROMPTS_JSON, AUDIO_DIR, clinic_ids=clinics, ward_nos=ward_nos, times=times)
    bad = 0
    for lang in LANGS:
        missing = cat.missing(lang)
        print(f"missing({lang}): {len(missing)}")
        for m in missing[: args.show]:
            print("   ", m)
        if len(missing) > args.show:
            print(f"    ... and {len(missing) - args.show} more")
        bad += len(missing)
    unavailable = plan.get("unavailable", [])
    print(f"unavailable in the plan (words do not exist yet): {len(unavailable)}")
    manifest_path = AUDIO_DIR / "MANIFEST.json"
    files = read_json(manifest_path)["files"] if manifest_path.is_file() else []
    known = {e["file"] for e in files}
    on_disk = {p.relative_to(AUDIO_DIR).as_posix() for p in AUDIO_DIR.rglob("*.wav")}
    print(f"wav files {len(on_disk)}; in MANIFEST.json {len(known)}; not in MANIFEST {len(on_disk - known)}")
    unplanned: list[tuple[float, str]] = []
    secs = sorted((e["seconds"], e["file"]) for e in files if e["file"] in on_disk)
    if secs:
        print(f"shortest: {secs[:3]}")
        print(f"longest:  {secs[-3:]}")
        short = [s for s in secs if s[0] < 0.25]
        print(f"under 0.25 s: {short if short else 'none'}")
        long = [s for s in secs if s[0] > LONG_PROMPT_S]
        print(f"long (over {LONG_PROMPT_S:g} s): {long if long else 'none'}")
        # Only the opt-in full notice may be long: it is heard only when the listener presses 0.
        unplanned = [s for s in long if Path(s[1]).stem not in LONG_BY_DESIGN]
        print(f"long and not by design: {unplanned if unplanned else 'none'}")
    return 1 if bad or (on_disk - known) or unplanned else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan", help="list the files and their words (app env)")
    pl.add_argument("--clinics", nargs="*", help=f"clinic ids (default {' '.join(DEFAULT_CLINICS)})")
    pl.add_argument("--times", nargs="*", help="HHMM visit times (default: every time in meta.spoken_times)")
    pl.add_argument("--data-dir", default=str(DATA))
    sub.add_parser("clips-plan", help="list the demo place clips and their words (app env)")
    g = sub.add_parser("generate", help="speak what is missing (voice env)")
    g.add_argument("--target", choices=("prompts", "clips"), default="prompts")
    g.add_argument("--langs", nargs="*", choices=LANGS)
    g.add_argument("--limit", type=int, help="make at most N files per language (a trial)")
    g.add_argument("--only", help="only paths containing this text")
    g.add_argument("--threads", type=int, default=3)
    g.add_argument("--models-dir", default=str(VOICE_MODELS))
    c = sub.add_parser("check", help="missing files and a duration spot-check (app env)")
    c.add_argument("--show", type=int, default=12)
    args = p.parse_args(argv)
    try:
        return {"plan": cmd_plan, "clips-plan": cmd_clips_plan, "generate": cmd_generate, "check": cmd_check}[
            args.cmd
        ](args)
    except PlanError as e:
        print(f"Cannot continue: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
