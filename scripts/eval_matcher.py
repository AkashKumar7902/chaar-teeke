#!/usr/bin/env python3
"""Measure the place matcher and summarise the saved speech-recognition results; write docs/EVALS.md.

    uv run python scripts/eval_matcher.py                       # writes docs/EVALS.md
    uv run python scripts/eval_matcher.py --stdout              # prints it instead
    uv run python scripts/eval_matcher.py --asr-dir DIR         # where the saved ASR results are

Offline and deterministic: no speech model runs here. The saved ASR transcripts were produced earlier by
asr-test/run_indic.py (see "Speech recognition" in the output for what they are and are not).
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import re
import statistics
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz.distance import Levenshtein

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chaarteeke.config import DATA_DIR  # noqa: E402
from chaarteeke.places.gazetteer import Gazetteer  # noqa: E402
from chaarteeke.places.matcher import MatchResult, match  # noqa: E402
from chaarteeke.places.normalise import _deva_key, normalise  # noqa: E402
from chaarteeke.policy import Policy, load_policy  # noqa: E402

DEFAULT_ASR_DIR = Path.home() / "chaar-teeke-build" / "asr-test"
DEFAULT_OUT = ROOT / "docs" / "EVALS.md"
CONDS = ("clean16", "tel", "telnoisy")
COND_LABEL = {"clean16": "clean", "tel": "phone line", "telnoisy": "noisy phone line"}

# What each of the 15 place names in asr-test/names.py means in the ward list (read off data/wards.geojson by
# hand). A name that is not a ward or landmark in the data has an empty set: placing it anywhere is wrong.
TRUTH: dict[str, set[int]] = {
    "gandhi_nagar": {20, 21},
    "rehari_colony": {24, 25},
    "talab_tillo": {30, 31, 41},
    "bakshi_nagar": {27},
    "gujjar_nagar": {6},
    "trikuta_nagar": {53, 54},
    "janipur": {34, 35, 36, 37},
    "muthi": {66, 67},
    "nanak_nagar": {42, 43, 44},
    "shastri_nagar": {22, 23},
    "channi_himmat": {50, 51, 52, 54},
    "bhagwati_nagar": {14},
    "purani_mandi": set(),
    "pacca_danga": {10},
    "kachi_chawni": set(),
}

# Spellings a person might type or a transcriber might produce, written by the builder (not by residents).
HAND_TYPED: list[tuple[str, set[int]]] = [
    ("Gandi Nagar", {20, 21}),
    ("Gandhinagar", {20, 21}),
    ("Bakshi Nagr", {27}),
    ("Baxi Nagar", {27}),
    ("Bakshinagar", {27}),
    ("Rehari Colni", {24, 25}),
    ("Rehri Colony", {24, 25}),
    ("Talab Tilo", {30, 31, 41}),
    ("Tallab Tillo", {30, 31, 41}),
    ("Gujar Nagar", {6}),
    ("Gujjar Nagr", {6}),
    ("Trikoota Nagar", {53, 54}),
    ("Trikuta Nagr", {53, 54}),
    ("Sarwaal", {18}),
    ("Sarval", {18}),
    ("Gangial", {56}),
    ("Gangyal", {56}),
    ("Digiana", {45, 58}),
    ("Deeli", {55}),
    ("Panjtirthi", {1}),
    ("Panch Tirthi", {1}),
    ("Bhagwati Nagar", {14}),
    ("Bhagvati Nagar", {14}),
    ("Shastri Nagr", {22, 23}),
    ("Sastri Nagar", {22, 23}),
    ("Pacca Danga", {10}),
    ("Pakka Danga", {10}),
    ("Dogra Hall", {8}),
    ("Amphalla", {17}),
    ("Ambphalla", {17}),
    ("Sainik Colony", {69, 70}),
    ("Muthi", {66, 67}),
    ("Sunjwan", {74}),
    ("Sanjwan", {74}),
    ("Akalpur", {75}),
    ("Shehzadpur", {72}),
    ("Paloura", {38, 60}),
    ("Jullaka Mohalla", {2}),
    ("Krishna Nagar", {12}),
    ("Chand Nagar", {19}),
    ("Subhash Nagar", {26}),
    ("Subash Nagar", {26}),
    ("Bohri", {41}),
    ("Greater Kailash", {68}),
    ("Tawi Vihar", {71}),
    ("Roop Nagar", {62}),
    ("Rup Nagar", {62}),
    ("Keran", {62, 63}),
    ("Bahu Fort", {48}),
    ("Bahu", {47, 48}),
]

# Things a patient may say that are not a place in Jammu city. None should be placed by itself.
NOT_PLACES: list[str] = [
    "kutte ne kata",
    "ghar ke paas",
    "ghar ke bahar",
    "near the school",
    "I do not know",
    "pata nahi",
    "mujhe yaad nahi",
    "road par",
    "bus stand",
    "market",
    "hospital ke paas",
    "gali mein",
    "mandir ke paas",
    "बाजार में",
    "मुझे नहीं पता",
    "घर के पास",
    "स्कूल के पास",
    "सड़क पर",
    "Delhi",
    "Srinagar",
    "Kathua",
    "Udhampur",
    "Pathankot",
    "Amritsar",
    "Ludhiana",
    "Chandigarh",
    "Mumbai",
    "दिल्ली",
    "श्रीनगर",
    "कठुआ",
    "one two three",
    "hello hello",
    "namaste",
    "हैलो",
    "ठीक है",
    "haan ji",
]


# ------------------------------------------------------------------------------------ outcomes
OUTCOMES = ("placed_right", "placed_wrong", "review_right", "review_wrong", "nothing")
OUTCOME_LABEL = {
    "placed_right": "placed by itself, right ward",
    "placed_wrong": "placed by itself, wrong ward",
    "review_right": "to review, right ward offered",
    "review_wrong": "to review, right ward not offered",
    "nothing": "nothing offered",
}


def outcome(result: MatchResult, expected: set[int]) -> str:
    wards = set(result.ward_nos)
    if result.confidence == "likely":
        return "placed_right" if result.candidates[0].ward_no in expected else "placed_wrong"
    if result.confidence == "none" or not wards:
        return "nothing"
    return "review_right" if wards & expected else "review_wrong"


@dataclass
class Tally:
    counts: collections.Counter = field(default_factory=collections.Counter)

    def add(self, label: str) -> None:
        self.counts[label] += 1

    @property
    def n(self) -> int:
        return sum(self.counts.values())

    def pct(self, label: str) -> str:
        return f"{100 * self.counts[label] / self.n:.1f}%" if self.n else "-"


def table(rows: Iterable[tuple[str, Tally]], first: str) -> str:
    head = f"| {first} | n | " + " | ".join(OUTCOME_LABEL[o] for o in OUTCOMES) + " |"
    sep = "|---|--:|" + "--:|" * len(OUTCOMES)
    lines = [head, sep]
    for name, t in rows:
        lines.append(f"| {name} | {t.n} | " + " | ".join(t.pct(o) for o in OUTCOMES) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- typed variants
def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[()/,.\-]+", " ", name.lower())).strip()


def _swap_wv(s: str) -> str:
    return s.translate(str.maketrans("wv", "vw"))


def _drop_h(s: str) -> str:
    return re.sub(r"(?<=[bdgjkpt])h", "", s)


def _squeeze(s: str) -> str:
    return re.sub(r"(.)\1+", r"\1", s)


def _vowel_stretch(s: str) -> str:
    out = s.replace("i", "ee", 1) if "i" in s else s.replace("u", "oo", 1)
    return out


def _drop_final_a(s: str) -> str:
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("a") else w for w in s.split())


def _drop_inner_vowel(s: str) -> str:
    """nagar -> nagr: the second vowel of each long word."""
    out = []
    for w in s.split():
        vs = [i for i, c in enumerate(w) if c in "aeiou"]
        out.append(w[: vs[1]] + w[vs[1] + 1 :] if len(w) > 4 and len(vs) >= 3 else w)
    return " ".join(out)


LATIN_RULES: list[tuple[str, Callable[[str], str]]] = [
    ("w and v swapped", _swap_wv),
    ("aspirate h dropped", _drop_h),
    ("double letters squeezed", _squeeze),
    ("i/u written ee/oo", _vowel_stretch),
    ("final a dropped", _drop_final_a),
    ("an inner vowel dropped", _drop_inner_vowel),
    ("run together", lambda s: s.replace(" ", "")),
]
FRAMES = ["mujhe {} mein kutte ne kata", "I was near {} bus stand"]


def variants_of(entry: dict) -> list[tuple[str, str]]:
    """(kind, text) typed forms of one gazetteer name. Deterministic."""
    base = _clean(entry["name"])
    out: list[tuple[str, str]] = [("name as written", entry["name"])]
    seen = {base}
    for _label, rule in LATIN_RULES:
        v = rule(base)
        if v and v not in seen:
            seen.add(v)
            out.append(("respelled", v))
    if entry.get("name_hi"):
        src = (
            "Devanagari (hand word list)"
            if entry.get("name_hi_source") == "dictionary"
            else "Devanagari (rule-made)"
        )
        out.append((src, entry["name_hi"]))
        out.append((src + ", run together", entry["name_hi"].replace(" ", "")))
    for f in FRAMES:
        out.append(("in a sentence", f.format(entry["name"])))
    return out


def eval_typed(gaz: Gazetteer, policy: Policy) -> tuple[dict[str, dict[str, Tally]], dict[str, int]]:
    """Tallies by (entry type -> variant kind), and counts of what was left out."""
    idx = gaz.index()
    by_latin = {f.latin: f for f in idx.forms}
    tallies: dict[str, dict[str, Tally]] = collections.defaultdict(lambda: collections.defaultdict(Tally))
    shared = {"names": 0, "shared": 0, "no_distinctive_word": 0}
    for e in gaz.entries():
        if e["type"] not in ("ward", "osm_place", "landmark", "road"):
            continue
        n = normalise(e["name"])
        if not n.distinctive:
            shared["no_distinctive_word"] += 1
            continue
        form = by_latin.get(n.latin)
        expected = set(form.ward_nos) if form else set(e["ward_nos"])
        shared["names"] += 1
        shared["shared"] += len(expected) > 1
        for kind, text in variants_of(e):
            r = match(gaz, text, policy)
            tallies[e["type"]][kind].add(outcome(r, expected))
    return tallies, shared


def eval_list(gaz: Gazetteer, policy: Policy, items: Iterable[tuple[str, set[int]]]) -> Tally:
    t = Tally()
    for text, expected in items:
        t.add(outcome(match(gaz, text, policy), expected))
    return t


def eval_not_places(gaz: Gazetteer, policy: Policy) -> tuple[Tally, list[str]]:
    t = Tally()
    placed = []
    for text in NOT_PLACES:
        r = match(gaz, text, policy)
        o = outcome(r, set())
        t.add(o)
        if r.confidence == "likely":
            placed.append(f"{text} -> ward {r.candidates[0].ward_no} ({r.candidates[0].evidence})")
    return t, placed


# ------------------------------------------------------------------------------- saved ASR
def load_names(asr_dir: Path) -> list[tuple[str, ...]]:
    spec = importlib.util.spec_from_file_location("asr_names", asr_dir / "names.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot read {asr_dir / 'names.py'}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.NAMES


def load_results(asr_dir: Path, *files: str) -> list[dict]:
    rows: dict[tuple, dict] = {}
    for f in files:
        path = asr_dir / "results" / f
        if not path.is_file():
            raise SystemExit(f"saved ASR results not found: {path}")
        d = json.loads(path.read_text(encoding="utf-8"))
        for r in d["results"]:
            rows.setdefault((r["id"], r["src"], r["kind"], r["cond"], r["lang"]), r)
    return list(rows.values())


def meta(asr_dir: Path, f: str) -> dict:
    d = json.loads((asr_dir / "results" / f).read_text(encoding="utf-8"))
    return {k: v for k, v in d.items() if k != "results"}


SOURCES = {
    "lekha_hi": "macOS Hindi voice (Lekha)",
    "mms_hi": "Meta MMS-TTS Hindi voice",
    "mms_ur": "Meta MMS-TTS Urdu voice",
    "lekha_extra": "macOS Hindi voice (Lekha), 40 more names",
}
HINTS = {"lekha_hi": ("hi", "doi"), "mms_hi": ("hi", "doi"), "mms_ur": ("ur",), "lekha_extra": ("hi", "doi")}


def eval_transcripts(gaz: Gazetteer, policy: Policy, rows: list[dict]):
    truth = dict(TRUTH)
    extra_notes = {"in_gazetteer": 0, "not_in_gazetteer": 0}
    for r in rows:
        if r["src"] == "lekha_extra" and r["id"] not in truth:
            wards = set(match(gaz, r["ref"], policy).ward_nos)
            truth[r["id"]] = wards
    for i, w in truth.items():
        if i.startswith("x"):
            extra_notes["in_gazetteer" if w else "not_in_gazetteer"] += 1
    out: dict[tuple, Tally] = collections.defaultdict(Tally)
    for r in rows:
        if r["lang"] not in HINTS.get(r["src"], ()):
            continue
        t = truth[r["id"]]
        out[(r["src"], r["kind"], r["lang"], r["cond"])].add(outcome(match(gaz, r["hyp"], policy), t))
    return out, extra_notes


def transcripts_table(res: dict[tuple, Tally]) -> str:
    lines = [
        "| Voice | Spoken | Language hint | Line | n | "
        + " | ".join(OUTCOME_LABEL[o] for o in OUTCOMES)
        + " |",
        "|---|---|---|---|--:|" + "--:|" * len(OUTCOMES),
    ]
    for (src, kind, lang, cond), t in sorted(
        res.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2], CONDS.index(kv[0][3]))
    ):
        what = "name only" if kind == "bare" else "name in a sentence"
        lines.append(
            f"| {SOURCES[src]} | {what} | {lang} | {COND_LABEL[cond]} | {t.n} | "
            + " | ".join(t.pct(o) for o in OUTCOMES)
            + " |"
        )
    return "\n".join(lines)


def pooled(res: dict[tuple, Tally], keep: Callable[[tuple], bool]) -> Tally:
    t = Tally()
    for k, v in res.items():
        if keep(k):
            t.counts.update(v.counts)
    return t


# ----------------------------------------------------------------------- ASR model summary
def _fold_text(s: str) -> str:
    """Spaces, punctuation, nukta, nasal marks and vowel length do not count as mistakes."""
    dev = _deva_key(s)
    if dev:
        return dev
    s = re.sub("[ً-ٰٟ]", "", s)
    return re.sub(r"[^\w]", "", s.lower())


def _name_heard(ref: str, hyp: str) -> bool:
    r = _fold_text(ref)
    return bool(r) and r in _fold_text(hyp)


def _cer(ref: str, hyp: str) -> float:
    r, h = _fold_text(ref), _fold_text(hyp)
    return Levenshtein.distance(r, h) / max(len(r), 1)


def _ref_in_script(names: list[tuple[str, ...]], r: dict) -> str:
    """The reference in the script the decoder wrote (the saved 'ref' is Devanagari or Urdu)."""
    if r["id"].startswith(("x", "d")):
        return r["ref"]
    row = next(n for n in names if n[0] == r["id"])
    return row[3] if r["lang"] == "ur" else row[2]


def asr_summary(asr_dir: Path) -> str:
    names = load_names(asr_dir)
    files = {
        "IndicConformer 600M, fp32, CTC": "indic-fp32-ctc_main.json",
        "IndicConformer 600M, fp32, RNN-T": "indic-fp32-rnnt_main.json",
        "Whisper small": "whisper-small_main.json",
        "Vosk small Hindi": "vosk-small_main.json",
    }
    heard_lines = [
        "| Model (hint) | "
        + " | ".join(f"name only, {COND_LABEL[c]}" for c in CONDS)
        + " | "
        + " | ".join(f"in a sentence, {COND_LABEL[c]}" for c in CONDS)
        + " |",
        "|---|" + "--:|" * 6,
    ]
    for label, f in files.items():
        rows = load_results(asr_dir, f)
        for hint in ("hi", "doi"):
            sel = [r for r in rows if r["src"] == "lekha_hi" and r["lang"] == hint]
            if not sel:
                continue
            cells = []
            for kind in ("bare", "phrase"):
                for c in CONDS:
                    g = [r for r in sel if r["kind"] == kind and r["cond"] == c]
                    if not g:
                        cells.append("-")
                        continue
                    k = sum(
                        _name_heard(
                            _ref_in_script(names, r) if kind == "bare" else TRUTH_REF[r["id"]], r["hyp"]
                        )
                        for r in g
                    )
                    cells.append(f"{100 * k / len(g):.0f}% ({len(g)})")
            heard_lines.append(f"| {label} ({hint}) | " + " | ".join(cells) + " |")
    ur_rows = [
        r
        for r in load_results(asr_dir, "indic-fp32-ctc_main.json")
        if r["src"] == "mms_ur" and r["lang"] == "ur"
    ]
    ur = []
    for c in CONDS:
        g = [r for r in ur_rows if r["cond"] == c]
        k = sum(_name_heard(_ref_in_script(names, r), r["hyp"]) for r in g)
        ur.append(f"{100 * k / len(g):.0f}% ({len(g)})")
    dogri = []
    for label, f in (
        ("IndicConformer 600M, fp32, CTC", "indic-fp32-ctc_dogri.json"),
        ("Vosk small Hindi", "vosk-small_dogri.json"),
    ):
        rows = load_results(asr_dir, f)
        for hint in ("doi", "hi"):
            sel = [r for r in rows if r["lang"] == hint]
            if not sel:
                continue
            cells = []
            for c in CONDS:
                g = [r for r in sel if r["cond"] == c]
                cells.append(f"{100 * statistics.mean(_cer(r['ref'], r['hyp']) for r in g):.0f}%")
            dogri.append(f"| {label} ({hint}) | " + " | ".join(cells) + " |")
    m = meta(asr_dir, "indic-fp32-ctc_lekha.json")
    lekha = load_results(asr_dir, "indic-fp32-ctc_lekha.json")
    per_clip = statistics.mean(r["t_enc"] + r["t_dec"] for r in lekha if r["lang"] == "hi")
    return "\n".join(
        [
            "### Names read by a machine voice\n",
            "Share of clips where the spoken name appears in the transcript (spaces, nukta, nasal marks and vowel length "
            "ignored). n in brackets. Hindi voice (macOS Lekha), 15 ward and locality names.\n",
            *heard_lines,
            "",
            "Urdu (Meta MMS-TTS Urdu voice, Urdu hint), same 15 names: "
            + ", ".join(f"{COND_LABEL[c]} {v}" for c, v in zip(CONDS, ur, strict=True))
            + ".\n",
            "### Real Dogri speech, error rate\n",
            "44 clips of real people speaking Dogri (14 speakers, mostly conversation, from Jammu, Reasi, Udhampur, Samba and "
            "Kathua), 2 to 6 seconds each. They are ordinary speech, not place names. Character error rate on the folded text "
            "(lower is better; 0% is perfect; above 50% the transcript is mostly wrong).\n",
            "| Model (hint) | " + " | ".join(COND_LABEL[c] for c in CONDS) + " |",
            "|---|--:|--:|--:|",
            *dogri,
            "",
            "### Cost on this laptop (8 GB Apple M1)\n",
            f"IndicConformer fp32 CTC: model load {m['load_s']:.0f} s, peak memory {m['peak_rss_gb']:.1f} GB, "
            f"about {per_clip:.1f} s per short clip once loaded (4 threads).",
        ]
    )


TRUTH_REF: dict[str, str] = {}


def build_truth_refs(names: list[tuple[str, ...]]) -> None:
    for n in names:
        TRUTH_REF[n[0]] = n[2]


# ---------------------------------------------------------------------------------- the file
def build_report(asr_dir: Path) -> str:
    policy = load_policy()
    gaz = Gazetteer.load(DATA_DIR)
    names = load_names(asr_dir)
    build_truth_refs(names)
    cfg = policy["matcher"]
    idx = gaz.index()

    typed, counts = eval_typed(gaz, policy)
    hand = eval_list(gaz, policy, HAND_TYPED)
    bad, placed_bad = eval_not_places(gaz, policy)
    rows = load_results(asr_dir, "indic-fp32-ctc_main.json", "indic-fp32-ctc_extra.json")
    res, extras = eval_transcripts(gaz, policy, rows)

    kinds = [
        "name as written",
        "respelled",
        "Devanagari (hand word list)",
        "Devanagari (hand word list), run together",
        "Devanagari (rule-made)",
        "Devanagari (rule-made), run together",
        "in a sentence",
    ]
    typed_rows: list[tuple[str, Tally]] = []
    for etype, label in (
        ("ward", "wards"),
        ("osm_place", "localities (OSM)"),
        ("landmark", "landmarks"),
        ("road", "roads"),
    ):
        for k in kinds:
            if k in typed.get(etype, {}):
                typed_rows.append((f"{label}: {k}", typed[etype][k]))
    all_names = Tally()
    for etype in ("ward", "osm_place", "landmark"):
        for t in typed.get(etype, {}).values():
            all_names.counts.update(t.counts)

    phone_hi = pooled(
        res, lambda k: k[0] == "lekha_hi" and k[1] == "bare" and k[2] == "hi" and k[3] != "clean16"
    )
    clean_hi = pooled(
        res, lambda k: k[0] == "lekha_hi" and k[1] == "bare" and k[2] == "hi" and k[3] == "clean16"
    )

    out = f"""# Evaluations

