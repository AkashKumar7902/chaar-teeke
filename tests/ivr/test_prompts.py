"""The prompt catalogue: segments, composition order, what is missing, the manifest."""

from __future__ import annotations

from datetime import date

import pytest
from ivr_support import CLINIC, REAL_PROMPTS

from chaarteeke.ivr.flow import REQUIRED_PROMPTS
from chaarteeke.ivr.prompts import (
    KNOWN_SLOTS,
    ClinicSetupError,
    MissingAudio,
    MissingSlot,
    PromptCatalogue,
    PromptError,
    UnknownPrompt,
    hhmm,
    required_extent,
    visit_time,
)

CLOSE = {"n": 2, "total": 4, "date": date(2026, 10, 20), "time": "09:00", "clinic": CLINIC}


def test_real_prompts_load_and_every_prompt_the_flows_play_is_defined():
    cat = PromptCatalogue(REAL_PROMPTS, "/nonexistent")
    assert len(cat.ids()) >= 47
    assert "FC_NOTICE_FULL" in cat.ids()
    gone = {"FC_DOSE", "FC_CLOSE_NODATE", "FC_CHILD", "FC_GUARD_NOTICE", "FC_GUARD_PENDING"}
    assert gone.isdisjoint(cat.ids())  # nothing plays them any more, and a prompt nobody plays is deleted
    # The flows ask for questions consent with FC_QCONSENT; the voice lane adds that line. If this fails,
    # the voice lane changed ids: line the flows up with them.
    assert set(cat.undefined_required()) <= {"FC_QCONSENT"}
    assert set(cat.undefined_required()) | set(cat.ids()) >= REQUIRED_PROMPTS


# ----------------------------------------------------------------- the layered notice
def _prompt(prompt_id: str) -> dict:
    import json
    from pathlib import Path

    return next(
        p
        for p in json.loads(Path(REAL_PROMPTS).read_text(encoding="utf-8"))["prompts"]
        if p["id"] == prompt_id
    )


def test_the_short_notice_is_short_and_names_every_item_the_full_notice_adds_the_rest():
    short, full = _prompt("FC_NOTICE")["text"]["en"].lower(), _prompt("FC_NOTICE_FULL")["text"]["en"].lower()
    assert len(short.split()) <= 56  # about 50 words, about 20 seconds spoken
    for item in (
        "before each injection",  # the call before each injection
        "7 days after the last",  # when the number is deleted
        "where the dog bit",  # the place question
        "your voice is recorded",  # the recording
        "deleted once it is found",  # and when the recording goes
        "kept apart from your number",  # answers are kept apart
        "press 9 at any time to stop",  # withdrawal at any time
        "press 0 for the full notice",  # the way to the full notice
    ):
        assert item in short, item
    assert (
        len(full.split()) > 100
    )  # the long form: the purpose, the register, the call-back and the complaint
    for item in ("data protection board of india", "tell the clinic first", "register", "sick"):
        assert item in full and item not in short, item


@pytest.mark.parametrize("lang", ["hi", "doi", "ur"])
def test_both_notices_exist_in_each_call_language_with_the_keys_and_the_full_one_is_longer(lang):
    short, full = _prompt("FC_NOTICE")["text"][lang], _prompt("FC_NOTICE_FULL")["text"][lang]
    assert "9" in short and "0" in short  # stop at any time, the full notice
    assert "9" in full and len(full) > 2 * len(short)


@pytest.mark.parametrize("prompt_id", ["FC_NOTICE", "FC_NOTICE_FULL", "FC_PLACE", "ENR_DUP"])
def test_a_changed_line_is_flagged_as_a_machine_draft_in_every_language_but_english(prompt_id):
    status = _prompt(prompt_id)["review_status"]
    assert status["en"] == "source"
    assert {lang: status[lang] for lang in ("hi", "doi", "ur")} == dict.fromkeys(
        ("hi", "doi", "ur"), "machine_draft_needs_native_review"
    )


def test_the_trimmed_place_and_already_signed_up_lines_keep_what_they_must_say():
    place, dup = _prompt("FC_PLACE")["text"], _prompt("ENR_DUP")["text"]
    assert "your voice is recorded" in place["en"].lower() and "beep" in place["en"]
    for lang, word in (("hi", "रिकॉर्ड"), ("doi", "रिकॉर्ड"), ("ur", "ریکارڈ")):
        assert word in place[lang], lang
    assert "press 9 now" in dup["en"].lower() or "press 9 now" in dup["en"]
    assert all("9" in dup[lang] for lang in ("hi", "doi", "ur", "en"))


