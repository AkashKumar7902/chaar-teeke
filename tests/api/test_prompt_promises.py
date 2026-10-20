"""The missed-dose "go to the clinic" message must not promise a call that the system never makes."""

from __future__ import annotations

import re

import pytest

from chaarteeke.config import DATA_DIR
from chaarteeke.ivr.prompts import PromptCatalogue

CATALOGUE = PromptCatalogue(DATA_DIR / "prompts" / "prompts.json", DATA_DIR / "prompts" / "audio")
TOMORROW = {"en": "tomorrow", "hi": "कल", "doi": "कल्ल", "ur": "کل"}
AGAIN = {"en": "call again", "hi": "फिर फ़ोन", "doi": "फही फोन", "ur": "دوبارہ فون"}


@pytest.mark.parametrize("lang", ["en", "hi", "doi", "ur"])
def test_rm_miss_not_promises_no_second_call(lang):
    text = CATALOGUE.text("RM_MISS_NOT", lang)
    words = re.findall(r"\w+", text.lower())
    assert TOMORROW[lang] not in words and AGAIN[lang] not in text.lower()
    assert "{clinic}" in text  # it still sends the patient to the clinic


@pytest.mark.parametrize("lang", ["hi", "doi", "ur"])
def test_the_audio_for_rm_miss_not_is_there(lang):
    assert CATALOGUE.missing(lang) == []
    pieces = [DATA_DIR / "prompts" / "audio" / lang / f"RM_MISS_NOT.{k}.wav" for k in (0, 1)]
    assert all(p.is_file() for p in pieces)


@pytest.mark.parametrize("lang", ["en", "hi", "doi", "ur"])
def test_the_review_status_is_still_the_machine_draft(lang):
    import json

    entry = next(
        e
        for e in json.loads((DATA_DIR / "prompts" / "prompts.json").read_text(encoding="utf-8"))["prompts"]
        if e["id"] == "RM_MISS_NOT"
    )
    assert (
        entry["review_status"][lang]
        == {
            "en": "source",
            "hi": "needs_check",
            "doi": "machine_draft_needs_native_review",
            "ur": "machine_draft_needs_native_review",
        }[lang]
    )