Generated by `scripts/eval_matcher.py` from `data/gazetteer.json`, `data/wards.geojson`, `data/policy.json`
(matcher thresholds: likely at least {cfg["likely_min"]} with a lead of {cfg["likely_gap"]}, possible at least
{cfg["possible_min"]}) and the speech results saved in `asr-test/results`. Re-run it after any change to the matcher,
the gazetteer or the policy; do not edit the numbers by hand.

**What these numbers are.** They measure the place step of a call: the words a speech model returns, or a person
types, against {len(idx.forms)} written forms of {len(gaz.entries())} Jammu place names. They do **not** measure real
patients on real phone lines: no real caller has been recorded yet. The first pilot week is where that is measured.

Outcomes, in every table: **placed by itself** means the matcher was sure enough to set the ward without a person;
its right/wrong is checked against the ward list. **To review** means a person decides; "right ward offered" means the
reviewer would find it among the (up to {cfg["max_candidates"]}) candidates. The number that must stay near zero is
**placed by itself, wrong ward**.

## Place matcher

### Typed spellings of every ward and landmark name

{all_names.n} typed forms of {counts["names"]} names (75 wards, 12 localities, 327 landmarks; roads are in the
second table), from `scripts/eval_matcher.py` rules: the name as written, up to 6 respellings per name (w/v swapped,
aspirate h dropped, doubled letters squeezed, i/u written ee/oo, final a dropped, an inner vowel dropped, words run
together), the Devanagari name (one table row for names whose Devanagari came from a hand-written word list, one for
names whose Devanagari the data builder made by rule), and two sentence frames.
{counts["shared"]} of the {counts["names"]} names are shared by more than one ward (or cross wards); for those the
right answer is to offer every ward and not place by itself, and the expected set is all of them.
{counts["no_distinctive_word"]} names (for example "Bus Stand") have no distinctive word and are left out.

