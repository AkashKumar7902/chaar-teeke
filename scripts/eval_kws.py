#!/usr/bin/env python3
"""Measure gazetteer keyword spotting (chaarteeke/places/kws.py) against the transcript matcher.

Four stages, because only the first two need the machine's speech and voice models:

    uv run python scripts/eval_kws.py make-clips      # speak the non-place phrases (macOS Lekha, Meta MMS-TTS)
    uv run python scripts/eval_kws.py collect         # ONE model load: frames + transcript of every clip -> cache
    uv run python scripts/eval_kws.py tune            # threshold sweep on the TUNING sets only
    uv run python scripts/eval_kws.py report          # score with data/policy.json, write docs/EVALS.md section

`collect` is the only stage that loads the speech model (about 2.2 GB); run nothing else that loads a model
at the same time. The others read the cache. `report` rewrites only the section 'Gazetteer keyword spotting' of
docs/EVALS.md, between its marker lines.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import pickle
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_ASR_DIR = Path.home() / "chaar-teeke-build" / "asr-test"
DEFAULT_WORK = Path.home() / "chaar-teeke-build" / "kws"
DEFAULT_MODEL_DIR = DEFAULT_ASR_DIR / "models" / "indic-conformer-600m-fp32"
VOICE_PYTHON = Path.home() / "chaar-teeke-build" / "voice" / ".venv" / "bin" / "python"
VOICE_MODELS = Path.home() / "chaar-teeke-build" / "voice" / "models"

# Things a patient might say that are not a place in Jammu city, spoken for the false-alarm check. The list is the
# same as NOT_PLACES in eval_matcher.py (no answer, "near the school", other cities, greetings), written in each
# script, plus a few longer sentences. None names a ward.
NON_PLACE_HI = [
    "कुत्ते ने काटा", "घर के पास", "घर के बाहर", "स्कूल के पास", "मुझे नहीं पता", "पता नहीं", "मुझे याद नहीं",
    "सड़क पर", "बस स्टैंड", "बाजार में", "अस्पताल के पास", "गली में", "मंदिर के पास", "दिल्ली", "श्रीनगर",
    "कठुआ", "ऊधमपुर", "पठानकोट", "अमृतसर", "लुधियाना", "चंडीगढ़", "मुंबई", "एक दो तीन", "हैलो हैलो", "नमस्ते",
    "हैलो", "ठीक है", "हाँ जी", "मैं खेत के पास था", "मुझे कुछ याद नहीं आ रहा", "दुकान के सामने",
    "रेलवे स्टेशन के पास", "पार्क में", "मैं बाजार जा रहा था", "रात को कुत्ते ने काटा", "कुत्ता सफेद था",
]  # fmt: skip
NON_PLACE_UR = [
    "کتے نے کاٹا", "گھر کے پاس", "گھر کے باہر", "سکول کے پاس", "مجھے نہیں پتہ", "پتہ نہیں", "مجھے یاد نہیں",
    "سڑک پر", "بس اڈہ", "بازار میں", "ہسپتال کے پاس", "گلی میں", "مسجد کے پاس", "دہلی", "سری نگر", "کٹھوعہ",
    "ادھم پور", "پٹھان کوٹ", "امرتسر", "لدھیانہ", "چندی گڑھ", "ممبئی", "ایک دو تین", "ہیلو ہیلو",
    "السلام علیکم", "ہیلو", "ٹھیک ہے", "جی ہاں", "میں کھیت کے پاس تھا", "مجھے کچھ یاد نہیں آ رہا",
    "دکان کے سامنے", "ریلوے سٹیشن کے پاس", "پارک میں", "میں بازار جا رہا تھا", "رات کو کتے نے کاٹا",
    "کتا سفید تھا",
]  # fmt: skip

# The 40 more names of asr-test/score.py DEV_EXTRA, in the same order, in Urdu as a person would write them (written by
# hand for this evaluation, not made by the rules in kws.py, so the rules are tested against spellings they did not make).
URDU_EXTRA = [
    "امفلا", "گول گجرال", "روپ نگر", "سینک کالونی", "ڈوگرہ ہال", "رگھوناتھ بازار", "پنج تیرتھی", "بن تالاب",
    "ستواری", "چھنی رام", "ٹرانسپورٹ نگر", "باہو فورٹ", "نروال", "بشناہ", "اکھنور روڈ", "تالاب کھٹیکاں",
    "مشری والا", "پریم نگر", "شکتی نگر", "پیر مٹھا", "جیل روڈ", "ریس کورس", "درگا نگر", "پٹیل نگر",
    "سنجواں", "کنجوانی", "ڈگیانہ", "گول مارکیٹ", "کنک منڈی", "پلورا", "گنگیال", "نانک نگر ایکسٹینشن",
    "سنجے نگر", "راجیو نگر", "جواہر نگر", "ریہاڑی چوک", "تالاب تلو چوک", "پرانی منڈی بازار", "بھگوتی چوک",
    "شہیدی چوک",
]  # fmt: skip

# Ward and locality names that are in neither list above, with the Urdu spelling a person would write (by hand, before
# the Urdu spelling rules were improved against the other two lists). The Devanagari is the data's own name_hi.
URDU_WARDS = [
    ("अकालपुर", "اکال پور"), ("भबरियां", "بھبریاں"), ("बोहड़ी", "بوہڑی"), ("चांद नगर", "چاند نگر"),
    ("डीली", "ڈیلی"), ("ग्रेटर कैलाश", "گریٹر کیلاش"), ("कांजी हाउस", "کانجی ہاؤس"),
    ("कृष्णा नगर", "کرشنا نگر"), ("मस्त गढ़", "مست گڑھ"), ("नई बस्ती", "نئی بستی"),
    ("नया प्लॉट", "نیا پلاٹ"), ("प्रताप गढ़", "پرتاپ گڑھ"), ("रेशम घर कॉलोनी", "ریشم گھر کالونی"),
    ("सरवाल", "سروال"), ("शिव नगर", "شیو نگر"), ("सुभाष नगर", "سبھاش نگر"), ("शहज़ादपुर", "شہزاد پور"),
    ("तोप शेरखानिया", "توپ شیرخانیہ"), ("तवी विहार", "توی وہار"), ("अशोक नगर", "اشوک نگر"),
    ("प्रीत नगर", "پریت نگر"), ("जुल्लका मोहल्ला", "جلکا محلہ"), ("मोहल्ला उस्ताद", "محلہ استاد"),
    ("पूंछ हाउस", "پونچھ ہاؤس"), ("राजपुरा मंगोत्रियां", "راجپورہ منگوتریاں"), ("अपर मुट्ठी", "اپر موٹھی"),
    ("लोअर मुट्ठी", "لوئر موٹھی"), ("सिद्धड़ा", "سدھڑا"), ("गंग्याल दो", "گنگیال دو"),
]  # fmt: skip

# "Near the railway station" was written as a non-place, but "Railway Station" is a gazetteer landmark (ward 20), so
# placing it is right. Found while checking the list against the matcher; left out of every false-alarm count.
LANDMARK_NOT_NON_PLACE = frozenset({"रेलवे स्टेशन के पास", "ریلوے سٹیشن کے پاس"})

# Run inside the voice environment (it has transformers; the app's environment does not).
_MMS_SCRIPT = r"""
import json, sys, zlib
import numpy as np, torch
from scipy.io import wavfile
from transformers import AutoTokenizer, VitsModel
models, out, jobs = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
for model_name, items in jobs.items():
    tok = AutoTokenizer.from_pretrained(f"{models}/{model_name}")
    model = VitsModel.from_pretrained(f"{models}/{model_name}").eval()
    for stem, text in items:
        torch.manual_seed(zlib.crc32(stem.encode()))
        with torch.no_grad():
            wave = model(**tok(text, return_tensors="pt")).waveform[0].numpy()
        wavfile.write(f"{out}/{stem}_mms.wav", model.config.sampling_rate, (np.clip(wave, -1, 1) * 32767).astype(np.int16))
