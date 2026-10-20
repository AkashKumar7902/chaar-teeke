"""Opt-in: loads the real 600M speech model (about 2.4 GB of memory) and hears one machine-voice clip.

    CT_TEST_ASR=1 CT_ASR_MODEL_DIR=<model folder> CT_TEST_ASR_CLIP=<a 16 kHz wav of "Bakshi Nagar"> \\
        uv run --extra asr pytest tests/places/test_places_asr_real.py

Never part of the normal run: the Mac has 8 GB, and a normal test run must not load a model.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from chaarteeke.config import Settings
from chaarteeke.places.matcher import match
from chaarteeke.policy import load_policy
from chaarteeke.speech.asr import load_transcriber

pytestmark = [
    pytest.mark.asr,
    pytest.mark.skipif(
        os.environ.get("CT_TEST_ASR") != "1", reason="loads the speech model; set CT_TEST_ASR=1"
    ),
    pytest.mark.skipif(
        not os.environ.get("CT_TEST_ASR_CLIP"), reason="set CT_TEST_ASR_CLIP to a wav of the place name"
    ),
]

CLIP = Path(os.environ.get("CT_TEST_ASR_CLIP", ""))


def test_the_real_model_hears_bakshi_nagar_and_the_matcher_places_it(gaz):
    t = load_transcriber(Settings(_env_file=None, asr="indicconformer"))
    heard = t.transcribe(CLIP.read_bytes(), "hi")
    assert heard.text
    assert match(gaz, heard.text, load_policy()).ward_nos[:1] == [27]