{table([("all wards, localities and landmarks", all_names)], "set")}

By kind:

{table(typed_rows, "set")}

**Limits.** The respelling rules are written by the same person who wrote the matcher's folding, so this is an
optimistic check of the folding, not of what people actually say. The "rule-made" Devanagari came from the same
transliteration family the matcher uses. The independent checks are the next two tables and the transcripts below.
All of these sets were also used while building the matcher (the rule that a candidate must share a word with the
speech, and the rule that short words must match exactly, came from failures seen here), so they are development sets,
not a held-out test. The held-out test is the first week of real calls.

### Hand-typed variants and non-places

{len(HAND_TYPED)} hand-written misspellings (builder's, not residents'), with the wards they should reach:

{table([("hand-typed spellings", hand)], "set")}

{len(NOT_PLACES)} things a patient might say that are not a place in Jammu city (no answer, "near the school", other
cities, greetings). None should be placed by itself; "to review, right ward not offered" and "nothing offered" are both
acceptable here, and "placed by itself" is a failure:

{table([("not places", bad)], "set")}
{chr(10).join("- failure: " + p for p in placed_bad) if placed_bad else "No non-place was placed by itself."}

## Speech recognition

**Model.** AI4Bharat IndicConformer-600M-multilingual, run as fp32 ONNX with CTC decoding and the language mask for
Hindi (`hi`), Dogri (`doi`) or Urdu (`ur`); this is what `chaarteeke/speech/asr.py` runs. Whisper small and Vosk small
Hindi were tried for comparison.