"""


def _import_file(path: Path, name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot read {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_DOGRI_SCRIPT = r"""
import io, json, sys
exec(open("build_dogri.py").read().split("pf=pq.ParquetFile")[0])   # the sparse reader of the downloaded row groups
import pyarrow.parquet as pq, pandas as pd, soundfile as sf, subprocess, os
out, skip, per_speaker = sys.argv[1], set(json.loads(sys.argv[2])), int(sys.argv[3])
pf = pq.ParquetFile(Sparse())
made = []
for rg in RG:
    t = pf.read_row_group(rg, columns=["audio_filepath", "text", "duration", "scenario", "speaker_id"]).to_pandas()
    t["id"] = [f"d{rg}_{i}" for i in t.index]
    t = t[(t.duration >= 1.5) & (t.duration <= 12) & (t.scenario != "Read") & (~t.id.isin(skip))]
    t = t.sort_values(["speaker_id", "id"]).groupby("speaker_id").head(per_speaker)
    for _, r in t.iterrows():
        open(f"{out}/{r.id}_raw.bin", "wb").write(r.audio_filepath["bytes"])
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{out}/{r.id}_raw.bin", "-ac", "1", "-ar", "16000",
                        f"{out}/{r.id}_src.wav"], check=True)
        os.remove(f"{out}/{r.id}_raw.bin")
        made.append(dict(stem=r.id, text=r.text, speaker=r.speaker_id, seconds=float(r.duration), scenario=r.scenario))
