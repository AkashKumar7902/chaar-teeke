"""Folding: one spelling for the many ways a place is written, said or transcribed."""

from __future__ import annotations

import pytest
from rapidfuzz import fuzz

from chaarteeke.places.normalise import alias_text, normalise, phonetic_word, raw_words

SAME = [
    ("Gandhi Nagar", "गांधी नगर"),
    ("Gandhi Nagar", "गांधीनगर".replace("नगर", " नगर")),
    ("Gandhi Nagar", "Gandi Nagr"),
    ("Bakshi Nagar", "बख्शी नगर"),
    ("Bakshi Nagar", "Baxi Nagar"),
    ("Rehari Colony", "रेहाड़ी कॉलोनी"),
    ("Rehari Colony", "Rehari Colni"),
    ("Talab Tillo", "तालाब तिल्लो"),
    ("Talab Tillo", "Tallab Tilo"),
    ("Kachi Chawni", "कच्ची छावनी"),
    ("Kachi Chawni", "Kacchi Chhawni"),
    ("Bhagwati Nagar", "भगवती नगर"),
    ("Chowk", "चौक"),
    ("Chowk", "Chauk"),
    ("Shastri Nagar", "शास्त्री नगर"),
    ("Pacca Danga", "पक्का डंगा"),
    ("Amphalla", "अम्फला"),
]


@pytest.mark.parametrize(("a", "b"), SAME)
def test_the_same_place_in_two_spellings_folds_to_a_near_spelling_and_one_skeleton(a, b):
    """Devanagari keeps some short vowels the Latin spelling leaves out (bagavati, bagvati), so the folds are
    close, not always equal; the consonant skeletons agree."""
    x, y = normalise(a), normalise(b)
    assert fuzz.ratio(x.latin_joined, y.latin_joined) >= 85, (x.latin, y.latin)
    assert fuzz.ratio(x.phonetic.replace(" ", ""), y.phonetic.replace(" ", "")) >= 85, (
        x.phonetic,
        y.phonetic,
    )


@pytest.mark.parametrize("a", ["Gandhi Nagar", "Bakshi Nagar", "Talab Tillo", "Rehari Colony"])
def test_latin_respellings_fold_to_exactly_one_spelling(a):
    assert normalise(a).latin == normalise(a.upper()).latin == normalise(a.replace("a", "aa", 1)).latin


def test_urdu_script_has_no_short_vowels_so_it_meets_the_name_at_the_consonants():
    urdu = normalise("گاندھی نگر")
    assert urdu.phonetic == normalise("Gandhi Nagar").phonetic
    assert urdu.latin != normalise("Gandhi Nagar").latin


def test_run_together_and_apart_differ_in_text_but_agree_without_spaces():
    a, b = normalise("गांधीनगर"), normalise("गांधी नगर")
    assert a.latin != b.latin
    assert a.latin_joined == b.latin_joined


def test_different_places_fold_differently():
    assert normalise("Bakshi Nagar").latin != normalise("Gandhi Nagar").latin
    assert normalise("Muthi").latin != normalise("Mehta").latin


def test_phonetic_key_is_the_consonant_skeleton():
    assert normalise("Rehari").phonetic == normalise("Rihari").phonetic
    assert phonetic_word("amfala") == "amfl"
    assert normalise("Gandhi Nagar").phonetic == "gnd ngr"


def test_the_fold_is_not_applied_twice_by_accident():
    # The fold is lossy ("ch" is kept apart from "c"), so a folded string is not a spelling to fold again.
    once = normalise("Chawni").latin
    assert once == "cavni"
    assert normalise(once).latin != once  # which is why aliases keep the heard words, not the fold


def test_a_sentence_keeps_its_words_and_marks_which_are_distinctive():
    n = normalise("मुझे गांधी नगर में कुत्ते ने काटा")
    assert "gandi" in n.tokens and "nagar" in n.tokens
    assert n.distinctive == ("gandi",)


def test_only_place_types_and_filler_has_no_distinctive_word():
    for text in ("bus stand", "near the school", "ghar ke paas", "Nagar", "बस स्टैंड", ""):
        assert not normalise(text).distinctive, text


def test_digits_are_words():
    assert normalise("Ward 27").tokens == ("vard", "27")
    assert normalise("Ward 27").distinctive == ("27",)


@pytest.mark.parametrize("text", ["", "   ", "!!! ??? ...", "​", "12", "a", "ा"])
def test_odd_input_never_raises(text):
    normalise(text)


def test_raw_words_are_lowercase_and_unfolded():
    assert raw_words("Delhi, में था!") == frozenset({"delhi", "में", "था"})


def test_alias_text_keeps_the_name_and_drops_filler():
    assert alias_text("मुझे बख्शी नगर में कुत्ते ने काटा") == "बख्शी नगर"
    assert alias_text("I was near Bakshi Nagar") == "bakshi nagar"


def test_alias_text_is_empty_when_nothing_but_filler_was_said():
    assert alias_text("main near the") == ""
    assert alias_text("") == ""


def test_alias_text_is_short():
    assert len(alias_text("alpha beta gamma delta epsilon zeta eta theta").split()) <= 6


def test_digits_stuck_to_a_word_are_split_off_so_numbers_stay_exact():
    assert normalise("Tawi Bridge3").tokens == normalise("Tawi Bridge 3").tokens
    assert normalise("bridge3").tokens[-1] == "3"
