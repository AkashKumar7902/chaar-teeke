"""Gazetteer keyword spotting: look for each place name in the sound itself, not only in the transcript.

The speech model gives, for every frame (about 80 ms), a probability for each piece of its vocabulary. The
transcript keeps the likeliest piece of each frame and drops the rest, so one wrong piece spoils a name the model
almost heard. Here each name of the gazetteer is written in the model's script, cut into the model's pieces, and
scored against those probabilities:

  gain(frame, piece) = log p(piece) - log p(likeliest piece)     never above 0; 0 means "the model's own choice"
  score(name)        = the best CTC alignment of the name's pieces over any stretch of the clip, summed gains,
                       with the frames before and after the stretch free (they say nothing about the name)

A name the model heard exactly scores 0. The sum is divided by the number of pieces, so long and short names
compare, and shown as 100 x exp(mean gain): 100 is a perfect hearing, and 50 means each piece was, on the geometric
average, half as likely as the model's favourite for that frame. A name is cut into pieces in more than one way
(the model's own cut is not published), and the best cut counts.

A ward takes the best score of any form that can mean it, as in matcher.py. `combine` then puts these scores next to
the transcript matcher's result and gives the same three words: likely, possible, none. Thresholds are in
data/policy.json, section matcher (the kws_* keys). Importing this module loads no heavy library.
"""

from __future__ import annotations

import re
import threading
import unicodedata
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..models import Confidence
from .gazetteer import Gazetteer, WrittenForm
from .matcher import KNOWN_OUTSIDE, MatchResult, Scored
from .normalise import normalise, raw_words

if TYPE_CHECKING:
    from ..speech.asr import Frames

# Algorithm constants (not operator thresholds; those are in policy.matcher).
MAX_CUTS = 4  # how many different ways of cutting a name into pieces are tried
MIN_PIECES = 2  # a name cut into fewer pieces than this says too little (one piece can match anywhere)
MAX_URDU_SPELLINGS = 6  # rule-made Urdu spellings kept per Devanagari name
MAX_PIECES = 60  # a longer written form is a sentence-like landmark, not something said in a few seconds
SUBSUMED_PENALTY = 20.0  # a name found inside a longer name that scored at least as well loses this much
NEG = -1.0e9

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LATIN = re.compile(r"[A-Za-z]")
_SCRIPT_OF = {"hi": "devanagari", "doi": "devanagari", "ur": "arabic"}


# ----------------------------------------------------------------------------- results
@dataclass(frozen=True)
class Hit:
    ward_no: int
    score: float  # 0..100, see the module note
    evidence: str  # the gazetteer name that scored it
    ambiguous: bool  # the form can mean more than one ward, or its ward is only a guess
    form: str  # the written form that scored (for the evaluation, not for screens)
    pieces: int  # how many pieces the form has (its shortest cut): short forms match by chance more easily


@dataclass(frozen=True)
class KwsResult:
    hits: list[Hit]  # best first, one per ward, only those at or above the policy floor
    searched: int  # written forms searched
    skipped: int  # written forms in this script that could not be cut into the model's pieces

    @property
    def ward_nos(self) -> list[int]:
        return [h.ward_no for h in self.hits]


# ---------------------------------------------------------------------------- cutting names into pieces
def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


class Pieces:
    """The model's vocabulary as a cutter: a written name in, the ways it can be spelled in pieces out."""

    def __init__(self, vocab: tuple[str, ...]) -> None:
        self.blank = len(vocab) - 1
        self.by_first: dict[str, list[tuple[str, int]]] = {}
        for i, piece in enumerate(vocab):
            if i in (0, self.blank):
                continue  # <unk> and the blank are not pieces of a spelling
            p = _nfc(piece)
            if p:
                self.by_first.setdefault(p[0], []).append((p, i))

    def cut(self, text: str, k: int | None = None) -> list[tuple[int, ...]]:
        """Up to k ways to spell `text` (words joined by the word-start mark) as vocabulary pieces: the fewest
        pieces first, and among equals the longer pieces first. Empty when some letter is not in the vocabulary
        (a digit, say): such a name is not searched."""
        k = MAX_CUTS if k is None else k
        s = "▁" + "▁".join(_nfc(text).split())
        n = len(s)
        best: list[list[tuple[tuple[int, int], tuple[int, ...]]]] = [[] for _ in range(n + 1)]
        best[0] = [((0, 0), ())]
        for i in range(n):
            if not best[i]:
                continue
            for piece, idx in self.by_first.get(s[i], ()):
                if s.startswith(piece, i):
                    j = i + len(piece)
                    for (count, neg_sq), seq in best[i]:
                        best[j].append(((count + 1, neg_sq - len(piece) ** 2), (*seq, idx)))
            if i + 1 <= n:
                best[i + 1].sort(key=lambda c: c[0])
                del best[i + 1][k:]
        best[n].sort(key=lambda c: c[0])
        return [seq for _, seq in best[n][:k]]


