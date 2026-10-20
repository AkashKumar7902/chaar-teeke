"""Gazetteer keyword spotting on made-up frame probabilities: no speech model is loaded."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy", reason="keyword spotting needs the asr extra (uv sync --extra asr)")

# ruff: noqa: E402  (the imports below need numpy, which the skip above checks first)
from places_support import SLIVER_GEOJSON, FakeTranscriber, entry, wav_bytes

from chaarteeke.places import kws, review
from chaarteeke.places.gazetteer import Gazetteer
from chaarteeke.places.matcher import MatchResult, Scored
from chaarteeke.policy import load_policy
from chaarteeke.speech.asr import Frames

CFG = dict(load_policy()["matcher"])


# ---------------------------------------------------------------------------- helpers
def vocab_for(*texts: str) -> tuple[str, ...]:
    """A small vocabulary the way the real one is made: <unk>, every letter alone, every letter as a word start,
    and a blank last."""
    letters = sorted({c for t in texts for c in t if not c.isspace()})
    return ("<unk>", *letters, *("▁" + c for c in letters), "|")


def say(vocab: tuple[str, ...], text: str, *, blanks: bool = True, p: float = 0.9) -> np.ndarray:
    """Frames in which the model says `text` (cut into single letters, word starts marked) and nothing else."""
    pieces = [
        vocab.index(("▁" if i == 0 or text[i - 1] == " " else "") + c) for i, c in enumerate(text) if c != " "
    ]
    blank = len(vocab) - 1
    ids: list[int] = [blank]
    for piece in pieces:
        ids += [piece, blank] if blanks else [piece]
    out = np.full((len(ids), len(vocab)), np.log((1 - p) / (len(vocab) - 1)), dtype=np.float32)
    for t, i in enumerate(ids):
        out[t, i] = np.log(p)
    return out


def table_gaz(*names: tuple[str, list[int], str]) -> Gazetteer:
    """A gazetteer whose entries have a Devanagari name: (Latin name, wards, Devanagari name)."""
    entries = [entry(n, w, "landmark", name_hi=hi) for n, w, hi in names]
    return Gazetteer({"entries": entries}, SLIVER_GEOJSON, None)


# ------------------------------------------------------------------------- cutting names
def test_a_name_is_cut_into_the_fewest_pieces_first():
    vocab = ("<unk>", "▁न", "ा", "न", "क", "▁ना", "ाक", "|")
    cutter = kws.Pieces(vocab)
    cuts = cutter.cut("नाक", k=3)
    assert cuts[0] == (vocab.index("▁ना"), vocab.index("क"))  # 2 pieces; the longer first piece wins the tie
    assert (vocab.index("▁न"), vocab.index("ाक")) in cuts and len(cuts) == 3
    assert all(len(c) >= 2 for c in cuts)


def test_a_name_with_a_letter_outside_the_vocabulary_cannot_be_cut():
    cutter = kws.Pieces(("<unk>", "▁न", "ा", "|"))
    assert cutter.cut("ना7") == [] and cutter.cut("नाक") == []


def test_words_are_joined_by_the_word_start_mark():
    vocab = ("<unk>", "▁न", "▁क", "ा", "|")
    assert kws.Pieces(vocab).cut("ना क") == [(1, 3, 2)]


# ---------------------------------------------------------------------- the alignment score
def onehot(rows: list[int], vocab_size: int = 6, p: float = 0.97) -> np.ndarray:
    out = np.full((len(rows), vocab_size), np.log((1 - p) / (vocab_size - 1)), dtype=np.float32)
    for t, i in enumerate(rows):
        out[t, i] = np.log(p)
    return out


def gains_of(logp: np.ndarray) -> np.ndarray:
    return logp - logp.max(axis=1, keepdims=True)


def best(logp: np.ndarray, pieces: list[int]) -> float:
    return float(kws.best_alignment_gain(gains_of(logp), np.array([pieces]))[0])


def test_the_models_own_choice_scores_zero_and_a_wrong_piece_costs():
    # vocabulary 0..4 are pieces, 5 is the blank
    heard = onehot([5, 1, 5, 2, 5])
    assert best(heard, [1, 2]) == pytest.approx(0.0, abs=1e-6)
    assert best(heard, [1, 3]) < -3.0  # piece 3 was never likely


def test_sound_before_and_after_the_name_costs_nothing():
    heard = onehot([4, 4, 0, 5, 1, 5, 2, 5, 3, 3])
    assert best(heard, [1, 2]) == pytest.approx(0.0, abs=1e-6)


def test_a_piece_may_last_several_frames_and_blanks_may_be_missing():
    assert best(onehot([1, 1, 1, 2, 2]), [1, 2]) == pytest.approx(0.0, abs=1e-6)


def test_the_same_piece_twice_needs_a_blank_between():
    # without a blank frame the two pieces cannot touch: one frame must be given to the blank, which costs
    assert -6.0 < best(onehot([1, 1, 1]), [1, 1]) < -4.0
    assert best(onehot([1, 5, 1]), [1, 1]) == pytest.approx(0.0, abs=1e-6)


def test_more_pieces_than_frames_cannot_be_found():
    assert best(onehot([1, 2]), [1, 2, 3]) < -1e8


def test_rows_of_one_length_are_scored_together():
    heard = onehot([5, 1, 5, 2, 5])
    out = kws.best_alignment_gain(gains_of(heard), np.array([[1, 2], [2, 1], [1, 3]]))
    assert out[0] == pytest.approx(0.0, abs=1e-6) and out[1] < -3 and out[2] < -3


# ---------------------------------------------------------------------------- Urdu spellings
def test_devanagari_names_get_urdu_spellings_without_short_vowels():
    assert kws.deva_to_urdu("नगर") == ["نگر"]
    assert kws.deva_to_urdu("गांधी नगर") == ["گاندھی نگر"]
    assert "بخشی" in kws.deva_to_urdu("बख्शी")  # the second spelling of ख
    spellings = kws.deva_to_urdu("गुज्जर")
    assert spellings[0] == "گجر"  # a doubled letter is written once; the plain reading comes first
    assert "گوجر" in spellings and "گزر" in spellings  # the short u written as و, and ج as ز


def test_a_letter_with_no_rule_gives_no_spelling():
    assert kws.deva_to_urdu("नगर 7") == []


# ------------------------------------------------------------------------------ the search
NAMES = (
    ("Bakshi Nagar", [1], "बक्शी नगर"),
    ("Bakshi Road", [2], "बक्शी रोड"),
    ("Gandhi Nagar", [1, 2], "गांधी नगर"),
)


@pytest.fixture
def searcher():
    gaz = table_gaz(*NAMES)
    vocab = vocab_for(*(n[2] for n in NAMES), "मंदिर")
    return kws.KeywordSpotter(gaz), vocab


def frames_for(vocab: tuple[str, ...], text: str, lang: str = "hi") -> Frames:
    return Frames(say(vocab, text), vocab, lang)


def test_the_spoken_name_scores_best_and_its_ward_comes_first(searcher):
    sp, vocab = searcher
    r = sp.search(frames_for(vocab, "बक्शी नगर"), 5.0)
    assert r.hits[0].ward_no == 1 and r.hits[0].score > 80
    assert r.hits[0].evidence == "Bakshi Nagar"
    assert [h.ward_no for h in r.hits] == sorted(
        {h.ward_no for h in r.hits}, key=lambda w: -next(h.score for h in r.hits if h.ward_no == w)
    )


def test_a_name_inside_a_longer_spoken_sentence_is_found(searcher):
    sp, vocab = searcher
    r = sp.search(frames_for(vocab, "बक्शी नगर"), 5.0)
    longer = Frames(np.vstack([say(vocab, "गांधी"), r.hits and say(vocab, "बक्शी नगर")]), vocab, "hi")
    assert sp.search(longer, 5.0).hits[0].ward_no == 1


def test_a_form_that_is_part_of_a_longer_scored_form_loses_points(searcher):
    sp, vocab = searcher
    bare = sp.search(frames_for(vocab, "बक्शी नगर"), 0.0)
    nagar = next(h for h in bare.hits if h.form == "बक्शी नगर")
    # "बक्शी" alone is not a form here, but "गांधी नगर" and "बक्शी नगर" share the word "नगर" (not a run of words)
    assert nagar.score > 80
    gaz = table_gaz(("Bakshi", [3], "बक्शी"), ("Bakshi Nagar", [1], "बक्शी नगर"))
    sp2 = kws.KeywordSpotter(gaz)
    r = sp2.search(frames_for(vocab, "बक्शी नगर"), 0.0)
    by = {h.ward_no: h.score for h in r.hits}
    assert by[1] > by[3] + 15  # the short name is outranked by the long one that scored as well


def test_a_name_made_of_a_place_type_only_is_not_searched():
    gaz = table_gaz(("Temple", [1], "मंदिर"), ("Bakshi Nagar", [2], "बक्शी नगर"))
    sp = kws.KeywordSpotter(gaz)
    vocab = vocab_for("मंदिर", "बक्शी नगर")
    r = sp.search(Frames(say(vocab, "मंदिर"), vocab, "hi"), 0.0)
    assert 1 not in r.ward_nos


def test_a_form_with_a_digit_is_skipped_and_counted():
    gaz = table_gaz(("Ward 7", [1], "वर्द 7"), ("Bakshi Nagar", [2], "बक्शी नगर"))
    sp = kws.KeywordSpotter(gaz)
    vocab = vocab_for("वर्द बक्शी नगर")
    r = sp.search(Frames(say(vocab, "बक्शी नगर"), vocab, "hi"), 0.0)
    assert r.skipped == 1 and r.searched == 1


def test_nothing_like_any_name_scores_below_the_floor(searcher):
    sp, vocab = searcher
    r = sp.search(Frames(say(vocab, "मंदिर"), vocab, "hi"), 5.0)
    assert r.hits == []


def test_the_table_follows_the_language_and_is_built_once(searcher):
    sp, vocab = searcher
    a = sp.table(frames_for(vocab, "बक्शी नगर", "hi"))
    assert sp.table(frames_for(vocab, "बक्शी नगर", "hi")) is a
    assert sp.table(frames_for(vocab, "बक्शी नगर", "doi")) is not a


def test_a_language_with_no_spotting_is_an_error(searcher):
    sp, vocab = searcher
    with pytest.raises(ValueError, match="'pa'"):
        sp.search(Frames(say(vocab, "बक्शी नगर"), vocab, "pa"), 5.0)


def test_urdu_forms_come_from_the_urdu_names_and_the_devanagari_ones():
    gaz = Gazetteer(
        {"entries": [entry("Bakshi Nagar", [1], name_hi="बक्शी नगर"), entry("Jammu", [2], name_ur="جموں")]},
        SLIVER_GEOJSON,
        None,
    )
    forms = {f.text for f in kws._spoken_forms(gaz.written_forms(), "arabic")}
    assert "جموں" in forms and any("نگر" in f for f in forms)
    assert not any("बक्शी" in f for f in forms)


def test_a_learned_name_is_searched_too(ctx):
    db = ctx.db
    gaz = Gazetteer({"entries": [entry("Bakshi Nagar", [1], name_hi="बक्शी नगर")]}, SLIVER_GEOJSON, db)
    sp = kws.KeywordSpotter(gaz)
    vocab = vocab_for("बक्शी नगर", "टोला मोड़")
    before = sp.search(Frames(say(vocab, "टोला मोड़"), vocab, "hi"), 5.0)
    assert before.hits == []
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO aliases(text_norm, ward_no, added_by, added_at) VALUES ('टोला मोड़', 2, 'staff:rev', 'x')"
        )
    after = sp.search(Frames(say(vocab, "टोला मोड़"), vocab, "hi"), 5.0)
    assert after.hits and after.hits[0].ward_no == 2


# ----------------------------------------------------------------------------- combining
def hit(ward, score, *, ambiguous=False, pieces=6, evidence="X"):
    return kws.Hit(ward, score, evidence, ambiguous, "form", pieces)


def result(*hits):
    return kws.KwsResult(sorted(hits, key=lambda h: -h.score), 100, 0)


def text_of(confidence, *wards, score=90.0):
    return (
        MatchResult(confidence, [Scored(w, score, "T", False) for w in wards], "x")
        if wards
        else MatchResult("none")
    )


def test_a_sure_keyword_hit_places_the_ward_when_the_transcript_found_nothing():
    r = kws.combine(MatchResult("none"), result(hit(27, 80.0), hit(14, 8.0)), CFG, "")
    assert r.confidence == "likely" and r.ward_nos[0] == 27


def test_a_weak_keyword_hit_is_only_offered():
    r = kws.combine(MatchResult("none"), result(hit(27, CFG["kws_likely_min"] - 1)), CFG, "")
    assert r.confidence == "possible" and r.ward_nos == [27]


def test_a_keyword_hit_without_a_clear_lead_is_only_offered():
    r = kws.combine(
        MatchResult("none"), result(hit(27, 80.0), hit(14, 80.0 - CFG["kws_likely_gap"] + 1)), CFG, ""
    )
    assert r.confidence == "possible" and set(r.ward_nos) == {27, 14}


def test_a_name_that_means_several_wards_is_offered_not_placed():
    r = kws.combine(
        MatchResult("none"), result(hit(20, 99.0, ambiguous=True), hit(21, 99.0, ambiguous=True)), CFG, ""
    )
    assert r.confidence == "possible" and set(r.ward_nos) == {20, 21}


def test_a_short_form_is_not_sure_by_itself():
    short = hit(27, 95.0, pieces=CFG["kws_sure_min_pieces"] - 1)
    assert kws.combine(MatchResult("none"), result(short), CFG, "").confidence == "possible"
    assert (
        kws.combine(
            MatchResult("none"), result(hit(27, 95.0, pieces=CFG["kws_sure_min_pieces"])), CFG, ""
        ).confidence
        == "likely"
    )


def test_the_transcript_alone_still_places_a_ward():
    r = kws.combine(text_of("likely", 27, score=95.0), result(), CFG, "bakshi nagar")
    assert r.confidence == "likely" and r.ward_nos == [27]


def test_two_sources_naming_different_wards_leave_it_to_the_reviewer():
    r = kws.combine(text_of("likely", 27), result(hit(14, 90.0)), CFG, "")
    assert r.confidence == "possible" and set(r.ward_nos) == {27, 14}


def test_a_weak_keyword_hit_elsewhere_does_not_block_a_sure_transcript():
    r = kws.combine(text_of("likely", 27), result(hit(14, CFG["kws_likely_min"] - 5)), CFG, "")
    assert r.confidence == "likely" and r.ward_nos[0] == 27


def test_both_sources_agreeing_rank_that_ward_first_and_place_it():
    r = kws.combine(text_of("possible", 27, 14, score=75.0), result(hit(27, 70.0, ambiguous=True)), CFG, "")
    assert r.ward_nos[0] == 27 and r.confidence == "possible"  # neither is sure on its own
    r2 = kws.combine(text_of("likely", 27), result(hit(27, 70.0)), CFG, "")
    assert r2.confidence == "likely" and r2.candidates[0].score > 85  # better than either source's own score


def test_a_known_outside_place_is_never_placed():
    r = kws.combine(MatchResult("none"), result(hit(55, 99.0)), CFG, "दिल्ली")
    assert r.confidence == "possible"


def test_at_most_max_candidates_are_offered_best_first():
    hits = [hit(w, 60.0 - w) for w in range(1, 7)]
    r = kws.combine(MatchResult("none"), result(*hits), CFG, "")
    assert r.ward_nos == [1, 2, 3] and len(r.candidates) == CFG["max_candidates"]


def test_nothing_from_either_source_is_none():
    assert kws.combine(MatchResult("none"), result(), CFG, "").confidence == "none"


def test_hits_below_the_floor_are_ignored():
    r = kws.combine(MatchResult("none"), result(hit(27, CFG["kws_possible_min"] - 1)), CFG, "")
    assert r.confidence == "none"


# --------------------------------------------------------------------- inside process_capture
def real_vocab(gaz) -> tuple[str, ...]:
    forms = [f.text for f in gaz.written_forms() if not f.text.isascii()]
    return vocab_for(*forms)


def test_the_app_places_a_name_the_transcript_got_wrong(pctx, world):
    """The transcript is nonsense, but the frames say Bakshi Nagar: the keyword spotter places ward 27."""
    vocab = real_vocab(pctx.gazetteer)
    frames = Frames(say(vocab, "बख्शी नगर"), vocab, "hi")
    pctx.transcriber = FakeTranscriber(text="ऊ ऊ ऊ", frames=frames)
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    row = world.row(rid)
    assert (row["location_status"], row["ward_no"]) == ("matched", 27)
    assert pctx.db.one("SELECT audit.detail_json FROM audit WHERE action = 'review.matched'") is not None


def test_a_transcriber_without_frames_gets_the_transcript_matcher_only(pctx, world):
    pctx.transcriber = FakeTranscriber(text="ऊ ऊ ऊ")
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    row = world.row(rid)
    assert row["location_status"] == "needs_review" and row["asr_status"] == "done"


def test_frames_of_speech_with_no_place_leave_it_to_the_reviewer(pctx, world):
    vocab = real_vocab(pctx.gazetteer)
    frames = Frames(say(vocab, "कुत्ता सफेद था"), vocab, "hi")
    pctx.transcriber = FakeTranscriber(text="कुत्ता सफेद था", frames=frames)
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    assert world.row(rid)["location_status"] == "needs_review"


def test_a_failure_of_the_keyword_search_is_shown_and_places_nothing(pctx, world, monkeypatch):
    vocab = real_vocab(pctx.gazetteer)
    pctx.transcriber = FakeTranscriber(text="बख्शी नगर", frames=Frames(say(vocab, "बख्शी नगर"), vocab, "hi"))

    def boom(*a, **k):
        raise RuntimeError("table broke")

    monkeypatch.setattr(kws.KeywordSpotter, "search", boom)
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    row = world.row(rid)
    assert row["asr_status"] == "failed" and "table broke" in row["asr_error"]
    assert row["location_status"] == "needs_review" and row["ward_no"] is None


def test_importing_the_module_loads_no_heavy_library():
    import subprocess
    import sys

    from chaarteeke.config import ROOT

    code = "import sys, chaarteeke.places.kws; print([m for m in ('torch','onnxruntime','numpy','scipy') if m in sys.modules])"
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"
