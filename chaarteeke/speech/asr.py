"""Speech to text for the place a patient names: AI4Bharat IndicConformer-600M, fp32, CTC decoding.

This follows the loader that was measured in the speech evaluation (docs/EVALS.md): the TorchScript
preprocessor makes the mel features, the ONNX encoder and CTC decoder run on the CPU, and the language mask
picks Hindi (hi), Dogri (doi) or Urdu (ur) out of the 22-language vocabulary. The model is read from a folder
on this machine and never downloaded. Heavy libraries are imported inside functions, so importing this
module is free and `CT_ASR=off` needs none of them.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..config import Settings

MODEL_DIR_ENV = "CT_ASR_MODEL_DIR"
MODEL_ID = "indic-conformer-600m-multilingual fp32 CTC"
SAMPLE_RATE = 16000
BLANK = 256  # the CTC blank id in this model's vocabulary
MIN_SECONDS = 0.2  # shorter than this holds no word
MAX_SECONDS = (
    30.0  # a place is spoken in a few seconds (policy calls.place_record_max_s); this only guards memory
)
MAX_BYTES = 8_000_000  # about 30 s of 16-bit stereo at 48 kHz; larger is refused before it is decoded
CACHE_SIZE = 16  # transcripts kept in memory by audio hash; never written to disk
LANG_CODES = {"hi": "hi", "doi": "doi", "ur": "ur"}
RUNTIME_MODULES = ("numpy", "scipy", "soundfile", "onnxruntime", "torch")
ASSET_FILES = ("preprocessor.ts", "encoder.onnx", "ctc_decoder.onnx", "vocab.json", "language_masks.json")


class SpeechError(RuntimeError):
    """The speech model is missing, will not load, or could not read this audio. The message is plain words."""


@dataclass(frozen=True)
class Frames:
    """What the CTC decoder said about one clip, frame by frame (one frame is about 80 ms of sound).

    The greedy transcript keeps one letter per frame and drops the rest; the gazetteer keyword spotter
    (places/kws.py) searches a place name in these numbers instead, so a name the transcript garbled can still
    be found. `logp` holds log-probabilities over this language's vocabulary (each row sums to 1 once
    exponentiated), `vocab[i]` is the piece for column i and the last column is the CTC blank."""

    logp: Any  # numpy float32 array, shape (frames, len(vocab))
    vocab: tuple[str, ...]
    lang: str  # the model's language code the rows were made with: hi, doi or ur


@dataclass(frozen=True)
class Transcript:
    text: str
    model: str
    seconds: float  # length of the audio that was read
    # Mean probability the CTC decoder gave to the letters it kept, 0 to 1 (None: the speech model did not
    # say, as with a test double). Nothing decides on it yet; the matcher's "likely" rule cannot use it until
    # data/policy.json holds a threshold, which is the architect's file.
    confidence: float | None = None
    # The frame log-probabilities behind `text` (None: the speech model did not give them, as with a test
    # double; the keyword spotter does not run then and the transcript alone is matched). Not compared or shown.
    frames: Frames | None = field(default=None, compare=False, repr=False)


class Transcriber(Protocol):
    model_id: str

    def transcribe(self, wav: bytes, lang: str) -> Transcript: ...


def model_dir() -> Path:
    """The folder named by CT_ASR_MODEL_DIR. There is no default: a path on the developer's machine would
    stand in for a setting the operator has not made."""
    chosen = os.environ.get(MODEL_DIR_ENV)
    if not chosen:
        raise SpeechError(
            f"The speech model folder is not set. Set {MODEL_DIR_ENV} to the folder that holds the "
            "IndicConformer files, or start with CT_ASR=off."
        )
    return Path(chosen)


def check_runtime(directory: Path | None = None) -> None:
    """Raise SpeechError, in plain words, unless the model folder and the libraries it needs are present."""
    import importlib.util

    root = directory or model_dir()
    assets = root / "assets"
    missing_files = [f for f in ASSET_FILES if not (assets / f).is_file()]
    if missing_files:
        raise SpeechError(
            f"The speech model is not at {root} (missing {', '.join(missing_files)}). Put the "
            f"IndicConformer files there, set {MODEL_DIR_ENV} to where they are, or start with CT_ASR=off."
        )
    missing_libs = [m for m in RUNTIME_MODULES if importlib.util.find_spec(m) is None]
    if missing_libs:
        raise SpeechError(
            f"The speech model needs these Python packages: {', '.join(missing_libs)}. "
            "Install the speech extra (uv sync --extra asr) or start with CT_ASR=off."
        )


def load_transcriber(settings: Settings) -> Transcriber | None:
    """None only when CT_ASR=off. Otherwise the model folder and libraries are checked now and a missing one
    raises SpeechError; the weights themselves load on the first clip (or on `.load()`, as preflight does)."""
    if settings.asr == "off":
        return None
    check_runtime()
    return IndicConformerTranscriber(model_dir())


class IndicConformerTranscriber:
    model_id = MODEL_ID

    def __init__(self, directory: Path, threads: int | None = None) -> None:
        self._dir = Path(directory)
        self._threads = threads or min(4, os.cpu_count() or 1)
        self._lock = threading.Lock()
        self._loaded: _Loaded | None = None
        self._cache: OrderedDict[tuple[str, str], Transcript] = OrderedDict()

    # --------------------------------------------------------------------- public
    def load(self) -> None:
        """Load the weights now (once). Safe to call again."""
        with self._lock:
            self._ensure_loaded()

    def transcribe(self, wav: bytes, lang: str) -> Transcript:
        code = LANG_CODES.get(lang)
        if code is None:
            raise SpeechError(f"The speech model does not know the language '{lang}'.")
        if len(wav) > MAX_BYTES:
            raise SpeechError("The recording is too long to read.")
        key = (hashlib.sha256(wav).hexdigest(), code)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
            audio = _decode(wav)
            seconds = len(audio) / SAMPLE_RATE
            if seconds < MIN_SECONDS:
                raise SpeechError("The recording is too short to hold a word.")
            if seconds > MAX_SECONDS:
                raise SpeechError("The recording is too long to read.")
            text, confidence, frames = self._ensure_loaded().run(audio, code)
            result = Transcript(
                text=text,
                model=self.model_id,
                seconds=round(seconds, 2),
                confidence=confidence,
                frames=frames,
            )
            self._cache[key] = result
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
            return result

    # -------------------------------------------------------------------- private
    def _ensure_loaded(self) -> _Loaded:
        if self._loaded is None:
            check_runtime(self._dir)
            self._loaded = _Loaded(self._dir / "assets", self._threads)
        return self._loaded


class _Loaded:
    def __init__(self, assets: Path, threads: int) -> None:
        import onnxruntime as ort
        import torch

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self._torch = torch
        self._pre = torch.jit.load(str(assets / "preprocessor.ts"), map_location="cpu")

        def session(name: str) -> Any:
            return ort.InferenceSession(
                str(assets / f"{name}.onnx"), opts, providers=["CPUExecutionProvider"]
            )

        self._enc = session("encoder")
        self._ctc = session("ctc_decoder")
        self._vocab = json.loads((assets / "vocab.json").read_text(encoding="utf-8"))
        self._masks = json.loads((assets / "language_masks.json").read_text(encoding="utf-8"))

    def run(self, audio: Any, lang: str) -> tuple[str, float | None, Frames]:
        import numpy as np

        torch = self._torch
        with torch.inference_mode():
            wave = torch.from_numpy(audio.astype(np.float32))[None]
            feats, length = self._pre(input_signal=wave, length=torch.tensor([wave.shape[-1]]))
        encoded, _ = self._enc.run(
            ["outputs", "encoded_lengths"], {"audio_signal": feats.numpy(), "length": length.numpy()}
        )
        logp = self._ctc.run(["logprobs"], {"encoder_output": encoded})[0][:, :, self._masks[lang]]
        text, confidence = collapse_ctc(logp[0], self._vocab[lang])
        return text, confidence, Frames(_renormalise(logp[0]), tuple(self._vocab[lang]), lang)


def _renormalise(logp: Any) -> Any:
    """Log-probabilities of the kept columns, scaled to sum to 1 per frame: the decoder's softmax also covers the
    other languages' pieces, which are cut away here."""
    import numpy as np

    top = logp.max(axis=-1, keepdims=True)
    return (logp - top - np.log(np.exp(logp - top).sum(axis=-1, keepdims=True))).astype(np.float32)