# ----------------------------------------------------------------------------------- Urdu spellings
_U_CONS = {
    "क": "ک", "ख": "کھ", "ग": "گ", "घ": "گھ", "ङ": "نگ", "च": "چ", "छ": "چھ", "ज": "\x03", "झ": "جھ",
    "ञ": "ن", "ट": "ٹ", "ठ": "ٹھ", "ड": "ڈ", "ढ": "ڈھ", "ण": "ن", "त": "ت", "थ": "تھ", "द": "د",
    "ध": "دھ", "न": "ن", "प": "پ", "फ": "پھ", "ब": "ب", "भ": "بھ", "म": "م", "य": "ی", "र": "ر",
    "ल": "ل", "ळ": "ل", "व": "و", "श": "ش", "ष": "ش", "स": "س", "ह": "ہ",
}  # fmt: skip
_U_NUKTA = {"क": "ق", "ख": "خ", "ग": "غ", "ज": "ز", "ड": "ڑ", "ढ": "ڑھ", "फ": "ف"}
_U_VOWEL = {
    "अ": "ا", "आ": "آ", "इ": "ا", "ई": "ای", "उ": "ا", "ऊ": "او", "ए": "اے", "ऐ": "اے", "ओ": "او",
    "औ": "او", "ऑ": "آ",
}  # fmt: skip
# Letters Urdu spells more than one way are marked here and written by `deva_to_urdu`, which tries the alternatives.
_I, _U, _J, _AU, _END_A = "\x01", "\x02", "\x03", "\x04", "\x05"
_U_MATRA = {"ा": "ا", "ि": _I, "ी": "ی", "ु": _U, "ू": "و", "ॉ": "ا", "ृ": "ر", "ॅ": "ا", "ौ": _AU}
_U_E_MATRA = {"े", "ै"}
_U_O_MATRA = {"ो": "و"}
# The plain reading of each marker, then its alternative spelling.
_MARKS: dict[str, tuple[str, str]] = {
    _I: ("", "ی"),  # a short i is usually left out; Trikuta is written تریکوٹا
    _U: ("", "و"),  # a short u too; Muthi is written موٹھی, Janipur جانی پور
    _J: ("ج", "ز"),  # Hindi ज for a Persian ز: बाजार is written بازار
    _AU: ("و", "او"),  # चौक is چوک, but औ is او in some names
    _END_A: ("ا", "ہ"),  # a final आ: Dogra is written ڈوگرہ
}
# Letters written two ways that need no marker (a second spelling of the plain reading).
_U_ALTERNATE = {"کھ": "خ", "پھ": "ف"}


def deva_to_urdu(text: str) -> list[str]:
    """Rule-made Urdu spellings (no short vowels, as Urdu is written) of a Devanagari name: the plain reading
    first, then one spelling per letter that Urdu writes two ways, then the spelling with all of them. Empty when a
    letter has no rule. Only for names written in Devanagari; a form already in Urdu is used as it is."""
    base = [_u_word(w) for w in _nfc(text).split()]
    if any(w is None for w in base):
        return []
    marked = " ".join(w for w in base if w)

    def write(use: set[str]) -> str:
        out = marked
        for mark, (plain, alt) in _MARKS.items():
            out = out.replace(mark, alt if mark in use else plain)
        return out

    present = [m for m in _MARKS if m in marked]
    spellings = [write(set())]
    for plain, alt in _U_ALTERNATE.items():
        if plain in spellings[0]:
            spellings.append(spellings[0].replace(plain, alt))
    spellings += [write({m}) for m in present]
    if len(present) > 1:
        spellings.append(write(set(present)))
    return list(dict.fromkeys(spellings))[:MAX_URDU_SPELLINGS]


