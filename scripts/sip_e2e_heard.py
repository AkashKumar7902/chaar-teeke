#!/usr/bin/env python3
"""What the speech model heard in the recordings a SIP end-to-end run kept (scripts/sip_e2e.sh --asr).

  CT_ASR_MODEL_DIR=<IndicConformer folder> uv run python scripts/sip_e2e_heard.py RUN_DIR [RUN_DIR ...]

Why this exists: when the app places a report in a ward by itself it deletes the clip, the words and the
candidates at once (privacy), so the run's database cannot say what was heard. The run therefore keeps a hard
link to the file the app saved (RUN_DIR/captured/1.wav, the exact bytes the app read, as recorded by Asterisk
from 8 kHz RTP). This script loads the speech model once, after the run's processes have stopped, reads those
files with the app's own Transcriber, and sends the words through the app's own matcher and keyword spotter
with the shipped gazetteer and policy. It shows the words, the model's own confidence (the mean probability of
the letters it kept, 0 to 1), the matcher's label and candidates, and whether the app's decision in
RUN_DIR/report.json agrees.

Differences from the app, stated so the output is not read as more than it is: this is a second load of the
model (the app's copy is gone), the gazetteer is read without the run's database (the demo database holds four
reviewer-taught "<name> market" forms), and the time shown is the first read after a load. Nothing is installed
and no outside service is called. No fallbacks: a missing folder, library or file stops the script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class HeardError(Exception):
    """A step that cannot run. The message says which."""


def clips_of(run_dir: Path) -> list[tuple[str, Path]]:
    """The recordings of a run: the one file the app saved, and, when the run kept them, Asterisk's own copy of
    every recording attempt (the app asks again when Asterisk reports no talking time, so there can be two)."""
    saved = sorted((run_dir / "captured").glob("*.wav"))
    if len(saved) != 1:
        raise HeardError(f"{run_dir}: expected one file in captured/, found {len(saved)}")
    found = [("app saved", saved[0])]
    found += [
        (f"Asterisk {p.stem.rsplit('-', 1)[-1]}", p)
        for p in sorted((run_dir / "captured_asterisk").glob("*.wav"))
    ]
    return found


def app_decision(run_dir: Path) -> dict | None:
    """The app's own decision for this run (the `place_outcome` the run wrote), or None if there is none."""
    report = run_dir / "report.json"
    if not report.is_file():
        raise HeardError(f"{run_dir}: no report.json (did the run finish?)")
    return json.loads(report.read_text()).get("measures", {}).get("place_outcome")


def agrees(app: dict | None, ours: dict) -> str:
    """Does what this script found match what the app did? Compared on the decision, not on the words."""
    if not app:
        return "no app decision to compare"
    app_placed = app.get("state") == "placed by itself"
    ours_placed = ours["combined"]["confidence"] == "likely"
    if app_placed != ours_placed:
        return f"DIFFERENT: app {app.get('state')}, here {ours['combined']['confidence']}"
    if app_placed:
        same = ours["combined"]["wards"][:1] == [app.get("ward_no")]
        return (
            "same ward"
            if same
            else f"DIFFERENT ward: app {app.get('ward_no')}, here {ours['combined']['wards'][:1]}"
        )
    app_words = app.get("transcript")
    if app_words is not None and app_words != ours["words"]:
        return f"same decision, DIFFERENT words: app {app_words!r}"
    return "same decision" + (", same words" if app_words is not None else "")


def read_clip(transcriber, gaz, policy, wav: Path, lang: str) -> dict:
    from chaarteeke.places import kws
    from chaarteeke.places.matcher import MatchResult, match

    cfg = policy["matcher"]
    began = time.perf_counter()
    heard = transcriber.transcribe(wav.read_bytes(), lang)
    took = time.perf_counter() - began
    text = match(gaz, heard.text, policy) if heard.text else None
    found = kws.spotter_for(gaz).search(heard.frames, cfg["kws_possible_min"])
    base = text if text is not None else MatchResult("none")
    both = kws.combine(base, found, cfg, heard.text)

    def cands(result) -> list[dict]:
        return [
            {
                "ward_no": c.ward_no,
                "ward": gaz.ward_name(c.ward_no),
                "score": round(c.score, 1),
                "evidence": c.evidence,
            }
            for c in result.candidates
        ]

    return {
        "clip_seconds": round(heard.seconds, 2),
        "words": heard.text,
        "asr_confidence": None if heard.confidence is None else round(heard.confidence, 4),
        "read_seconds_after_load": round(took, 2),
        "transcript_alone": {
            "confidence": base.confidence,
            "wards": base.ward_nos,
            "candidates": cands(base),
        },
        "keywords_alone": [
            {
                "ward_no": h.ward_no,
                "ward": gaz.ward_name(h.ward_no),
                "score": round(h.score, 1),
                "evidence": h.evidence,
            }
            for h in found.hits[:3]
        ],
        "combined": {"confidence": both.confidence, "wards": both.ward_nos, "candidates": cands(both)},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="what the speech model heard in a SIP end-to-end run's kept recordings"
    )
    ap.add_argument(
        "runs", nargs="+", type=Path, help="run folders (each with captured/1.wav and report.json)"
    )
    ap.add_argument("--lang", default="hi", help="the language hint, as the call used (default hi)")
    ap.add_argument("--json", type=Path, help="also write the results to this file")
    args = ap.parse_args(argv)
    try:
        folder = os.environ.get("CT_ASR_MODEL_DIR")
        if not folder:
            raise HeardError("CT_ASR_MODEL_DIR is not set (there is no default folder)")
        work = [(run, clips_of(run), app_decision(run)) for run in args.runs]

        from chaarteeke.places.gazetteer import Gazetteer
        from chaarteeke.policy import load_policy
        from chaarteeke.speech.asr import IndicConformerTranscriber, check_runtime

        check_runtime(Path(folder))
        policy = load_policy()
        data = ROOT / "data"
        gaz = Gazetteer(data / "gazetteer.json", data / "wards.geojson", None)
        transcriber = IndicConformerTranscriber(Path(folder))
        began = time.perf_counter()
        transcriber.load()
        loaded = time.perf_counter() - began
        print(f"model loaded in {loaded:.1f} s (a second load, after the run's processes stopped)")
        out = []
        for run, clips, decision in work:
            same_as: dict[str, str] = {}  # file hash -> the label that read it first
            for label, wav in clips:
                digest = hashlib.sha256(wav.read_bytes()).hexdigest()
                if digest in same_as:
                    entry = {"run": run.name, "recording": label, "same_bytes_as": same_as[digest]}
                else:
                    same_as[digest] = label
                    entry = {
                        "run": run.name,
                        "recording": label,
                        **read_clip(transcriber, gaz, policy, wav, args.lang),
                    }
                    if label == "app saved":
                        entry["app_decision"] = decision
                        entry["agrees_with_app"] = agrees(decision, entry)
                out.append(entry)
                print(json.dumps(entry, ensure_ascii=False))
        if args.json:
            args.json.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except HeardError as e:
        print(f"sip_e2e_heard: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