json.dump(made, open(f"{out}/dogri2_made.json", "w"), ensure_ascii=False)
"""


def _to_16k(src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)],
        check=True,
    )


def make_clips(asr_dir: Path, work: Path) -> None:
    """Make the clips that are not in asr-test, each pushed through the same simulated phone line as the others
    (asr-test/deg.py). A clip that exists is not made again, so the frames cache stays valid.

      non-place phrases   NON_PLACE_HI with macOS Lekha and MMS-TTS Hindi, NON_PLACE_UR with MMS-TTS Urdu
      Urdu names          URDU_EXTRA with MMS-TTS Urdu
      more real Dogri     recordings of the corpus rows already downloaded for asr-test and not used there
                          (spontaneous speech, up to 4 per speaker), kept only if what is said names no place
    """
    import soundfile as sf

    deg = _import_file(asr_dir / "deg.py", "asr_deg")
    out = work / "clips"
    out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []

    def todo(stem: str) -> bool:
        return not (out / f"{stem}__clean16.wav").is_file()

    for i, text in enumerate(NON_PLACE_HI):
        stem = f"nonplace_lekha_hi_{i:02d}"
        manifest.append(dict(stem=stem, voice="lekha_hi", lang="hi", text=text))
        if todo(stem):
            subprocess.run(["say", "-v", "Lekha", "-o", str(out / f"{stem}.aiff"), text], check=True)
            _to_16k(out / f"{stem}.aiff", out / f"{stem}_src.wav")
            (out / f"{stem}.aiff").unlink()
    jobs: dict[str, list[tuple[str, str]]] = {"mms-tts-hin": [], "mms-tts-urd-script_arabic": []}
    for i, text in enumerate(NON_PLACE_HI):
        manifest.append(dict(stem=f"nonplace_mms_hi_{i:02d}", voice="mms_hi", lang="hi", text=text))
    for i, text in enumerate(NON_PLACE_UR):
        manifest.append(dict(stem=f"nonplace_mms_ur_{i:02d}", voice="mms_ur", lang="ur", text=text))
    for i, text in enumerate(URDU_EXTRA):
        manifest.append(dict(stem=f"mms_ur_extra_{i:02d}", voice="mms_ur_extra", lang="ur", text=text))
    for i, (deva, text) in enumerate(URDU_WARDS):
        manifest.append(
            dict(stem=f"mms_ur_wards_{i:02d}", voice="mms_ur_wards", lang="ur", text=text, deva=deva)
        )
    for m in manifest:
        if m["voice"] != "lekha_hi" and todo(m["stem"]):
            jobs["mms-tts-hin" if m["lang"] == "hi" else "mms-tts-urd-script_arabic"].append(
                (m["stem"], m["text"])
            )
    jobs = {k: v for k, v in jobs.items() if v}
    if jobs:
        subprocess.run(
            [
                str(VOICE_PYTHON),
                "-c",
                _MMS_SCRIPT,
                str(VOICE_MODELS),
                str(out),
                json.dumps(jobs, ensure_ascii=False),
            ],
            check=True,
        )
        for items in jobs.values():
            for stem, _ in items:
                _to_16k(out / f"{stem}_mms.wav", out / f"{stem}_src.wav")
                (out / f"{stem}_mms.wav").unlink()
    # more real Dogri speech
    d2 = work / "dogri2_manifest.json"
    if not d2.is_file():
        used = [
            m["id"]
            for m in json.loads((asr_dir / "data" / "manifest_dogri.json").read_text(encoding="utf-8"))
        ]
        py = asr_dir / ".venv" / "bin" / "python"
        subprocess.run(
            [str(py), "-c", _DOGRI_SCRIPT, str(out), json.dumps(used), "4"], check=True, cwd=asr_dir
        )
        made = json.loads((out / "dogri2_made.json").read_text(encoding="utf-8"))
        (out / "dogri2_made.json").unlink()
        d2.write_text(json.dumps(made, ensure_ascii=False, indent=1), encoding="utf-8")
    for m in json.loads(d2.read_text(encoding="utf-8")):
        if todo("dogri2_" + m["stem"]):
            (out / f"{m['stem']}_src.wav").rename(out / f"dogri2_{m['stem']}_src.wav")
        manifest.append(dict(stem="dogri2_" + m["stem"], voice="dogri_real_2", lang="doi", text=m["text"]))
    for m in manifest:
        src = out / f"{m['stem']}_src.wav"
        if src.is_file():
            x, sr = sf.read(src)
            if sr != 16000:
                raise SystemExit(f"{src} is {sr} Hz, expected 16000")
            deg.degrade(x, str(out / m["stem"]), pad=0.3)
            src.unlink()
    (work / "nonplace_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"{len(manifest)} clips in {out} (each on 5 lines)")


# ============================================================================== the clips
CONDS_FILE = {
    "clean": "clean16.wav",
    "phone16": "tel.wav",
    "noisy16": "telnoisy.wav",
    "phone": "tel8k.mulaw.wav",
    "noisy": "telnoisy8k.mulaw.wav",
}
COND_LABEL = {
    "clean": "clean",
    "phone": "phone line",
    "noisy": "noisy phone line",
    "phone16": "phone line (16 kHz)",
    "noisy16": "noisy phone line (16 kHz)",
}
# Which set of clips was used to choose thresholds. Everything else is held out: no threshold, rule or
# algorithm constant was looked at against it.
TUNING_SETS = ("lekha_extra", "nonplace_lekha_hi")
# Looked at while the rule-made Urdu spellings were improved (not used for thresholds): no longer held out.
DEVELOPMENT_SETS = ("mms_ur", "mms_ur_extra")
VOICE_LABEL = {
    "lekha_name": "macOS Hindi voice (Lekha), 15 names",
    "lekha_sentence": "macOS Hindi voice (Lekha), 15 names in a sentence",
    "lekha_extra": "macOS Hindi voice (Lekha), 40 more names",
    "mms_hi": "Meta MMS-TTS Hindi voice, 15 names",
    "mms_ur": "Meta MMS-TTS Urdu voice, 15 names",
    "mms_ur_extra": "Meta MMS-TTS Urdu voice, 40 more names (spelled by hand)",
    "mms_ur_wards": "Meta MMS-TTS Urdu voice, 29 ward and locality names (spelled by hand)",
    "mms_pa": "Meta MMS-TTS Punjabi voice, 15 names (Punjabi is not an app language)",
    "demo": "demo clips (Meta MMS-TTS Hindi voice, 8 kHz)",
    "nonplace_lekha_hi": "macOS Hindi voice (Lekha), 35 non-place phrases",
    "nonplace_mms_hi": "Meta MMS-TTS Hindi voice, 35 non-place phrases",
    "nonplace_mms_ur": "Meta MMS-TTS Urdu voice, 35 non-place phrases",
    "dogri_real": "real Dogri speakers, 44 sentences (no place name in them)",
    "dogri_real_2": "real Dogri speakers, more sentences (none names a place in the data)",
}


@dataclass(frozen=True)
class Clip:
    key: str  # unique: file or id, condition, language hint
    set: str  # a key of VOICE_LABEL
    lang: str  # the language hint given to the model: hi, doi or ur
    cond: str  # a key of COND_LABEL
    kind: str  # name | sentence | unknown_name | not_place | real_speech
    uid: str  # the utterance (the five lines of one utterance share it)
    truth: frozenset[int]  # wards the spoken name can mean (empty: it names no ward)
    path: Path | None = None  # a wav file, or None for a clip made in memory (see `source`)
    source: str = ""  # for in-memory clips: "noisy:<path>"

    @property
    def tuning(self) -> bool:
        return self.set in TUNING_SETS

    def wav(self) -> bytes:
        if self.path is not None and not self.source:
            return self.path.read_bytes()
        return noisy_variant(Path(self.source.removeprefix("noisy:")))


def noisy_variant(path: Path) -> bytes:
    """The demo clip with band-limited noise at 15 dB signal-to-noise added (the recipe of asr-test/deg.py, without
    the codec round trip: the demo clips are already 8 kHz telephone audio)."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import butter, sosfilt

    x, sr = sf.read(path, dtype="float64")
    rng = np.random.default_rng(7)
    sos = butter(4, [300, 3400], btype="band", fs=sr, output="sos")
    n = sosfilt(sos, rng.standard_normal(len(x)))
    n *= np.sqrt(np.mean(x**2) / (10 ** (15 / 10)) / np.mean(n**2))
    buf = io.BytesIO()
    sf.write(buf, np.clip(x + n, -1, 1), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def written_truth(gaz, text: str, policy) -> frozenset[int]:
    """The wards a correctly spelled name means: those of the data's forms spelled exactly so; for a name the data
    spells differently (or lacks), the wards the transcript matcher offers for the correct spelling."""
    from chaarteeke.places.matcher import match

    want = " ".join(text.split())
    exact = {w for f in gaz.written_forms() if f.text == want for w in f.ward_nos}
    return frozenset(exact or match(gaz, text, policy).ward_nos)


def _named_words(gaz) -> set[tuple[str, ...]]:
    """Every Devanagari or Urdu name of the data (with a distinctive word) as a tuple of words."""
    from chaarteeke.places.normalise import normalise

    return {
        tuple(f.text.split())
        for f in gaz.written_forms()
        if normalise(f.text).distinctive and not f.text.isascii()
    }


def _mentions_a_place(text: str, named: set[tuple[str, ...]]) -> bool:
    words = text.split()
    return any(
        tuple(words[i : i + len(n)]) == n
        for n in named
        for i in range(len(words) - len(n) + 1)
        if words[i] == n[0]
    )


def inventory(asr_dir: Path, work: Path, gaz, policy) -> list[Clip]:
    """Every clip to measure, with the wards its words mean. See VOICE_LABEL for the sets."""
    from chaarteeke.config import DATA_DIR

    em = _import_file(ROOT / "scripts" / "eval_matcher.py", "eval_matcher_for_kws")
    truth = {k: frozenset(v) for k, v in em.TRUTH.items()}
    clips: list[Clip] = []

    def add(set_: str, base: Path, lang: str, uid: str, kind: str, wards: frozenset[int], conds=CONDS_FILE):
        for cond, suffix in conds.items():
            path = Path(f"{base}__{suffix}")
            if not path.is_file():
                raise SystemExit(f"missing clip {path}")
            clips.append(Clip(f"{set_}/{path.name}/{lang}", set_, lang, cond, kind, uid, wards, path))

    man = json.loads((asr_dir / "clips" / "manifest.json").read_text(encoding="utf-8"))
    for m in man:
        wards = truth[m["id"]]
        kind = "name" if wards else "unknown_name"
        base = asr_dir / m["base"]
        if m["src"] == "lekha_hi":
            set_ = "lekha_name" if m["kind"] == "bare" else "lekha_sentence"
            kind = kind if m["kind"] == "bare" else ("sentence" if wards else "unknown_name")
            for lang in ("hi", "doi"):
                add(set_, base, lang, f"{set_}/{m['id']}", kind, wards)
        elif m["src"] == "mms_hi":
            for lang in ("hi", "doi"):
                add("mms_hi", base, lang, f"mms_hi/{m['id']}", kind, wards)
        elif m["src"] == "mms_ur":
            add("mms_ur", base, "ur", f"mms_ur/{m['id']}", kind, wards)
        elif m["src"] == "mms_pa":
            add("mms_pa", base, "hi", f"mms_pa/{m['id']}", kind, wards)
    for m in json.loads((asr_dir / "clips" / "manifest_extra.json").read_text(encoding="utf-8")):
        wards = written_truth(gaz, m["ref"], policy)
        for lang in ("hi", "doi"):
            add(
                "lekha_extra",
                asr_dir / m["base"],
                lang,
                f"lekha_extra/{m['id']}",
                "name" if wards else "unknown_name",
                wards,
            )
    for m in json.loads((asr_dir / "data" / "manifest_dogri.json").read_text(encoding="utf-8")):
        for lang in ("doi", "hi"):
            add("dogri_real", asr_dir / m["base"], lang, f"dogri_real/{m['id']}", "real_speech", frozenset())
    manifest = work / "nonplace_manifest.json"
    if not manifest.is_file():
        raise SystemExit(f"{manifest} not found: run the make-clips stage first")
    extra_truth = {
        i: written_truth(gaz, m["ref"], policy)
        for i, m in enumerate(
            json.loads((asr_dir / "clips" / "manifest_extra.json").read_text(encoding="utf-8"))
        )
    }
    named = _named_words(gaz)
    for m in json.loads(manifest.read_text(encoding="utf-8")):
        if m["text"] in LANDMARK_NOT_NON_PLACE:
            continue
        stem, base = m["stem"], work / "clips" / m["stem"]
        if m["voice"] == "mms_ur_extra":
            wards = extra_truth[int(stem.rsplit("_", 1)[1])]
            add(
                "mms_ur_extra", base, "ur", f"mms_ur_extra/{stem}", "name" if wards else "unknown_name", wards
            )
        elif m["voice"] == "mms_ur_wards":
            wards = written_truth(gaz, m["deva"], policy)
            add(
                "mms_ur_wards", base, "ur", f"mms_ur_wards/{stem}", "name" if wards else "unknown_name", wards
            )
        elif m["voice"] == "dogri_real_2":
            if _mentions_a_place(m["text"], named):
                continue  # what is said holds a name from the data: not a false-alarm clip
            for lang in ("doi", "hi"):
                add("dogri_real_2", base, lang, f"dogri_real_2/{stem}", "real_speech", frozenset())
        else:
            set_ = {
                "lekha_hi": "nonplace_lekha_hi",
                "mms_hi": "nonplace_mms_hi",
                "mms_ur": "nonplace_mms_ur",
            }[m["voice"]]
            add(set_, base, m["lang"], f"{set_}/{stem}", "not_place", frozenset())
    demo_dir = DATA_DIR / "demo" / "clips"
    for item in json.loads((demo_dir / "PLAN.json").read_text(encoding="utf-8"))["items"]:
        if item.get("kind") != "clip" or item.get("lang") != "hi":
            continue
        ward = item["ward_no"]
        wards = frozenset({ward}) if ward is not None else frozenset()
        kind = "name" if wards else "not_place"
        uid = f"demo/{item['path']}"
        path = demo_dir / item["path"]
        clips.append(Clip(f"demo/{item['path']}/hi/phone", "demo", "hi", "phone", kind, uid, wards, path))
        clips.append(
            Clip(
                f"demo/{item['path']}/hi/noisy",
                "demo",
                "hi",
                "noisy",
                kind,
                uid,
                wards,
                path,
                f"noisy:{path}",
            )
        )
    return clips


# ============================================================================== frames from the model
@dataclass
class Heard:
    text: str
    confidence: float | None
    logp: Any  # float32 (frames, 257)
    vocab: tuple[str, ...]
    lang: str
    seconds: float


def collect(clips: list[Clip], model_dir: Path, cache: Path) -> None:
    """One model load; every clip not yet in the cache through the app's own Transcriber; frames kept.
    Written to `cache` (pickle). A clip already there with the same audio is not run again."""
    import resource

    from chaarteeke.speech.asr import IndicConformerTranscriber

    heard: dict[str, Heard] = {}
    sha: dict[str, str] = {}
    runs: list[dict] = []
    if cache.is_file():
        old = load_cache(cache)
        heard, sha, runs = old["heard"], old["sha"], old["meta"]
    wavs = {c.key: c.wav() for c in clips}
    todo = [c for c in clips if sha.get(c.key) != hashlib.sha256(wavs[c.key]).hexdigest()]
    print(f"{len(todo)} of {len(clips)} clips to run")
    if not todo:
        return
    t0 = time.time()
    tr = IndicConformerTranscriber(model_dir)
    tr.load()
    load_s = time.time() - t0
    t1 = time.time()
    for n, c in enumerate(todo):
        r = tr.transcribe(wavs[c.key], c.lang)
        if r.frames is None:
            raise SystemExit("the transcriber returned no frames")
        heard[c.key] = Heard(r.text, r.confidence, r.frames.logp, r.frames.vocab, r.frames.lang, r.seconds)
        sha[c.key] = hashlib.sha256(wavs[c.key]).hexdigest()
        if n % 100 == 0:
            print(f"  {n}/{len(todo)} {c.key}: {r.text}", flush=True)
    meta = dict(
        model=tr.model_id,
        load_seconds=round(load_s, 1),
        decode_seconds=round(time.time() - t1, 1),
        clips=len(todo),
        peak_rss_gb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30, 2),
        made=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    rows = {k: dict(vars(v)) for k, v in heard.items()}
    with cache.open("wb") as f:
        pickle.dump(dict(meta=[*runs, meta], heard=rows, sha=sha), f)
    print(meta)


def load_cache(cache: Path) -> dict:
    if not cache.is_file():
        raise SystemExit(f"{cache} not found: run the collect stage first")
    with cache.open("rb") as f:
        data = _Unpickler(f).load()
    if isinstance(data["meta"], dict):
        data["meta"] = [data["meta"]]  # the first cache kept one run
    data["heard"] = {k: v if isinstance(v, Heard) else Heard(**v) for k, v in data["heard"].items()}
    return data


class _Unpickler(pickle.Unpickler):
    """The first cache was written when `Heard` lived in __main__; read it as the class it is now."""

    def find_class(self, module: str, name: str):
        if name == "Heard":
            return Heard
        return super().find_class(module, name)


# ============================================================================== scoring the clips
METHODS = ("transcript", "keyword", "combined")
METHOD_LABEL = {
    "transcript": "transcript matcher (as before)",
    "keyword": "keyword spotting alone",
    "combined": "combined (what the app now runs)",
}
LINES = ("clean", "phone", "noisy")  # the 8 kHz lines; the 16 kHz variants are in the saved results only


@dataclass(frozen=True)
class Row:
    clip: Clip
    method: str
    top1: bool  # the first candidate is a ward the name means
    top3: bool
    placed: bool  # placed by itself ("likely")
    placed_right: bool
    offered: bool  # at least one candidate


def precompute(clips: list[Clip], heard: dict[str, Heard], gaz, policy) -> dict[str, tuple[Any, Any]]:
    """Per clip: the transcript matcher's result and every keyword hit (floor 0). Thresholds are applied later."""
    from chaarteeke.places import kws
    from chaarteeke.places.matcher import match
    from chaarteeke.speech.asr import Frames

    spotter = kws.spotter_for(gaz)
    out = {}
    for c in clips:
        h = heard[c.key]
        out[c.key] = (match(gaz, h.text, policy), spotter.search(Frames(h.logp, h.vocab, h.lang), 0.0))
    return out


def score_clips(clips: list[Clip], heard: dict[str, Heard], pre: dict, cfg: dict[str, Any]) -> list[Row]:
    """Every clip through the three methods with the thresholds in `cfg` (the policy's matcher section)."""
    from chaarteeke.places import kws
    from chaarteeke.places.matcher import MatchResult

    rows: list[Row] = []
    for c in clips:
        text, found0 = pre[c.key]
        spoken = heard[c.key].text
        found = kws.KwsResult(
            [h for h in found0.hits if h.score >= cfg["kws_possible_min"]], found0.searched, found0.skipped
        )
        results = {
            "transcript": text,
            "keyword": kws.combine(MatchResult("none"), found, cfg, spoken),
            "combined": kws.combine(text, found, cfg, spoken),
        }
        for method, r in results.items():
            wards = [x.ward_no for x in r.candidates]
            placed = r.confidence == "likely"
            rows.append(
                Row(
                    c,
                    method,
                    top1=bool(wards) and wards[0] in c.truth,
                    top3=any(w in c.truth for w in wards),
                    placed=placed,
                    placed_right=placed and wards[0] in c.truth,
                    offered=bool(wards),
                )
            )
    return rows


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def pct(k: int, n: int) -> str:
    """'k/n = 62% (36-83)': the share with its 95% Wilson interval."""
    if n == 0:
        return "-"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.0f}% ({100 * lo:.0f}-{100 * hi:.0f})"