def test_every_text_in_every_language_uses_only_known_slots():
    cat = PromptCatalogue(REAL_PROMPTS, "/nonexistent")
    for pid in cat.ids():
        for lang in ("hi", "doi", "ur", "en"):
            assert set(cat.slots_of(pid, lang)) <= set(KNOWN_SLOTS) | {"clinic_phone"}


def test_fc_close_hindi_follows_hindi_word_order(catalogue):
    """total comes before n in Hindi ("kul {total} mein se teeka number {n}"); the date is weekday, day, month."""
    assert catalogue.render("hi", "FC_CLOSE", CLOSE) == [
        "chaar/hi/FC_CLOSE.0",
        "chaar/hi/slot/num_4",
        "chaar/hi/FC_CLOSE.1",
        "chaar/hi/slot/num_2",
        "chaar/hi/slot/weekday_1",
        "chaar/hi/slot/day_20",
        "chaar/hi/slot/month_10",
        "chaar/hi/FC_CLOSE.2",
        "chaar/hi/slot/time_0900",
        "chaar/hi/slot/clinic_cln_a",
        "chaar/hi/FC_CLOSE.3",
    ]


def test_fc_close_english_follows_english_word_order(catalogue):
    assert catalogue.render("en", "FC_CLOSE", CLOSE) == [
        "chaar/en/FC_CLOSE.0",
        "chaar/en/slot/num_2",
        "chaar/en/FC_CLOSE.1",
        "chaar/en/slot/num_4",
        "chaar/en/FC_CLOSE.2",
        "chaar/en/slot/weekday_1",
        "chaar/en/slot/day_20",
        "chaar/en/slot/month_10",
        "chaar/en/FC_CLOSE.3",
        "chaar/en/slot/time_0900",
        "chaar/en/FC_CLOSE.4",
        "chaar/en/slot/clinic_cln_a",
        "chaar/en/FC_CLOSE.5",
    ]


def test_blank_literal_segments_are_not_recorded_and_numbering_has_no_gaps(catalogue):
    files = [i.path for i in catalogue.manifest("hi") if i.prompt_id == "FC_CLOSE"]
    assert files == [f"hi/FC_CLOSE.{k}.wav" for k in range(4)]


def test_a_prompt_without_slots_is_one_file(catalogue):
    assert catalogue.render("doi", "SYS_BYE") == ["chaar/doi/SYS_BYE"]
    assert catalogue.media_name("doi", "SYS_BYE") == "chaar/doi/SYS_BYE"
    assert catalogue.url("doi", "SYS_BYE") == "/media/prompts/doi/SYS_BYE.wav"
    assert catalogue.has_audio("doi", "SYS_BYE")
    with pytest.raises(ValueError):
        catalogue.media_name("hi", "FC_CLOSE")


def test_dates_use_monday_zero_and_numbers_are_read_digit_by_digit(catalogue):
    assert catalogue.say_date("ur", date(2026, 10, 19)) == [
        "chaar/ur/slot/weekday_0",
        "chaar/ur/slot/day_19",
        "chaar/ur/slot/month_10",
    ]
    assert catalogue.say_number("hi", 4) == ["chaar/hi/slot/num_4"]
    assert catalogue.say_number("hi", "941") == [
        "chaar/hi/slot/num_9",
        "chaar/hi/slot/num_4",
        "chaar/hi/slot/num_1",
    ]
    with pytest.raises(MissingSlot):
        catalogue.say_number("hi", "x1")


def test_vaccine_out_reads_the_clinic_phone_digit_by_digit(catalogue):
    media = catalogue.render("en", "RM_EVE_BODY_OUT", {**CLOSE, "clinic_phone": "019-12"})
    tail = [m for m in media if "num_" in m][2:]  # after n and total
    assert tail == [
        "chaar/en/slot/num_0",
        "chaar/en/slot/num_1",
        "chaar/en/slot/num_9",
        "chaar/en/slot/num_1",
        "chaar/en/slot/num_2",
    ]


def test_ward_slot(catalogue):
    media = catalogue.render("hi", "FIN_TEAM", {"ward": 7})
    assert "chaar/hi/slot/ward_7" in media


def test_a_missing_file_is_named_not_invented(catalogue, damage):
    damage("hi/slot/day_20.wav", "hi/FC_CLOSE.1.wav")
    with pytest.raises(MissingAudio) as e:
        catalogue.render("hi", "FC_CLOSE", CLOSE)
    assert e.value.files == ["hi/FC_CLOSE.1.wav", "hi/slot/day_20.wav"]
    assert catalogue.render("en", "FC_CLOSE", CLOSE)  # another language is untouched


def test_a_missing_slot_value_is_refused(catalogue):
    with pytest.raises(MissingSlot):
        catalogue.render("hi", "FC_CLOSE", {k: v for k, v in CLOSE.items() if k != "date"})
    with pytest.raises(MissingSlot):
        catalogue.render("hi", "FC_CLOSE", {**CLOSE, "time": "9am"})