def collapse_ctc(logp: Any, vocab: list[str]) -> tuple[str, float | None]:
    """Greedy CTC decoding of one clip: frames of log-probabilities in, the text and its confidence out.

    Repeated frames and blanks are dropped. The confidence is the mean, over the letters kept, of the best
    probability any frame of that letter's run gave it. None when nothing was kept."""
    import numpy as np

    best = logp.argmax(-1)
    out: list[str] = []
    sure: list[float] = []
    prev = None
    for frame, i in enumerate(best):
        p = float(np.exp(logp[frame, i]))
        if i != prev and i != BLANK:
            out.append(vocab[i])
            sure.append(p)
        elif i == prev and i != BLANK and sure:
            sure[-1] = max(sure[-1], p)
        prev = i
    text = "".join(out).replace("▁", " ").strip()
    return text, (round(min(1.0, sum(sure) / len(sure)), 4) if sure else None)


def _decode(wav: bytes) -> Any:
    """Mono float32 at 16 kHz from WAV bytes of any rate (a browser clip is 16 kHz, a phone line 8 kHz)."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    try:
        data, rate = sf.read(io.BytesIO(wav), dtype="float32", always_2d=True)
    except Exception as e:
        raise SpeechError("The recording could not be read as audio.") from e
    mono = data.mean(axis=1)
    if rate != SAMPLE_RATE:
        g = int(np.gcd(SAMPLE_RATE, rate))
        mono = resample_poly(mono, SAMPLE_RATE // g, rate // g).astype(np.float32)
    return mono