def _select(rows: list[Row], method: str, set_: str, lang: str, cond: str) -> list[Row]:
    return [
        r
        for r in rows
        if r.method == method and r.clip.set == set_ and r.clip.lang == lang and r.clip.cond == cond
    ]


def _cells(rows: list[Row]) -> list[tuple[str, str, str]]:
    seen = {(r.clip.set, r.clip.lang, r.clip.cond) for r in rows}
    return sorted(
        seen, key=lambda k: (list(VOICE_LABEL).index(k[0]), k[1], LINES.index(k[2]) if k[2] in LINES else 9)
    )


def _share(rows: list[Row], pred) -> str:
    return pct(sum(1 for r in rows if pred(r)), len(rows))


def _table_rows(rows: list[Row], sets: tuple[str, ...], with_truth: bool):
    """(set, lang, line, {method: rows}) for every cell that has clips of the wanted kind."""
    for set_, lang, cond in _cells(rows):
        if set_ not in sets or cond not in LINES:
            continue
        by = {
            m: [r for r in _select(rows, m, set_, lang, cond) if bool(r.clip.truth) == with_truth]
            for m in METHODS
        }
        if by["combined"]:
            yield set_, lang, cond, by


def recall_table(rows: list[Row], sets: tuple[str, ...]) -> str:
    out = [
        "| Voice | Language hint | Line | Names | transcript: right ward first | keywords alone: right ward first | combined: right ward first | combined: right ward in top 3 | transcript: in top 3 |",
        "|---|---|---|--:|--:|--:|--:|--:|--:|",
    ]
    for set_, lang, cond, by in _table_rows(rows, sets, True):
        out.append(
            f"| {VOICE_LABEL[set_]} | {lang} | {COND_LABEL[cond]} | {len(by['combined'])} | "
            f"{_share(by['transcript'], lambda r: r.top1)} | {_share(by['keyword'], lambda r: r.top1)} | "
            f"{_share(by['combined'], lambda r: r.top1)} | {_share(by['combined'], lambda r: r.top3)} | "
            f"{_share(by['transcript'], lambda r: r.top3)} |"
        )
    return "\n".join(out)


