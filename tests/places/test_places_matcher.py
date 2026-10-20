"""The matcher: which ward, how sure, and what is offered instead of a guess."""

from __future__ import annotations

import copy

import pytest
from places_support import SLIVER_GEOJSON, entry

from chaarteeke.places.gazetteer import Gazetteer
from chaarteeke.places.matcher import match
from chaarteeke.policy import Policy, load_policy

POLICY = load_policy()


def ask(gaz, text, policy=POLICY):
    return match(gaz, text, policy)


# ----------------------------------------------------------------- placed by itself
@pytest.mark.parametrize(
    ("text", "ward"),
    [
        ("Bakshi Nagar", 27),
        ("bakshi nagar", 27),
        ("Baxi Nagr", 27),
        ("बख्शी नगर", 27),
        ("बखशीनगर", 27),
        ("मुझे बख्शी नगर में कुत्ते ने काटा", 27),
        ("I was near Bakshi Nagar bus stand", 27),
        ("main bakshi nagar mein tha", 27),
        ("Gujjar Nagar", 6),
        ("गुजर नगर", 6),
        ("Panjtirthi", 1),
        ("Amphalla", 17),
        ("अम्फला", 17),
        ("Dogra Hall", 8),
        ("Sunjwan", 74),
        ("Bahu Fort", 48),
        ("Pacca Danga", 10),
        ("Gandhi Nagar North", 20),
        ("ward 27", 27),
        ("वार्ड 27", 27),
    ],
)
def test_a_clear_name_is_placed_by_itself(gaz, text, ward):
    r = ask(gaz, text)
    assert r.confidence == "likely", (text, r)
    assert r.candidates[0].ward_no == ward


def test_the_evidence_is_the_gazetteer_name_that_matched(gaz):
    assert ask(gaz, "baxi nagar").candidates[0].evidence == "Bakshi Nagar"


# --------------------------------------------------------- ambiguity: a list, never a pick
@pytest.mark.parametrize(
    ("text", "wards"),
    [
        ("Gandhi Nagar", {20, 21}),
        ("गांधी नगर", {20, 21}),
        ("Rehari Colony", {24, 25}),
        ("Rehri Colony", {24, 25}),
        ("Talab Tillo", {30, 31}),
        ("Trikuta Nagar", {53, 54}),
        ("Janipura", {34, 35, 36}),
        ("Muthi", {66, 67}),
        ("Nanak Nagar", {42, 43, 44}),
        ("Sainik Colony", {69, 70}),
    ],
)
def test_a_name_shared_by_wards_offers_them_and_is_not_placed(gaz, text, wards):
    r = ask(gaz, text)
    assert r.confidence == "possible"
    assert wards <= set(r.ward_nos)
    assert r.candidates[0].ambiguous


def test_at_most_max_candidates_are_offered(gaz):
    r = ask(gaz, "Janipura")
    assert len(r.candidates) == POLICY["matcher"]["max_candidates"]


def test_a_close_second_ward_stops_auto_placement():
    """Two different names that score within the gap are a review, not a coin toss."""
    g = Gazetteer(
        {"entries": [entry("Alpha Park", [1], "osm_place"), entry("Alpha Parka", [2], "osm_place")]},
        SLIVER_GEOJSON,
    )
    r = ask(g, "alpha park")
    assert r.confidence == "possible"
    assert set(r.ward_nos) == {1, 2}


def test_one_ward_name_alone_is_placed_when_the_runner_up_is_far():
    g = Gazetteer(
        {"entries": [entry("Alpha Park", [1], "osm_place"), entry("Zebra Lines", [2], "osm_place")]},
        SLIVER_GEOJSON,
    )
    r = ask(g, "alpha park")
    assert r.confidence == "likely" and r.ward_nos == [1]


def test_a_place_in_an_overlap_sliver_is_offered_for_both_wards_not_placed():
    g = Gazetteer({"entries": [entry("Sliver Temple", [1], lat=0.5, lon=1.5)]}, SLIVER_GEOJSON)
    r = ask(g, "sliver temple")
    assert r.confidence == "possible"
    assert set(r.ward_nos) == {1, 2}


def test_a_ward_that_is_only_a_guess_from_a_pincode_is_never_placed():
    g = Gazetteer(
        {"entries": [entry("Lonely Post Office", [3], "post_office", ward_confidence="low (point)")]},
        SLIVER_GEOJSON,
    )
    r = ask(g, "lonely post office")
    assert r.confidence == "possible" and r.ward_nos == [3]


