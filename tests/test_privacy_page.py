"""The /privacy page says everything the spoken full notice (FC_NOTICE_FULL) says, in every language.

The spoken full notice, the sticker and the page are meant to be one text (docs/PRIVACY.md). This pins the
three things the first version of the page left out, and that the four languages stay the same shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "web" / "i18n"
LANGS = ["en", "hi", "doi", "ur"]

# What FC_NOTICE_FULL says that the short notice and the first page did not, in each language's own words. Each
# phrase must be in the spoken text too, so a page edit cannot drift away from it.
MUST_SAY = {
    "en": {
        "the Data Protection Board": "Data Protection Board of India",
        "the register link": "entry in the clinic's register",
        "the call if the dog looked sick": "the clinic may call you",
    },
    "hi": {
        "the Data Protection Board": "डेटा प्रोटेक्शन बोर्ड ऑफ़ इंडिया",
        "the register link": "क्लिनिक के रजिस्टर में आपकी एंट्री से जुड़े",
        "the call if the dog looked sick": "बीमार लगा हो तो क्लिनिक आपको फ़ोन कर सकता है",
    },
    "doi": {
        "the Data Protection Board": "डेटा प्रोटेक्शन बोर्ड ऑफ इंडिया",
        "the register link": "क्लिनिक दे रजिस्टर च तुंदी एंट्री कन्नै जुड़े",
        "the call if the dog looked sick": "बमार लग्गेआ होऐ तां क्लिनिक तुसेंगी फोन करी सकदा ऐ",
    },
    "ur": {
        "the Data Protection Board": "ڈیٹا پروٹیکشن بورڈ آف انڈیا",
        "the register link": "کلینک کے رجسٹر میں آپ کے اندراج سے جڑے",
        "the call if the dog looked sick": "بیمار لگا ہو تو کلینک آپ کو فون کر سکتا ہے",
    },
}


def _page(lang: str) -> dict:
    return json.loads((I18N / f"privacy.{lang}.json").read_text(encoding="utf-8"))["privacy"]


def _page_text(lang: str) -> str:
    return " ".join(item["p"] for item in _page(lang)["items"].values())


def _full_notice(lang: str) -> str:
    prompts = json.loads((ROOT / "data" / "prompts" / "prompts.json").read_text(encoding="utf-8"))
    (notice,) = [p for p in prompts["prompts"] if p["id"] == "FC_NOTICE_FULL"]
    return notice["text"][lang]


@pytest.mark.parametrize("lang", LANGS)
def test_the_page_says_what_the_full_notice_says(lang):
    page, spoken = _page_text(lang), _full_notice(lang)
    for what, phrase in MUST_SAY[lang].items():
        assert phrase in page, f"privacy.{lang}.json does not say {what}"
        assert (
            phrase in spoken
        ), f"{what}: {phrase!r} is not in FC_NOTICE_FULL ({lang}); page and notice differ"


@pytest.mark.parametrize("lang", LANGS[1:])
def test_every_language_has_the_same_sections_as_english(lang):
    en, other = _page("en")["items"], _page(lang)["items"]
    assert list(other) == list(en)
    for key in en:
        assert set(other[key]) == {"h", "p"}
        assert other[key]["h"].strip() and other[key]["p"].strip()


@pytest.mark.parametrize("lang", ["doi", "ur"])
def test_dogri_and_urdu_stay_flagged_as_machine_drafts(lang):
    meta = json.loads((I18N / f"privacy.{lang}.json").read_text(encoding="utf-8"))["_meta"]
    assert meta["machine_written"] is True
    assert meta["needs_native_check"] is True


@pytest.mark.parametrize("lang", LANGS)
def test_each_section_is_short(lang):
    longest = max(len(item["p"].split()) for item in _page(lang)["items"].values())
    assert longest <= 55, f"a privacy section has {longest} words in {lang}; keep each one short"