def placing_table(rows: list[Row], sets: tuple[str, ...]) -> str:
    out = [
        "| Voice | Language hint | Line | Names | transcript: placed, right ward | transcript: placed, wrong ward | combined: placed, right ward | combined: placed, wrong ward |",
        "|---|---|---|--:|--:|--:|--:|--:|",
    ]
    for set_, lang, cond, by in _table_rows(rows, sets, True):

        def right(r: Row) -> bool:
            return r.placed_right

        def wrong(r: Row) -> bool:
            return r.placed and not r.placed_right

        out.append(
            f"| {VOICE_LABEL[set_]} | {lang} | {COND_LABEL[cond]} | {len(by['combined'])} | "
            f"{_share(by['transcript'], right)} | {_share(by['transcript'], wrong)} | "
            f"{_share(by['combined'], right)} | {_share(by['combined'], wrong)} |"
        )
    return "\n".join(out)


def false_alarm_table(rows: list[Row], sets: tuple[str, ...]) -> str:
    out = [
        "| Speech | Language hint | Line | Clips | transcript: placed | keywords alone: placed | combined: placed | transcript: offered a ward | combined: offered a ward |",
        "|---|---|---|--:|--:|--:|--:|--:|--:|",
    ]
    for set_, lang, cond, by in _table_rows(rows, sets, False):
        label = VOICE_LABEL[set_] + ("" if set_ in NO_NAME_SETS else ", the names that are not in the data")

        def placed(r: Row) -> bool:
            return r.placed

        def offered(r: Row) -> bool:
            return r.offered

        out.append(
            f"| {label} | {lang} | {COND_LABEL[cond]} | {len(by['combined'])} | {_share(by['transcript'], placed)} | "
            f"{_share(by['keyword'], placed)} | {_share(by['combined'], placed)} | "
            f"{_share(by['transcript'], offered)} | {_share(by['combined'], offered)} |"
        )
    return "\n".join(out)


