"""The evaluation script's own logic (the numbers in docs/EVALS.md come from it)."""

from __future__ import annotations

import importlib.util
import sys

import pytest

from chaarteeke.config import ROOT
from chaarteeke.places.matcher import MatchResult, Scored


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location(
        "eval_matcher_under_test", ROOT / "scripts" / "eval_matcher.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def result(confidence, *wards):
    return MatchResult(confidence, [Scored(w, 90.0, "x", False) for w in wards])


def test_outcomes_are_classified_against_the_expected_wards(ev):
    assert ev.outcome(result("likely", 27), {27}) == "placed_right"
    assert ev.outcome(result("likely", 27), {20, 21}) == "placed_wrong"
    assert ev.outcome(result("possible", 20, 21), {21}) == "review_right"
    assert ev.outcome(result("possible", 20, 21), {5}) == "review_wrong"
    assert ev.outcome(result("none"), {5}) == "nothing"
    assert ev.outcome(result("likely", 27), set()) == "placed_wrong"  # a place that is not in the data


def test_tally_percentages(ev):
    t = ev.Tally()
    for label in ("placed_right", "placed_right", "nothing", "placed_wrong"):
        t.add(label)
    assert t.n == 4 and t.pct("placed_right") == "50.0%" and t.pct("review_right") == "0.0%"
    assert ev.Tally().pct("nothing") == "-"


def test_variants_are_deterministic_and_never_repeat_the_name(ev):
    e = {"name": "Gandhi Nagar", "name_hi": "गांधी नगर", "name_hi_source": "dictionary"}
    a, b = ev.variants_of(e), ev.variants_of(e)
    assert a == b
    texts = [t for _, t in a]
    assert len(texts) == len(set(texts))
    kinds = {k for k, _ in a}
    assert {"name as written", "respelled", "in a sentence", "Devanagari (hand word list)"} <= kinds
    assert "gandi nagar" in texts  # aspirate h dropped


def test_every_truth_ward_exists(ev, gaz):
    for name, wards in ev.TRUTH.items():
        assert all(gaz.has_ward(w) for w in wards), name
    for text, wards in ev.HAND_TYPED:
        assert all(gaz.has_ward(w) for w in wards), text


def test_the_hand_typed_names_reach_the_wards_they_claim(ev, gaz):
    from chaarteeke.places.matcher import match
    from chaarteeke.policy import load_policy

    for text, wards in ev.HAND_TYPED:
        r = match(gaz, text, load_policy())
        assert set(r.ward_nos) & wards, text
        if r.confidence == "likely":
            assert r.candidates[0].ward_no in wards, text


def test_no_non_place_is_placed_by_itself(ev, gaz):
    from chaarteeke.policy import load_policy

    tally, placed = ev.eval_not_places(gaz, load_policy())
    assert placed == [] and tally.counts["placed_wrong"] == 0


def test_the_typed_evaluation_never_places_a_wrong_ward(ev, gaz):
    from chaarteeke.policy import load_policy

    tallies, _ = ev.eval_typed(gaz, load_policy())
    wrong = {
        (t, k): v.counts["placed_wrong"]
        for t, d in tallies.items()
        for k, v in d.items()
        if v.counts["placed_wrong"]
    }
    assert wrong == {}


def test_name_heard_and_error_rate_ignore_spaces_and_vowel_length(ev):
    assert ev._name_heard("गांधी नगर", "गांधीनगर")
    assert ev._name_heard("रेहाड़ी कॉलोनी", "रेहाडी कॉलोनी")
    assert not ev._name_heard("गांधी नगर", "बख्शी नगर")
    assert ev._cer("गांधी नगर", "गांधीनगर") == 0
    assert 0 < ev._cer("गांधी नगर", "गांधी") <= 1