def _u_word(word: str) -> str | None:
    chars = list(unicodedata.normalize("NFD", word))
    out: list[str] = []
    prev_cons = ""
    i = 0
    while i < len(chars):
        c = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        last = i == len(chars) - 1 or (nxt == "़" and i + 2 == len(chars))
        if c in _U_CONS:
            if nxt == "़" and c in _U_NUKTA:
                out.append(_U_NUKTA[c])
                i += 1
            elif c == "य" and last and prev_cons:
                out.append("ے")  # a final y after a consonant: Sanjay is سنجے
            else:
                out.append(_U_CONS[c])
            prev_cons = c
        elif c == "्":
            nxt_c = chars[i + 1] if i + 1 < len(chars) else ""
            if nxt_c == prev_cons and nxt_c in _U_CONS:
                i += 1  # a doubled letter is written once
        elif c in _U_MATRA:
            out.append(_END_A if c == "ा" and last and len(out) > 3 else _U_MATRA[c])
            prev_cons = ""
        elif c in _U_E_MATRA:
            out.append("ے" if last else "ی")
            prev_cons = ""
        elif c in _U_O_MATRA:
            out.append(_U_O_MATRA[c])
            prev_cons = ""
        elif c in "ंँ":
            out.append("ں" if last else "ن")
        elif c in _U_VOWEL:
            out.append(_U_VOWEL[c])
            prev_cons = ""
        elif c == "़" or unicodedata.category(c).startswith("M"):
            pass
        elif c.isspace():
            pass
        else:
            return None
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------------------- the search
@dataclass
class _Table:
    key: tuple
    forms: list[WrittenForm]
    skipped: int
    pieces: list[int]  # per form: pieces in its shortest cut
    containers: list[list[int]]  # per form: the forms whose words contain this form's words, as a run
    # rows grouped by number of pieces: length -> (labels array (n, length) , form index array (n,))
    groups: dict[int, tuple[Any, Any]]


class KeywordSpotter:
    """Searches every written form of a gazetteer in the frames of a clip. One per gazetteer."""

    def __init__(self, gaz: Gazetteer) -> None:
        self._gaz = gaz
        self._lock = threading.Lock()
        self._tables: dict[tuple[str, tuple], _Table] = {}

    def table(self, frames: Frames) -> _Table:
        key = (frames.lang, self._gaz.forms_key())
        with self._lock:
            hit = self._tables.get(key)
            if hit is None:
                hit = self._tables[key] = self._build(frames.lang, frames.vocab, key)
                for stale in [k for k in self._tables if k[0] == frames.lang and k != key]:
                    del self._tables[stale]
            return hit

    def search(self, frames: Frames, floor: float) -> KwsResult:
        """Hits (one per ward, best first) whose score is at least `floor` (0..100)."""
        table = self.table(frames)
        scores = adjust_for_containers(score_forms(frames.logp, table), table)
        best: dict[int, Hit] = {}
        for fi, s in enumerate(scores):
            if s < floor:
                continue
            form = table.forms[fi]
            for ward in form.ward_nos:
                hit = Hit(
                    ward,
                    round(float(s), 1),
                    form.evidence_for(ward),
                    form.ambiguous,
                    form.text,
                    table.pieces[fi],
                )
                old = best.get(ward)
                if old is None or (hit.score, not hit.ambiguous) > (old.score, not old.ambiguous):
                    best[ward] = hit
        hits = sorted(best.values(), key=lambda h: (-h.score, h.ward_no))
        return KwsResult(hits, len(table.forms), table.skipped)

    # ------------------------------------------------------------------------------- building
    def _build(self, lang: str, vocab: tuple[str, ...], key: tuple) -> _Table:
        if lang not in _SCRIPT_OF:
            raise ValueError(f"no keyword spotting for language {lang!r}")
        cutter = Pieces(vocab)
        forms = _spoken_forms(self._gaz.written_forms(), _SCRIPT_OF[lang])
        kept: list[WrittenForm] = []
        shortest: list[int] = []
        by_len: dict[int, list[tuple[tuple[int, ...], int]]] = {}
        skipped = 0
        for f in forms:
            cuts = cutter.cut(f.text)
            cuts = [c for c in cuts if MIN_PIECES <= len(c) <= MAX_PIECES]
            if not cuts:
                skipped += 1
                continue
            kept.append(f)
            shortest.append(min(len(c) for c in cuts))
            for c in cuts:
                by_len.setdefault(len(c), []).append((c, len(kept) - 1))
        import numpy as np

        groups = {
            n: (
                np.array([r[0] for r in rows], dtype=np.int64),
                np.array([r[1] for r in rows], dtype=np.int64),
            )
            for n, rows in by_len.items()
        }
        return _Table(key, kept, skipped, shortest, _containers(kept), groups)