NO_NAME_SETS = ("nonplace_lekha_hi", "nonplace_mms_hi", "nonplace_mms_ur", "dogri_real", "dogri_real_2")
NAME_SETS = (
    "lekha_name",
    "lekha_sentence",
    "lekha_extra",
    "mms_hi",
    "mms_ur",
    "mms_ur_extra",
    "mms_ur_wards",
    "mms_pa",
    "demo",
)


def sweep(clips: list[Clip], heard: dict[str, Heard], pre: dict, policy) -> None:
    """Placed right / placed wrong / false alarms on the TUNING sets for a grid of thresholds. Prints; changes nothing."""
    tune = [c for c in clips if c.tuning]
    base = dict(policy["matcher"])
    print(f"{len(tune)} tuning clips ({', '.join(TUNING_SETS)})")
    for floor, lmin, gap, pieces in [
        (f, m, g, 3) for f in (3, 5, 10) for m in (30, 40, 50, 60, 80) for g in (10, 20, 30)
    ] + [(5, 40, 20, k) for k in (2, 4, 5, 6)]:
        cfg = dict(
            base,
            kws_possible_min=floor,
            kws_likely_min=lmin,
            kws_likely_gap=gap,
            kws_sure_min_pieces=pieces,
        )
        rows = [r for r in score_clips(tune, heard, pre, cfg) if r.method == "combined"]
        names = [r for r in rows if r.clip.truth]
        none = [r for r in rows if not r.clip.truth]
        print(
            f"floor {floor:2} likely_min {lmin:2} gap {gap:2} min_pieces {pieces}: names {len(names)}  "
            f"placed right {sum(r.placed_right for r in names)}  "
            f"placed wrong {sum(r.placed and not r.placed_right for r in names)}  "
            f"top1 {sum(r.top1 for r in names)}  top3 {sum(r.top3 for r in names)}  "
            f"not-names {len(none)}  placed {sum(r.placed for r in none)}"
        )


def tuning_margin(clips: list[Clip], pre: dict[str, tuple[Any, Any]], min_pieces: int) -> dict[str, float]:
    """On the tuning sets, the highest keyword score of the first hit when it is not a ward the clip means: for a
    name clip (any form; and only forms that could make keyword spotting sure: one ward, enough pieces), and for a
    clip that names no ward. Computed here so the text of the section cannot drift from the cache."""
    out = {"wrong_name_any": 0.0, "wrong_name_sure_capable": 0.0, "no_name_any": 0.0}
    for c in clips:
        if not c.tuning:
            continue
        hits = pre[c.key][1].hits
        if not hits:
            continue
        top = hits[0]
        if not c.truth:
            out["no_name_any"] = max(out["no_name_any"], top.score)
        elif top.ward_no not in c.truth:
            out["wrong_name_any"] = max(out["wrong_name_any"], top.score)
            if not top.ambiguous and top.pieces >= min_pieces:
                out["wrong_name_sure_capable"] = max(out["wrong_name_sure_capable"], top.score)
    return out


def timing(clips: list[Clip], heard: dict[str, Heard], gaz) -> dict[str, float]:
    """Seconds: building the name table once per language, and one search of a clip (median, 95th percentile)."""
    import statistics

    from chaarteeke.places import kws
    from chaarteeke.speech.asr import Frames

    spotter = kws.KeywordSpotter(gaz)  # a fresh one: the first call per language pays for the table
    build: dict[str, float] = {}
    for lang in ("hi", "doi", "ur"):
        c = next(c for c in clips if c.lang == lang)
        h = heard[c.key]
        t = time.perf_counter()
        spotter.search(Frames(h.logp, h.vocab, h.lang), 5.0)
        build[lang] = time.perf_counter() - t
    sample = [c for c in clips if not c.tuning][::10]
    each = []
    for c in sample:
        h = heard[c.key]
        t = time.perf_counter()
        spotter.search(Frames(h.logp, h.vocab, h.lang), 5.0)
        each.append(time.perf_counter() - t)
    each.sort()
    return dict(
        build_hi=build["hi"],
        build_doi=build["doi"],
        build_ur=build["ur"],
        median=statistics.median(each),
        p95=each[int(0.95 * (len(each) - 1))],
        n=len(each),
    )


