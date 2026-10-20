"""voiced_seconds: how long a person spoke, measured from the recording itself."""

from __future__ import annotations

import io
import struct
import wave

import pytest
from ivr_support import _wav, noise_wav, silent_wav, speech_wav, tone_burst_wav

from chaarteeke.config import DATA_DIR
from chaarteeke.ivr.voiced import UnreadableRecording, voiced_seconds
from chaarteeke.policy import load_policy

CALLS = load_policy()["calls"]
RMS_MIN = CALLS["speech_frame_rms_min"]
MIN_VOICED = CALLS["speech_min_voiced_s"]


def test_a_tone_is_voiced_for_its_whole_length():
    assert voiced_seconds(speech_wav(1.0), RMS_MIN) == pytest.approx(1.0, abs=0.02)
    assert voiced_seconds(speech_wav(0.5, rate=8000), RMS_MIN) == pytest.approx(0.5, abs=0.02)


def test_silence_has_no_voiced_seconds():
    assert voiced_seconds(silent_wav(2.0), RMS_MIN) == 0.0


def test_a_header_only_file_is_silent_not_an_error():
    header = _wav(b"", 8000)
    assert len(header) == 44
    assert voiced_seconds(header, RMS_MIN) == 0.0


def test_a_short_word_is_measured_not_rounded_to_whole_seconds():
    word = tone_burst_wav(total_s=2.0, burst_s=0.4)
    assert voiced_seconds(word, RMS_MIN) == pytest.approx(0.4, abs=0.04)
    assert voiced_seconds(word, RMS_MIN) >= MIN_VOICED


def test_quiet_line_noise_is_not_voiced():
    assert voiced_seconds(noise_wav(3.0, rms=0.005), RMS_MIN) == 0.0
    assert voiced_seconds(noise_wav(3.0, rms=0.015), RMS_MIN) == 0.0


def test_a_brief_noise_burst_stays_under_the_minimum():
    burst = noise_wav(0.2, rms=0.1)  # 0.2 s of loud noise: voiced, but under speech_min_voiced_s
    seconds = voiced_seconds(burst, RMS_MIN)
    assert seconds == pytest.approx(0.2, abs=0.02) and seconds < MIN_VOICED


def test_the_frame_threshold_is_on_a_minus_one_to_one_scale():
    amp = 0.03 * 32768 * 2**0.5  # a sine with RMS 0.03
    loud = speech_wav(0.5, rate=8000, amp=int(amp))
    quiet = speech_wav(0.5, rate=8000, amp=int(0.01 * 32768 * 2**0.5))
    assert voiced_seconds(loud, 0.02) == pytest.approx(0.5, abs=0.02)
    assert voiced_seconds(loud, 0.05) == 0.0
    assert voiced_seconds(quiet, 0.02) == 0.0


def test_a_last_frame_shorter_than_20_ms_is_not_counted():
    assert voiced_seconds(speech_wav(0.03, rate=8000), RMS_MIN) == 0.02


def test_two_channels_are_measured_over_the_same_20_ms():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<hh", 8000, -8000) * 4000)  # 0.5 s
    assert voiced_seconds(buf.getvalue(), RMS_MIN) == pytest.approx(0.5, abs=0.02)


def test_audio_that_is_not_a_16_bit_wav_is_refused_not_guessed():
    with pytest.raises(UnreadableRecording):
        voiced_seconds(b"", RMS_MIN)
    with pytest.raises(UnreadableRecording):
        voiced_seconds(b"RIFFaudio", RMS_MIN)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(8000)
        w.writeframes(b"\x80" * 800)
    with pytest.raises(UnreadableRecording):
        voiced_seconds(buf.getvalue(), RMS_MIN)


@pytest.mark.parametrize("n", range(1, 13))
def test_every_shipped_demo_clip_counts_as_speech(n):
    clip = (DATA_DIR / "demo" / "clips" / f"{n}.wav").read_bytes()
    assert voiced_seconds(clip, RMS_MIN) >= MIN_VOICED
