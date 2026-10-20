"""The speech module without the model: loading rules, errors in plain words, the cache and the lock."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from places_support import wav_bytes

from chaarteeke.config import ROOT, Settings
from chaarteeke.speech import asr
from chaarteeke.speech.asr import ASSET_FILES, IndicConformerTranscriber, SpeechError, load_transcriber


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def model_folder(tmp_path: Path, missing: tuple[str, ...] = ()) -> Path:
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    for name in ASSET_FILES:
        if name not in missing:
            (assets / name).write_bytes(b"x")
    return tmp_path


def test_importing_the_module_loads_no_heavy_library():
    code = (
        "import sys, chaarteeke.speech.asr, chaarteeke.places.review, chaarteeke.places.matcher;"
        "bad=[m for m in ('torch','onnxruntime','numpy','scipy','soundfile') if m in sys.modules];"
        "print(bad)"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]", out.stdout


def test_asr_off_gives_no_transcriber():
    assert load_transcriber(settings(asr="off")) is None


def test_a_missing_model_folder_is_an_error_in_plain_words(tmp_path, monkeypatch):
    monkeypatch.setenv(asr.MODEL_DIR_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(SpeechError) as e:
        load_transcriber(settings(asr="indicconformer"))
    msg = str(e.value)
    assert "nowhere" in msg and "CT_ASR=off" in msg and asr.MODEL_DIR_ENV in msg


def test_a_model_folder_missing_a_file_names_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv(asr.MODEL_DIR_ENV, str(model_folder(tmp_path, missing=("encoder.onnx",))))
    with pytest.raises(SpeechError, match="encoder.onnx"):
        load_transcriber(settings(asr="indicconformer"))


def test_a_missing_library_is_named_with_the_extra_to_install(tmp_path, monkeypatch):
    import importlib.util

    monkeypatch.setenv(asr.MODEL_DIR_ENV, str(model_folder(tmp_path)))
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "torch" else real(name, *a)
    )
    with pytest.raises(SpeechError) as e:
        load_transcriber(settings(asr="indicconformer"))
    assert "torch" in str(e.value) and "--extra asr" in str(e.value)


def test_an_unset_model_folder_is_a_startup_error_that_names_the_variable(monkeypatch):
    monkeypatch.delenv(asr.MODEL_DIR_ENV, raising=False)
    with pytest.raises(SpeechError) as e:
        load_transcriber(settings(asr="indicconformer"))
    assert asr.MODEL_DIR_ENV in str(e.value) and "CT_ASR=off" in str(e.value)
    with pytest.raises(SpeechError, match=asr.MODEL_DIR_ENV):
        asr.model_dir()


def test_the_environment_overrides_the_model_folder(monkeypatch, tmp_path):
    monkeypatch.setenv(asr.MODEL_DIR_ENV, str(tmp_path))
    assert asr.model_dir() == tmp_path


class _Loaded:
    def __init__(self):
        self.runs = []
        self.inside = 0
        self.max_inside = 0

    def run(self, audio, code):
        self.inside += 1
        self.max_inside = max(self.max_inside, self.inside)
        self.runs.append(code)
        time.sleep(0.005)
        self.inside -= 1
        return f"heard in {code}", 0.9, asr.Frames([[0.0]], ("a", "|"), code)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """A transcriber whose audio decoding and model are replaced, so no library or weights are needed."""
    t = IndicConformerTranscriber(tmp_path)
    loaded = _Loaded()
    monkeypatch.setattr(t, "_ensure_loaded", lambda: loaded)
    seconds = {"value": 1.0}
    monkeypatch.setattr(asr, "_decode", lambda wav: [0.0] * int(seconds["value"] * asr.SAMPLE_RATE))
    return t, loaded, seconds


def test_language_hints_map_to_the_models_codes(fake):
    t, loaded, _ = fake
    for lang in ("hi", "doi", "ur"):
        assert t.transcribe(wav_bytes() + lang.encode(), lang).text == f"heard in {lang}"
    assert loaded.runs == ["hi", "doi", "ur"]


def test_an_unknown_language_is_refused_before_anything_loads(tmp_path):
    t = IndicConformerTranscriber(tmp_path)
    with pytest.raises(SpeechError, match="'pa'"):
        t.transcribe(wav_bytes(), "pa")


def test_the_transcript_names_the_model_and_the_audio_length(fake):
    t, _, seconds = fake
    seconds["value"] = 2.5
    r = t.transcribe(wav_bytes(), "hi")
    assert r.model == "indic-conformer-600m-multilingual fp32 CTC" and r.seconds == 2.5


def test_the_same_audio_and_language_is_transcribed_once(fake):
    t, loaded, _ = fake
    clip = wav_bytes()
    a, b = t.transcribe(clip, "hi"), t.transcribe(clip, "hi")
    assert a == b and loaded.runs == ["hi"]
    t.transcribe(clip, "doi")  # a different language is a different question
    t.transcribe(wav_bytes(2.0), "hi")  # different audio too
    assert loaded.runs == ["hi", "doi", "hi"]


def test_the_cache_is_bounded(fake):
    t, loaded, _ = fake
    for i in range(asr.CACHE_SIZE + 5):
        t.transcribe(wav_bytes() + bytes([i]), "hi")
    assert len(t._cache) == asr.CACHE_SIZE


def test_audio_too_short_to_hold_a_word_is_an_error(fake):
    t, loaded, seconds = fake
    seconds["value"] = 0.05
    with pytest.raises(SpeechError, match="too short"):
        t.transcribe(wav_bytes(), "hi")
    assert loaded.runs == []


def test_one_clip_at_a_time_goes_through_the_model(fake):
    t, loaded, _ = fake
    threads = [threading.Thread(target=t.transcribe, args=(wav_bytes() + bytes([i]), "hi")) for i in range(8)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert len(loaded.runs) == 8 and loaded.max_inside == 1


def test_a_failed_decode_is_an_error_and_is_not_cached(tmp_path, monkeypatch):
    t = IndicConformerTranscriber(tmp_path)
    monkeypatch.setattr(t, "_ensure_loaded", lambda: _Loaded())

    def bad(wav):
        raise SpeechError("The recording could not be read as audio.")

    monkeypatch.setattr(asr, "_decode", bad)
    with pytest.raises(SpeechError, match="could not be read"):
        t.transcribe(b"not audio", "hi")
    assert len(t._cache) == 0


def test_a_recording_far_longer_than_a_place_is_refused_before_it_is_decoded(fake):
    t, loaded, _ = fake
    with pytest.raises(SpeechError, match="too long"):
        t.transcribe(b"\x00" * (asr.MAX_BYTES + 1), "hi")
    assert loaded.runs == [] and len(t._cache) == 0


def test_audio_longer_than_the_limit_is_an_error_and_is_not_cached(fake):
    t, loaded, seconds = fake
    seconds["value"] = asr.MAX_SECONDS + 1
    with pytest.raises(SpeechError, match="too long"):
        t.transcribe(wav_bytes(), "hi")
    assert loaded.runs == [] and len(t._cache) == 0


def test_the_transcript_carries_the_decoder_confidence(fake):
    t, _, _ = fake
    assert t.transcribe(wav_bytes(), "hi").confidence == 0.9


def test_ctc_collapse_keeps_letters_once_and_scores_them():
    np = pytest.importorskip("numpy")
    vocab = ["a", "b", "▁"]
    probs = np.full((6, asr.BLANK + 1), 1e-6)
    # frames: a a blank b b (space) -> "ab " stripped to "ab"; the first letter is surer than the second
    for frame, (idx, p) in enumerate(
        [(0, 0.9), (0, 0.7), (asr.BLANK, 0.99), (1, 0.5), (1, 0.6), (asr.BLANK, 0.99)]
    ):
        probs[frame, idx] = p
    text, confidence = asr.collapse_ctc(np.log(probs), vocab)
    assert text == "ab"
    assert confidence == round((0.9 + 0.6) / 2, 4)  # the best frame of each letter's run
    silent = np.full((3, asr.BLANK + 1), 1e-6)
    silent[:, asr.BLANK] = 0.99
    assert asr.collapse_ctc(np.log(silent), vocab) == ("", None)


def test_the_transcript_carries_the_frames_and_they_do_not_change_equality(fake):
    t, _, _ = fake
    r = t.transcribe(wav_bytes(), "ur")
    assert r.frames is not None and r.frames.lang == "ur" and r.frames.vocab[-1] == "|"
    assert r == asr.Transcript(text="heard in ur", model=r.model, seconds=r.seconds, confidence=0.9)


def test_frames_are_log_probabilities_that_sum_to_one_per_frame():
    np = pytest.importorskip("numpy", reason="needs the asr extra (uv sync --extra asr)")

    raw = np.log(
        np.array([[0.2, 0.2, 0.1], [0.05, 0.05, 0.4]], dtype=np.float32)
    )  # mass left over for other languages
    out = asr._renormalise(raw)
    assert out.dtype == np.float32
    assert np.allclose(np.exp(out).sum(axis=1), 1.0, atol=1e-6)
    assert np.allclose(out[0] - out[0, 0], raw[0] - raw[0, 0], atol=1e-6)  # ratios between pieces are kept