BEGIN = "<!-- kws:begin (written by scripts/eval_kws.py report; do not edit by hand) -->"
END = "<!-- kws:end -->"


def build_section(clips: list[Clip], data: dict, gaz, policy) -> tuple[str, list[dict]]:
    heard = data["heard"]
    cfg = dict(policy["matcher"])
    pre = precompute(clips, heard, gaz, policy)
    rows = score_clips(clips, heard, pre, cfg)
    held = [r for r in rows if not r.clip.tuning and r.clip.set not in DEVELOPMENT_SETS]
    tuned = [r for r in rows if r.clip.tuning]
    developed = [r for r in rows if r.clip.set in DEVELOPMENT_SETS]
    tm = timing(clips, heard, gaz)
    margin = tuning_margin(clips, pre, cfg["kws_sure_min_pieces"])
    from chaarteeke.places import kws
    from chaarteeke.speech.asr import Frames

    first = heard[clips[0].key]
    table = kws.spotter_for(gaz).table(Frames(first.logp, first.vocab, "hi"))
    urdu = {k: len({c.uid for c in clips if c.set == k}) for k in ("mms_ur",)}
    n_utt = lambda sets: len({c.uid for c in clips if c.set in sets})  # noqa: E731
    meta = data["meta"]
    n_real = len({c.uid for c in clips if c.set in ("dogri_real", "dogri_real_2")})
    days = sorted({m["made"][:10] for m in meta})
    collected = days[0] if len(days) == 1 else f"{days[0]} to {days[-1]}"
    runs = {1: "one run", 2: "two runs", 3: "three runs"}.get(len(meta), f"{len(meta)} runs")
    text = f"""{BEGIN}
## Gazetteer keyword spotting

Written by `scripts/eval_kws.py` (stages `make-clips`, `collect`, `report`); the model's frames were collected on
{collected} and the tables were computed from them on {time.strftime("%d %B %Y")}. `report` rewrites only this section.

**What it does.** The speech model gives, for every 80 ms frame, a probability for each piece of its vocabulary. The
transcript keeps the likeliest piece per frame, so one wrong piece spoils a name the model almost heard. Keyword spotting
(`chaarteeke/places/kws.py`) writes each gazetteer name in the model's script (Devanagari from `name_hi` and
`aliases_devanagari`, plus names a reviewer taught; for Urdu, the Urdu forms in the data plus rule-made Urdu spellings of
the Devanagari ones), cuts it into the model's pieces in up to 4 ways, and scores the best CTC alignment of those pieces
over any stretch of the clip (the sound before and after the stretch costs nothing). The score is 100 x exp(mean over
pieces of log(probability of the piece / probability of the model's favourite piece in that frame)): 100 is a name the
model heard exactly, 50 is "each piece half as likely as the model's favourite". A name that is only part of a longer name
that scored as well loses 20. A name with no distinctive word ("Temple", "Bus Stand") is not searched, as in the
transcript matcher. A ward takes the best score of any form that can mean it. {len(table.forms)} Hindi and Dogri forms
are searched; {table.skipped} written forms in Devanagari could not be cut into pieces (almost all are ward numbers such
as "ward 75": the model's vocabulary has no digits) and are left to the transcript matcher.

**How it joins the transcript matcher** (`kws.combine`, thresholds in `data/policy.json` matcher, version
{policy.version}): a ward is placed by itself only when one source is sure and the other does not name a different ward.
Keyword spotting is sure of a ward when its score is at least {cfg["kws_likely_min"]}, it leads every other ward by at
least {cfg["kws_likely_gap"]}, and the name that scored it means one ward only and has at least {cfg["kws_sure_min_pieces"]} pieces (2 to 4
pieces gave the same tuning result, so 4 is a margin, not a measured optimum); the transcript matcher is sure when it says "likely". A
ward is offered to the reviewer when its keyword score is at least {cfg["kws_possible_min"]} or the transcript matcher
offers it. `review.process_capture` runs the combined matcher whenever the speech model gives its frames (always, with the
real model); a test double without frames gets the transcript matcher only.

**Which clips, and which were held out.** {len(clips)} clip/hint pairs from {n_utt(set(VOICE_LABEL))} utterances: the
machine-voice clips of the earlier sections (`asr-test/clips`), {n_real} real Dogri recordings (no place name in them, so
only for false alarms; 44 from `asr-test`, the rest taken from rows of the same corpus that `asr-test` did not use), the 12
demo clips, 29 Urdu ward and locality names spelled by hand, and non-place phrases spoken for this evaluation ("near the temple", "I do not
know", other cities, greetings; 35 phrases, three voices, made by `make-clips`). Each utterance is also on four telephone
lines; the tables show the clean file and the two 8 kHz mu-law lines, the 16 kHz variants are in the saved results. The
thresholds were chosen only on the **tuning sets** (the 40 more names read by Lekha, and the non-place phrases read by
Lekha); everything else is **held out**: none of it was looked at to set a threshold or an algorithm constant. The
tuning sets cannot tell thresholds apart on the dangerous side (no threshold in the grid placed a wrong ward or a false
alarm there), so `kws_likely_min` and `kws_likely_gap` were set with a margin. On the tuning sets the first keyword hit
scored at most {margin["wrong_name_any"]:.1f} when it was a ward the name does not mean, {margin["wrong_name_sure_capable"]:.1f} when its
form could make keyword spotting sure (one ward, at least {cfg["kws_sure_min_pieces"]} pieces), and {margin["no_name_any"]:.1f} on a phrase that names
no ward; `kws_likely_min` is {cfg["kws_likely_min"]}.
Two corrections to the clip lists were made before measuring held-out sets and are applied to every voice: "near the
railway station" is a gazetteer landmark (ward 20), so it was removed from the non-place phrases; and the wards of a
spoken name are those of the data's forms spelled exactly so (a road that crosses four wards means four wards).

Units: a **name** is one clip of a spoken place; "right ward first" means the first ward offered is one the name means
("top 3": any of the first three offered). "Placed" means set by itself ("likely"). Intervals are 95% Wilson intervals
on the number of utterances in the row; a row's lines are the same utterances, so rows are not independent of each other.
"Wrong ward" is a clip placed by itself in a ward the name does not mean (a name that is not in the data counts as wrong
anywhere).

### Held-out: does the right ward come first?

{recall_table(held, NAME_SETS)}

### Held-out: placed by itself, right and wrong

{placing_table(held, NAME_SETS)}

### Held-out: false alarms (no place named in the speech)

"Placed" is the failure. "Offered a ward" is only a longer reviewer list.

{false_alarm_table(held, tuple(VOICE_LABEL))}

### Tuning sets (used to choose thresholds, so these flatter the method)

{recall_table(tuned, NAME_SETS)}

{placing_table(tuned, NAME_SETS)}

{false_alarm_table(tuned, tuple(VOICE_LABEL))}

### Urdu development sets (the 15 Urdu names and the 40 more: the rule-made Urdu spellings were improved against them, so they are no longer held out)

{recall_table(developed, NAME_SETS)}

{placing_table(developed, NAME_SETS)}

{false_alarm_table(developed, tuple(VOICE_LABEL))}

### Cost on this laptop

Searching one clip: median {1000 * tm["median"]:.0f} ms, 95th percentile {1000 * tm["p95"]:.0f} ms ({tm["n"]} held-out
clips, one process, 8 GB Apple M1, other work running). The first search per language builds the table of names:
{tm["build_hi"]:.1f} s for Hindi, {tm["build_doi"]:.1f} s for Dogri, {tm["build_ur"]:.1f} s for Urdu (done once, again after
a reviewer teaches a name). The frames kept for a clip are about 40 KB for 2 seconds of speech. The model run that
produced the frames: {sum(m["clips"] for m in meta)} clips in {sum(m["decode_seconds"] for m in meta):.0f} s ({runs}, each after a load of
{min(m["load_seconds"] for m in meta):.0f} to {max(m["load_seconds"] for m in meta):.0f} s), peak {max(m["peak_rss_gb"] for m in meta)} GB of memory ({meta[0]["model"]}, the app's own `Transcriber`; the evaluation machine was busy with other
work, so the decode time is not a speed figure).

### Limits

- Every clip is a machine voice, or a real Dogri speaker saying something that is not a place. **No clip is a real caller
  naming a mohalla on a real line.** The recall numbers are an upper bound for that reason, and the first week of the pilot
  is the real test.
- Intervals are for the clips here; they do not cover other voices, other names, or the people of Jammu. Names and phrases
  are few (15 names per voice, 35 phrases), so intervals are wide.
- Urdu forms are rule-made from the Devanagari spellings (no short vowels; alternative spellings for letters Urdu
  writes more than one way) except {sum(1 for f in gaz.written_forms() if any("؀" <= ch <= "ۿ" for ch in f.text))} forms the data writes
  in Urdu. They were not checked by an Urdu speaker. The Urdu thresholds are the Hindi ones; no Urdu clip was in the
  tuning sets. The rule-made spellings were improved while looking at the 15 Urdu names and the 40 more, so those
  ({urdu["mms_ur"]} of the names) are not held out. The 29 ward and locality names and the non-place phrases are held out,
  with one caveat: the 29 names were spelled before their frames were computed, but `kws.py` was last edited in the same
  few minutes, and no log shows whether that edit used them, so read their numbers as nearly, not strictly, held out.
  Urdu recall is far lower than Hindi recall on every set; the cause was not isolated (spelling rules, the Urdu voice
  or the model's Urdu head are all possible).
- The Punjabi voice is not an app language (decoded with the Hindi hint); it is there to show how a different accent
  reading the same names behaves.
- The Dogri real speech has no place name, so it can only show false alarms, not recall. Dogri place names are untested.
- Names with a digit ("ward 12") are not searched (the model's vocabulary has no digits).
{END}
"""
    flat = [
        dict(
            key=r.clip.key,
            set=r.clip.set,
            lang=r.clip.lang,
            cond=r.clip.cond,
            kind=r.clip.kind,
            method=r.method,
            truth=sorted(r.clip.truth),
            top1=r.top1,
            top3=r.top3,
            placed=r.placed,
            placed_right=r.placed_right,
            offered=r.offered,
        )
        for r in rows
    ]
    return text, flat