_SPOTTERS: weakref.WeakKeyDictionary[Gazetteer, KeywordSpotter] = weakref.WeakKeyDictionary()
_SPOTTERS_LOCK = threading.Lock()


def spotter_for(gaz: Gazetteer) -> KeywordSpotter:
    with _SPOTTERS_LOCK:
        sp = _SPOTTERS.get(gaz)
        if sp is None:
            sp = _SPOTTERS[gaz] = KeywordSpotter(gaz)
        return sp


def _spoken_forms(forms: list[WrittenForm], script: str) -> list[WrittenForm]:
    """The written forms in the model's script. For Urdu, Devanagari names get rule-made Urdu spellings; Latin
    and other scripts are not searched (they are not what this model writes)."""
    out: dict[str, WrittenForm] = {}

    def add(text: str, src: WrittenForm) -> None:
        f = out.setdefault(text, WrittenForm(text))
        for s in src.sources:
            if s not in f.sources:
                f.sources.append(s)

    for f in forms:
        if _LATIN.search(f.text) or not normalise(f.text).distinctive:
            continue  # a name of only place types ("Temple", "Bus Stand") says nothing, as in matcher.py
        if script == "devanagari" and _DEVANAGARI.search(f.text) and not _ARABIC.search(f.text):
            add(f.text, f)
        elif script == "arabic":
            if _ARABIC.search(f.text) and not _DEVANAGARI.search(f.text):
                add(f.text, f)
            elif _DEVANAGARI.search(f.text) and not _ARABIC.search(f.text):
                for spelling in deva_to_urdu(f.text):
                    add(spelling, f)
    return [out[t] for t in sorted(out)]


def _containers(forms: list[WrittenForm]) -> list[list[int]]:
    words = [tuple(f.text.split()) for f in forms]
    out: list[list[int]] = [[] for _ in forms]
    by_word: dict[str, list[int]] = {}
    for i, w in enumerate(words):
        for token in set(w):
            by_word.setdefault(token, []).append(i)
    for i, a in enumerate(words):
        for j in by_word.get(a[0], ()):
            b = words[j]
            if len(b) > len(a) and any(b[k : k + len(a)] == a for k in range(len(b) - len(a) + 1)):
                out[i].append(j)
    return out


def adjust_for_containers(scores: Any, table: _Table) -> Any:
    """A name that is only a part of a longer name ("Gole" in "Gole Market") is outranked when the longer name
    scored at least as well: the speaker said the longer one."""
    out = scores.copy()
    for i, boxes in enumerate(table.containers):
        if boxes and max(scores[j] for j in boxes) >= scores[i]:
            out[i] = max(0.0, scores[i] - SUBSUMED_PENALTY)
    return out


# ----------------------------------------------------------------------------------- the scoring
def score_forms(logp: Any, table: _Table) -> Any:
    """Score 0..100 of every form in the table against these frames (see the module note)."""
    import numpy as np

    scores = np.zeros(len(table.forms), dtype=np.float32)
    gains = np.asarray(logp, dtype=np.float32)
    gains = gains - gains.max(axis=1, keepdims=True)
    for n, (labels, form_idx) in table.groups.items():
        total = best_alignment_gain(gains, labels)
        per_piece = np.where(total > NEG / 2, total / n, -50.0)
        score = 100.0 * np.exp(np.maximum(per_piece, -50.0))
        np.maximum.at(scores, form_idx, score.astype(np.float32))
    return scores


def best_alignment_gain(gains: Any, labels: Any) -> Any:
    """For each row of `labels` (rows of piece ids, all the same length n), the best total gain of a CTC alignment
    of those pieces over any stretch of the frames: blanks may sit between and around pieces, a piece may span
    frames, and the frames before and after the stretch cost nothing. NEG when there are too few frames."""
    import numpy as np

    frames, vocab = gains.shape
    blank = vocab - 1
    rows, n = labels.shape
    states = 2 * n + 1
    ext = np.full((rows, states), blank, dtype=np.int64)
    ext[:, 1::2] = labels
    # a piece may follow the piece before it without a blank only when they differ
    skip_ok = np.zeros((rows, states), dtype=bool)
    skip_ok[:, 3::2] = labels[:, 1:] != labels[:, :-1]
    g = gains[:, ext]  # (frames, rows, states)
    alpha = np.full((rows, states), NEG, dtype=np.float32)
    best = np.full(rows, NEG, dtype=np.float32)
    for t in range(frames):
        stay = alpha
        step = np.empty_like(alpha)
        step[:, 0] = NEG
        step[:, 1:] = alpha[:, :-1]
        skip = np.full_like(alpha, NEG)
        skip[:, 2:] = np.where(skip_ok[:, 2:], alpha[:, :-2], NEG)
        nxt = np.maximum(np.maximum(stay, step), skip)
        nxt[:, 1] = np.maximum(nxt[:, 1], 0.0)  # a stretch may begin at any frame, with the first piece
        alpha = np.maximum(nxt + g[t], NEG)
        best = np.maximum(best, alpha[:, states - 2])
    return best