def test_unknown_prompt_or_language(catalogue):
    with pytest.raises(UnknownPrompt):
        catalogue.text("NOPE", "hi")
    with pytest.raises(UnknownPrompt):
        catalogue.render("fr", "SYS_BYE")


def test_missing_lists_every_absent_file_for_one_language(catalogue, damage):
    assert catalogue.missing("hi") == []
    damage("hi/FC_HELLO.0.wav", "hi/slot/clinic_cln_a.wav", "hi/slot/time_0900.wav")
    assert catalogue.missing("hi") == [
        "hi/FC_HELLO.0.wav",
        "hi/slot/clinic_cln_a.wav",
        "hi/slot/time_0900.wav",
    ]
    assert catalogue.missing("doi") == []


def test_missing_adds_the_clinics_wards_and_times_it_is_asked_about(catalogue):
    extra = catalogue.missing("hi", clinic_ids=["cln_new"], ward_nos=[41], times=["17:30"])
    assert extra == ["hi/slot/clinic_cln_new.wav", "hi/slot/time_1730.wav", "hi/slot/ward_41.wav"]


def test_an_empty_audio_folder_misses_everything_including_the_undefined_prompts(tmp_path):
    cat = PromptCatalogue(REAL_PROMPTS, tmp_path / "none", clinic_ids=[CLINIC], times=["09:00"])
    missing = cat.missing("hi")
    assert "hi/FC_CLOSE.0.wav" in missing and "hi/slot/weekday_6.wav" in missing
    assert len(missing) >= len(cat.manifest("hi"))  # the manifest is exactly the missing set
    assert {m.path for m in cat.manifest("hi")} <= set(missing)


def test_manifest_lists_every_required_file_once_with_the_words_to_record(catalogue):
    items = catalogue.manifest()
    paths = [i.path for i in items]
    assert len(paths) == len(set(paths))
    assert {i.lang for i in items} == {"hi", "doi", "ur"}
    seg = next(i for i in items if i.path == "hi/FC_NOTICE.wav")
    assert seg.kind == "segment" and seg.text == catalogue.text("FC_NOTICE", "hi")
    slot = next(i for i in items if i.path == "doi/slot/clinic_cln_a.wav")
    assert (slot.kind, slot.slot, slot.key, slot.text) == ("slot", "clinic", CLINIC, None)
    assert sum(1 for i in items if i.lang == "hi" and i.slot == "day") == 31
    assert sum(1 for i in items if i.lang == "hi" and i.slot == "month") == 12
    assert sum(1 for i in items if i.lang == "hi" and i.slot == "weekday") == 7
    assert sum(1 for i in items if i.lang == "hi" and i.slot == "num") == 10
    # the test voice was recorded to this manifest, so nothing is missing in any language
    assert all(catalogue.missing(lang) == [] for lang in ("hi", "doi", "ur"))


def test_hhmm():
    assert hhmm("09:00") == "0900" and hhmm("0930") == "0930"
    for bad in ("9:00", "0900pm", "", "12345"):
        with pytest.raises(MissingSlot):
            hhmm(bad)


def test_the_visit_time_is_the_clinics_own_and_is_refused_when_absent(ictx, world):
    world.clinic("cln_t", visit_time="09:30")
    world.clinic("cln_none", visit_time=None)
    assert visit_time(ictx.db.one("SELECT * FROM clinics WHERE id='cln_t'")) == "0930"
    with pytest.raises(ClinicSetupError) as e:
        visit_time(ictx.db.one("SELECT * FROM clinics WHERE id='cln_none'"))
    assert "no visit time" in e.value.message
    assert isinstance(e.value, PromptError)


def test_required_extent_covers_clinics_wards_and_the_visit_time(ictx, world):
    world.clinic("cln_x")
    world.clinic("cln_y", visit_time=None)  # no time set up: it adds none, and its calls are refused
    extent = required_extent(ictx)
    assert extent["clinic_ids"] == ["cln_x", "cln_y"]
    assert len(extent["ward_nos"]) == 75 and extent["ward_nos"][0] == 1
    assert extent["times"] == ["0900"]
    assert ictx.prompts.missing("hi", **extent)  # 75 wards are not in the test voice


def test_a_clinic_or_ward_name_that_could_leave_the_audio_folder_is_refused(catalogue):
    for bad in ("../x", "a/b", "a b", ""):
        with pytest.raises(MissingSlot):
            catalogue.render("hi", "FIN_TEAM", {"ward": bad})
        with pytest.raises(MissingSlot):
            catalogue.render("hi", "FC_CLOSE", {**CLOSE, "clinic": bad})