def write_section(path: Path, section: str) -> None:
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    if BEGIN in old and END in old:
        head, rest = old.split(BEGIN, 1)
        tail = rest.split(END, 1)[1]
        new = head + section.rstrip("\n") + tail
    else:
        new = old.rstrip("\n") + "\n\n" + section
    path.write_text(new, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["make-clips", "collect", "tune", "report"])
    ap.add_argument("--asr-dir", type=Path, default=DEFAULT_ASR_DIR)
    ap.add_argument("--work", type=Path, default=DEFAULT_WORK)
    ap.add_argument("--docs", type=Path, default=ROOT / "docs" / "EVALS.md")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    a = ap.parse_args()
    a.work.mkdir(parents=True, exist_ok=True)
    cache = a.work / "frames.pkl"
    if a.stage == "make-clips":
        make_clips(a.asr_dir, a.work)
        return
    from chaarteeke.config import DATA_DIR
    from chaarteeke.places.gazetteer import Gazetteer
    from chaarteeke.policy import load_policy

    policy = load_policy()
    gaz = Gazetteer.load(DATA_DIR)
    clips = inventory(a.asr_dir, a.work, gaz, policy)
    if a.stage == "collect":
        print(f"{len(clips)} clips")
        collect(clips, a.model_dir, cache)
    else:
        data = load_cache(cache)
        stale = [
            c.key for c in clips if hashlib.sha256(c.wav()).hexdigest() != data["sha"].get(c.key)
        ]  # or absent
        if stale:
            raise SystemExit(
                f"{cache} does not match the clips on disk ({len(stale)} differ or are absent): run collect again"
            )
        if a.stage == "tune":
            tune = [c for c in clips if c.tuning]
            sweep(clips, data["heard"], precompute(tune, data["heard"], gaz, policy), policy)
        else:
            section, flat = build_section(clips, data, gaz, policy)
            (a.work / "results.json").write_text(json.dumps(flat, ensure_ascii=False), encoding="utf-8")
            if a.stdout:
                print(section)
            else:
                write_section(a.docs, section)
                print(f"wrote the keyword spotting section of {a.docs}")


if __name__ == "__main__":
    main()
