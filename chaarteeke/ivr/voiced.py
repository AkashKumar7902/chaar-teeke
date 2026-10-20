"""How long a person spoke in a recording, measured from the audio itself.

Asterisk reports talking time in whole seconds, so a place named in a second or less often arrives as 0 and
looked like silence (5 of 18 SIP calls lost their place that way, 20 Oct 2026). The app therefore decides
from the recording it fetched whether there was speech, the same way for both transports. The rule: cut the audio into 20 ms
frames; a frame is voiced when its RMS, on samples scaled to -1..1, is at least policy
calls.speech_frame_rms_min; the recording is speech when the voiced seconds (voiced frames x 0.02) reach
policy calls.speech_min_voiced_s. Both numbers live in data/policy.json, not here.
"""

from __future__ import annotations

import io
import sys
import wave
from array import array

FRAME_S = 0.02  # the definition of "voiced seconds", not a tuning knob


class UnreadableRecording(ValueError):
    """The bytes are not a 16-bit WAV file. Said as it is: nothing is assumed about what was said."""


def voiced_seconds(wav_bytes: bytes, frame_rms_min: float) -> float:
    """Voiced seconds of a 16-bit WAV recording (a 44-byte file, with no samples, has 0.0). A last frame
    shorter than 20 ms is not counted."""
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            if width != 2 or rate <= 0:
                raise UnreadableRecording("the recording is not 16-bit audio")
            pcm = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise UnreadableRecording("the recording is not a WAV file") from e
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    size = max(1, round(rate * FRAME_S)) * channels
    floor = (frame_rms_min * 32768.0) ** 2  # compare mean squares: no square root per frame
    voiced = 0
    for start in range(0, len(samples) - size + 1, size):
        if sum(x * x for x in samples[start : start + size]) / size >= floor:
            voiced += 1
    return round(voiced * FRAME_S, 2)
