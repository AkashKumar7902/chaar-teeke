"""Words in, wards out: score a heard (or typed) place against every written form of every name.

A form's score is the best of four views of the same words, all on the folded text of `normalise`:
  whole    the whole string against the whole form (edit distance, word order ignored, spaces ignored)
  contain  every word of the form is found in the speech ("main gandhi nagar mein tha" holds "gandhi nagar")
  phonetic the consonant skeletons agree (names that differ only in their vowels), slightly discounted
  script   the same Devanagari spelling, exactly
Scores are 0-100 and never shown; the words `likely` and `possible` are. A ward takes the best score of any
form that can mean it. Only a `likely` result is placed without a person: the top score is at least
policy `likely_min`, leads the next ward by `likely_gap`, and the name that scored it means one ward only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz, process

from ..models import Confidence
from .gazetteer import Form, Gazetteer, Index
from .normalise import GENERIC, NormText, normalise, raw_words

# Algorithm constants (not operator thresholds; those are in policy.matcher). Tuned on the evaluation in
# docs/EVALS.md; scripts/eval_matcher.py reproduces it.
TOKEN_MATCH_MIN = 85  # a form's word counts as heard when a spoken word is this close
PHONETIC_WEIGHT = 0.92  # a skeleton match is worth a little less than the same spelling
PHONETIC_MIN_LEN = 3  # skeletons shorter than this say too little
SUBSUMED_PENALTY = 20  # a contained form is outranked by a longer form that covers more of the speech
SHARED_WORD_MIN = 75  # a candidate must share a word (or run-together spelling) at least this close
MIN_CONTAIN_SINGLE_LEN = 4  # a one-word name this short is not found inside a sentence
EXACT_TOKEN_LEN = 5  # a form word this short must be heard exactly: one wrong letter makes another word


# Places outside Jammu city that sound like a ward ("Delhi" folds to the same spelling as ward 55 "Deeli").
# Said as written, they are never placed by themselves; a person decides.
KNOWN_OUTSIDE = frozenset({"delhi", "dilli", "दिल्ली", "दिल्लि", "ڈیلی", "دہلی"})


@dataclass(frozen=True)
class Scored:
    ward_no: int
    score: float
    evidence: str  # the gazetteer name that matched
    ambiguous: bool  # the matching name can mean more than one ward, or its ward is only a guess


@dataclass(frozen=True)
class MatchResult:
    confidence: Confidence
    candidates: list[Scored] = field(default_factory=list)
    heard: str = ""  # the folded text that was compared (for tests and the evaluation, not for screens)

    @property
    def ward_nos(self) -> list[int]:
        return [c.ward_no for c in self.candidates]


def match(gaz: Gazetteer, text: str, policy: Any) -> MatchResult:
    """`policy` is the loaded Policy; thresholds come from policy['matcher']."""
    cfg = policy["matcher"]
    q = normalise(text)
    if not q.distinctive:
        return MatchResult("none", [], q.latin)
    idx = gaz.index()
    scores = _score_forms(q, idx, floor=cfg["possible_min"])
    top = _per_ward(idx, scores, cfg["possible_min"], q)
    if not top:
        return MatchResult("none", [], q.latin)
    top = top[: cfg["max_candidates"]] if len(top) > cfg["max_candidates"] else top
    first = top[0]
    runner_up = top[1].score if len(top) > 1 else 0.0
    sure = first.score >= cfg["likely_min"] and first.score - runner_up >= cfg["likely_gap"]
    if sure and not first.ambiguous and not raw_words(text) & KNOWN_OUTSIDE:
        confidence: Confidence = "likely"
    else:
        confidence = "possible"
    return MatchResult(confidence, top, q.latin)


# --------------------------------------------------------------------------------- scoring
def _score_forms(q: NormText, idx: Index, floor: float) -> dict[int, float]:
    """Best score per form index, for forms that reach the floor."""
    best: dict[int, float] = {}

    def take(i: int, s: float) -> None:
        if s >= floor and s > best.get(i, 0.0):
            best[i] = s

    for scorer in (fuzz.ratio, fuzz.token_sort_ratio):
        for _, s, i in process.extract(q.latin, idx.latin, scorer=scorer, score_cutoff=floor, limit=None):
            take(i, s)
    if len(q.tokens) > 1:
        for _, s, i in process.extract(
            q.latin_joined, idx.joined, scorer=fuzz.ratio, score_cutoff=floor, limit=None
        ):
            take(i, s)
    if len(q.phonetic.replace(" ", "")) >= PHONETIC_MIN_LEN:
        for _, s, i in process.extract(
            q.phonetic,
            idx.phonetic,
            scorer=fuzz.ratio,
            score_cutoff=min(100.0, floor / PHONETIC_WEIGHT),
            limit=None,
        ):
            if len(idx.phonetic[i].replace(" ", "")) >= PHONETIC_MIN_LEN:
                take(i, PHONETIC_WEIGHT * s)
    for i, s in _contained(q, idx).items():
        take(i, s)
    for i in idx.deva.get(q.devanagari_joined, ()):
        take(i, 100.0)
    return best


def _units(tokens: tuple[str, ...]) -> list[tuple[str, frozenset[int]]]:
    """Spoken words, plus each pair run together ("gandhi nagar" may have been heard as "gandhinagar")."""
    units = [(t, frozenset({i})) for i, t in enumerate(tokens)]
    units += [(tokens[i] + tokens[i + 1], frozenset({i, i + 1})) for i in range(len(tokens) - 1)]
    return units


def _contained(q: NormText, idx: Index) -> dict[int, float]:
    """Forms whose words are all found in the speech, scored by how close the found words are."""
    units = _units(q.tokens)
    unit_words = [u[0] for u in units]
    seeds: set[int] = set()
    for tok in q.distinctive:
        for hit, _, _ in process.extract(
            tok, idx.vocab, scorer=fuzz.ratio, score_cutoff=TOKEN_MATCH_MIN, limit=None
        ):
            seeds.update(idx.by_token[hit])
    found: dict[int, tuple[float, frozenset[int]]] = {}
    for i in seeds:
        f = idx.forms[i]
        got = _find_form(f, unit_words, units)
        if got is not None:
            found[i] = got
    out: dict[int, float] = {}
    for i, (s, covered) in found.items():
        outranked = any(covered < other for j, (_, other) in found.items() if j != i)
        out[i] = s - SUBSUMED_PENALTY if outranked else s
    return out


def _find_form(
    f: Form, unit_words: list[str], units: list[tuple[str, frozenset[int]]]
) -> tuple[float, frozenset[int]] | None:
    if len(f.tokens) == 1 and len(f.tokens[0]) < MIN_CONTAIN_SINGLE_LEN:
        return None
    total = 0.0
    weight = 0
    covered: set[int] = set()
    for tok in f.tokens:
        need = 100 if len(tok) <= EXACT_TOKEN_LEN else TOKEN_MATCH_MIN
        hit = process.extractOne(tok, unit_words, scorer=fuzz.ratio, score_cutoff=need)
        if hit is None:
            return _find_joined(f, unit_words, units)
        total += hit[1] * len(tok)
        weight += len(tok)
        covered |= units[hit[2]][1]
    return total / weight, frozenset(covered)


def _find_joined(
    f: Form, unit_words: list[str], units: list[tuple[str, frozenset[int]]]
) -> tuple[float, frozenset[int]] | None:
    """The form's words run together may be one spoken word."""
    if len(f.tokens) < 2 or len(f.joined) < 6:
        return None
    hit = process.extractOne(f.joined, unit_words, scorer=fuzz.ratio, score_cutoff=TOKEN_MATCH_MIN)
    return None if hit is None else (hit[1], units[hit[2]][1])