# --------------------------------------------------------------------------------------- combining
def combine(text: MatchResult, kws: KwsResult, cfg: dict[str, Any], spoken: str = "") -> MatchResult:
    """The transcript matcher's result and the keyword spotter's, as one result with the usual three words.

    Each ward's evidence is the transcript score (70 to 100, rescaled so that the policy's `possible_min` is 0) and
    the keyword score (0 to 100), joined as "either may be right": 100 x (1 - (1 - a)(1 - b)). Candidates are the
    wards either source offers, best first, at most `max_candidates`.

    A ward is placed ("likely") only when one source is sure and the other does not disagree:
      keyword sure   its score is at least `kws_likely_min`, it leads every other ward's keyword score by at least
                     `kws_likely_gap`, and the name that scored it means one ward only;
      transcript sure the transcript matcher said "likely" for that ward;
    and no sure source names a different ward, and the speech is not a known place outside Jammu. `spoken` is the
    transcript, for that last test only."""
    floor = cfg["kws_possible_min"]
    hits = {h.ward_no: h for h in kws.hits if h.score >= floor}
    t_floor = cfg["possible_min"]
    text_by = {c.ward_no: c for c in text.candidates}
    scored: dict[int, Scored] = {}
    for ward in hits.keys() | text_by.keys():
        k = hits[ward].score if ward in hits else 0.0
        t = text_by[ward] if ward in text_by else None
        t_scaled = max(0.0, (t.score - t_floor) / (100.0 - t_floor) * 100.0) if t else 0.0
        fused = 100.0 * (1.0 - (1.0 - k / 100.0) * (1.0 - t_scaled / 100.0))
        use_kws = ward in hits and (t is None or k >= t_scaled)
        evidence = hits[ward].evidence if use_kws else t.evidence  # type: ignore[union-attr]
        ambiguous = hits[ward].ambiguous if use_kws else t.ambiguous  # type: ignore[union-attr]
        scored[ward] = Scored(ward, round(fused, 1), evidence, ambiguous)
    ranked = sorted(scored.values(), key=lambda c: (-c.score, c.ward_no))[: cfg["max_candidates"]]
    if not ranked:
        return MatchResult("none", [], text.heard)
    top = ranked[0]
    kws_sure = _kws_sure(kws, hits, cfg)
    text_sure = text.confidence == "likely" and text.candidates[0].ward_no == top.ward_no
    outside = bool(raw_words(spoken) & KNOWN_OUTSIDE)
    disagree = (kws_sure is not None and kws_sure != top.ward_no) or (
        text.confidence == "likely" and text.candidates[0].ward_no != top.ward_no
    )
    kws_strong = (
        kws.hits and kws.hits[0].score >= cfg["kws_likely_min"] and kws.hits[0].ward_no != top.ward_no
    )
    sure = (kws_sure == top.ward_no or text_sure) and not disagree and not kws_strong and not outside
    confidence: Confidence = "likely" if sure else "possible"
    return MatchResult(confidence, ranked, text.heard)


def _kws_sure(kws: KwsResult, hits: dict[int, Hit], cfg: dict[str, Any]) -> int | None:
    """The ward the keyword spotter alone is sure of, or None."""
    if not kws.hits:
        return None
    first = kws.hits[0]
    rest = max((h.score for h in kws.hits[1:]), default=0.0)
    if (
        first.score >= cfg["kws_likely_min"]
        and first.score - rest >= cfg["kws_likely_gap"]
        and not first.ambiguous
        and first.pieces >= cfg["kws_sure_min_pieces"]
        and first.ward_no in hits
    ):
        return first.ward_no
    return None