# ------------------------------------------------------------------------------ nothing
@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "kutte ne kata",
        "ghar ke paas",
        "near the school",
        "bus stand",
        "pata nahi",
        "namaste",
        "हैलो",
        "मुझे नहीं पता",
        "xyzzy plugh",
    ],
)
def test_something_that_is_not_a_place_is_never_placed(gaz, text):
    r = ask(gaz, text)
    assert r.confidence != "likely"
    if r.confidence == "none":
        assert r.candidates == []


def test_filler_and_place_types_alone_give_none(gaz):
    for text in ("bus stand", "ghar ke paas", "near the school", ""):
        assert ask(gaz, text).confidence == "none", text


@pytest.mark.parametrize("text", ["Delhi", "दिल्ली", "delhi mein"])
def test_delhi_sounds_like_ward_55_but_is_never_placed_by_itself(gaz, text):
    assert ask(gaz, text).confidence != "likely"


@pytest.mark.parametrize("text", ["Srinagar", "Kathua", "Udhampur", "Pathankot", "Amritsar", "Mumbai"])
def test_other_towns_are_never_placed_by_itself(gaz, text):
    assert ask(gaz, text).confidence != "likely"


# ---------------------------------------------------------------- thresholds are policy
def _policy(**matcher) -> Policy:
    raw = copy.deepcopy(POLICY.raw)
    raw["matcher"].update(matcher)
    return Policy(raw, "test")


def test_likely_min_comes_from_policy(gaz):
    assert ask(gaz, "Bakshi Nagar").confidence == "likely"
    assert ask(gaz, "Bakshi Nagar", _policy(likely_min=101)).confidence == "possible"


def test_likely_gap_comes_from_policy(gaz):
    assert ask(gaz, "Gandhi Nagar North").confidence == "likely"
    assert ask(gaz, "Gandhi Nagar North", _policy(likely_gap=50)).confidence == "possible"


def test_possible_min_comes_from_policy(gaz):
    assert ask(gaz, "Bakshi Nagrit").confidence == "likely"  # scores 90
    assert ask(gaz, "Bakshi Nagrit", _policy(possible_min=99)).confidence == "none"


def test_max_candidates_comes_from_policy(gaz):
    assert len(ask(gaz, "Janipura", _policy(max_candidates=1)).candidates) == 1


# ------------------------------------------------- what speech models really returned
ASR_HEARD = [
    ("बखशीनगर", 27),  # IndicConformer on a Hindi machine voice, "Bakshi Nagar"
    ("गुजर नगर", 6),  # "Gujjar Nagar"
    ("भगवती नगर", 14),
    ("पक्का डंगा", 10),
    ("अमफला", 17),  # "Amphalla"
    ("मुझे बखशीनगर में कुत्ते ने काटा", 27),
]


@pytest.mark.parametrize(("heard", "ward"), ASR_HEARD)
def test_transcripts_saved_from_the_speech_evaluation_are_placed_right(gaz, heard, ward):
    r = ask(gaz, heard)
    assert r.confidence == "likely" and r.ward_nos[0] == ward


def test_a_garbled_shared_name_still_offers_the_right_wards(gaz):
    r = ask(gaz, "रिहारी कॉलोनी")  # "Rehari Colony" as heard
    assert r.confidence == "possible" and {24, 25} <= set(r.ward_nos)


# ----------------------------------------------------------------- learned names
def test_a_taught_name_is_matched_like_any_other(ctx):
    g = Gazetteer({"entries": [entry("Alpha Park", [1], "osm_place")]}, SLIVER_GEOJSON, ctx.db)
    assert ask(g, "sabzi mandi wala chowk").confidence == "none"
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO aliases(text_norm, ward_no, added_by, added_at) VALUES ('sabzi mandi wala chowk', 3, 'staff:rev', 'x')"
        )
    r = ask(g, "main sabzi mandi wala chowk ke paas tha")
    assert r.confidence == "likely" and r.ward_nos == [3]
    assert r.candidates[0].evidence == "sabzi mandi wala chowk"


def test_a_name_taught_for_two_wards_is_ambiguous(ctx):
    g = Gazetteer({"entries": [entry("Alpha Park", [1], "osm_place")]}, SLIVER_GEOJSON, ctx.db)
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO aliases(text_norm, ward_no, added_by, added_at) VALUES ('kali mata', 1, 'a', 'x')"
        )
        conn.execute(
            "INSERT INTO aliases(text_norm, ward_no, added_by, added_at) VALUES ('kali mata', 2, 'a', 'x')"
        )
    r = ask(g, "kali mata")
    assert r.confidence == "possible" and set(r.ward_nos) == {1, 2}


def test_matching_is_fast_enough_for_a_call(gaz):
    import time

    ask(gaz, "warm up")
    t = time.perf_counter()
    for _ in range(20):
        ask(gaz, "मुझे गांधी नगर में कुत्ते ने काटा")
    assert (time.perf_counter() - t) / 20 < 0.25