# ------------------------------------------------------------------------------- per ward
def _shares_word(q: NormText, f: Form) -> bool:
    """A form is a candidate only if it has a word the speech also has (or the same words run together).
    Without this, a filler phrase can score 70 against some name by chance letters."""
    wanted = {t for t in f.tokens if t.isdigit()}
    if wanted and not wanted <= {t for t in q.tokens if t.isdigit()}:
        return False  # numbers must be heard exactly: ward 2 is not ward 27
    if fuzz.ratio(q.latin_joined, f.joined) >= SHARED_WORD_MIN:
        return True
    names = [t for t in f.tokens if t not in GENERIC]
    return any(fuzz.ratio(a, b) >= SHARED_WORD_MIN for a in q.distinctive for b in names)


def _per_ward(idx: Index, scores: dict[int, float], floor: float, q: NormText) -> list[Scored]:
    by_ward: dict[int, Scored] = {}
    for i, s in scores.items():
        f = idx.forms[i]
        if not _shares_word(q, f):
            continue
        ambiguous = f.ambiguous
        for ward in f.ward_nos:
            cand = Scored(ward, round(s, 1), f.evidence_for(ward), ambiguous)
            old = by_ward.get(ward)
            if old is None or (cand.score, not cand.ambiguous) > (old.score, not old.ambiguous):
                by_ward[ward] = cand
    ranked = sorted(by_ward.values(), key=lambda c: (-c.score, c.ward_no))
    return [c for c in ranked if c.score >= floor]