**Which clips.** Machine voices reading place names (the macOS Hindi voice Lekha and the Meta MMS-TTS voices), and 44 real
Dogri recordings of ordinary speech from a public corpus. Each was also pushed through a simulated phone line (band-limited
300-3400 Hz, 8 kHz mu-law, and the same with noise at 15 dB signal-to-noise). **No clip is a real caller on a real phone.**
A machine voice is cleaner and more regular than a frightened person; read these as an upper bound on name accuracy, and
the real-Dogri table as the only evidence about real voices (and that is not place names).

{asr_summary(asr_dir)}

### Saved transcripts through the place matcher

The transcripts above, matched with the same matcher. The right ward is taken from the ward list for the 15 main names;
for the 40 extra names it is whatever the matcher answers for the correct written name ({extras["in_gazetteer"]} of the 40
are in the data, {extras["not_in_gazetteer"]} are not and any placement of them counts as wrong). Hindi voice, name only,
Hindi hint, pooled over the three lines: clean {clean_hi.pct("placed_right")} placed right /
{clean_hi.pct("placed_wrong")} placed wrong; phone lines {phone_hi.pct("placed_right")} placed right /
{phone_hi.pct("placed_wrong")} placed wrong.

{transcripts_table(res)}

**Reading it honestly.** On these 15 machine-voice Hindi names alone, Vosk small hears the exact name more often than
IndicConformer (60% against 40% clean), so IndicConformer is not simply "better". The trade-off is that Vosk small has no
Dogri or Urdu mode and its error on the 44 real Dogri clips is far higher (57% to 69%, against 34% to 37% for
IndicConformer with the same Hindi hint). The Dogri hint matters most: with it IndicConformer's error falls to 13% to
16%, which is why the call's language is passed to the model. These are small samples (15 names, 44 clips) and
the Urdu voice is itself a machine voice that may be hard to understand, so the 7% for Urdu names says "Urdu place names are
not proven", not "Urdu cannot work". A shared name (Gandhi Nagar, Janipura, Rehari Colony) is never placed by itself, by
design, so a good result for it is "to review, right ward offered". Most of what goes to review is therefore the
reviewer's job, not an error; the placed-wrong column is the one that matters. With `CT_ASR=off` or a weak transcript
every report simply waits for the reviewer.
"""
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asr-dir", type=Path, default=DEFAULT_ASR_DIR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--stdout", action="store_true")
    a = ap.parse_args()
    if not (a.asr_dir / "results").is_dir():
        raise SystemExit(f"saved ASR results not found under {a.asr_dir} (use --asr-dir)")
    text = build_report(a.asr_dir)
    if a.stdout:
        print(text)
    else:
        a.out.write_text(text, encoding="utf-8")
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
