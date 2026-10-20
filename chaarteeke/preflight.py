"""`chaar-teeke preflight`: every check a demo needs, one line each, exit code 1 on a failure.

By default it never loads the speech model (that would cost gigabytes on an 8 GB laptop): it says whether
CT_ASR asks for one and whether its folder is there. With --with-asr it loads the model once and has it
transcribe the first bundled clip in data/demo/clips. The end-to-end call runs on a throwaway database in a temporary
folder, so the real data is not touched and no real number is dialled.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import math
import sys
import tempfile
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .context import AppContext

# What the scripted phone presses in the first call: Hindi, yes to reminders, yes to the questions, the first
# dose today, a place type, a street dog, a notch seen, and "no" to signs of rabies.
FIRST_CALL_KEYS = {
    "SYS_LANG": "1",
    "FC_CONSENT_Q": "1",
    "FC_QCONSENT": "1",
    "FC_DOSE": "1",
    "FC_PLACETYPE": "1",
    "FC_ANIMAL": "1",
    "FC_NOTCH": "1",
    "FC_SICK": "2",
}


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


def _line(r: Result) -> str:
    return f"{'ok  ' if r.ok else 'FAIL'}  {r.name:<11} {r.detail}"


def speech_wav(seconds: float = 1.5, rate: int = 16000) -> bytes:
    """A tone loud enough to count as speaking: what the scripted phone 'says' for the place question."""
    frames = b"".join(
        int(8000 * math.sin(2 * math.pi * 300 * i / rate)).to_bytes(2, "little", signed=True)
        for i in range(int(seconds * rate))
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


class ScriptedSocket:
    """The server's end of a browser WebSocket with a scripted person behind it."""

    def __init__(self, keys: dict[str, str]) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.keys = dict(keys)
        self.heard: list[str] = []
        self.closed = False

    async def receive(self) -> dict:
        return await self.incoming.get()

    async def send_json(self, message: dict) -> None:
        if self.closed:
            raise RuntimeError("socket closed")
        kind = message["type"]
        if kind == "ring":
            self._push({"type": "answer"})
        elif kind == "play":
            ids = [u.rsplit("/", 1)[-1].removesuffix(".wav").split(".")[0] for u in message["urls"]]
            self.heard += ids
            self._push({"type": "played", "playback_id": message["playback_id"], "ok": True})
            for pid in reversed(ids):
                if pid in self.keys:
                    self._push({"type": "dtmf", "digit": self.keys.pop(pid)})
                    break
        elif kind == "record":
            self.incoming.put_nowait({"type": "websocket.receive", "bytes": speech_wav()})

    async def close(self, code: int = 1000) -> None:
        self.closed = True

    def _push(self, message: dict) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(message)})

    def push(self, message: dict) -> None:
        self._push(message)

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect"})


async def sim_first_call(ctx: AppContext) -> tuple[str, dict[str, str]]:
    """Give a missed call from a browser phone and let the first call run to its end. The context must be a
    demo one with the browser phone, the runner and the prompts attached. Returns the call's status and the
    answers it saved. Nothing is dialled outside this process."""
    from .telephony.sim import SimPort

    sim = ctx.telephony
    if not isinstance(sim, SimPort):
        raise RuntimeError("the end-to-end check needs CT_TELEPHONY=sim")
    session = sim.create_session(
        "system:preflight", missed_calls_per_minute=ctx.policy["visitor"]["phone_missed_calls_per_minute"]
    )
    sock = ScriptedSocket(FIRST_CALL_KEYS)
    attach = asyncio.create_task(sim.attach(session.id, sock))
    try:
        await asyncio.sleep(0)
        sock.push({"type": "missed_call"})
        call_id = await _wait_for(
            lambda: ctx.db.one("SELECT id FROM calls WHERE kind='first' AND status='scheduled'"),
            "the missed call",
        )
        status = await asyncio.wait_for(
            ctx.runner.place(call_id["id"]), timeout=ctx.policy["calls"]["step_timeout_s"]
        )
        answers = {
            r["question"]: r["value"]
            for r in ctx.db.query("SELECT question, value FROM answers WHERE call_id = ?", (call_id["id"],))
        }
        return status, answers
    finally:
        sock.disconnect()
        await asyncio.wait_for(attach, 5)


async def _wait_for(find: Callable, what: str, seconds: float = 10.0):
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        found = find()
        if found:
            return found
        await asyncio.sleep(0.05)
    raise TimeoutError(f"{what} did not arrive")


# ------------------------------------------------------------------------ checks
def check_database(ctx: AppContext) -> Result:
    try:
        version = ctx.db.one("SELECT MAX(version) AS v FROM schema_version")["v"]
        clinics = ctx.db.one("SELECT COUNT(*) AS n FROM clinics")["n"]
    except Exception as e:
        return Result("database", False, f"cannot read {ctx.settings.db}: {type(e).__name__}: {e}")
    return Result("database", True, f"{ctx.settings.db} (schema {version}, {clinics} clinics)")


def check_data(ctx: AppContext) -> Result:
    wards = len(ctx.gazetteer.wards())
    clusters = len(ctx.clusters.all())
    return Result("data", True, f"policy {ctx.policy.version}, {wards} wards, {clusters} analysis areas")


def check_prompts(ctx: AppContext) -> list[Result]:
    from .api.routes_system import LANG_NAMES, languages_in_use, missing_by_language

    in_use = languages_in_use(ctx)
    out = []
    for lang, files in missing_by_language(ctx).items():
        name = LANG_NAMES[lang]
        if not files:
            out.append(Result(f"voice {lang}", True, f"{name}: every prompt has its audio"))
        else:
            first = ", ".join(files[:3])
            if lang in in_use:
                out.append(
                    Result(
                        f"voice {lang}",
                        False,
                        f"{name} is in use and {len(files)} files are missing ({first} ...)",
                    )
                )
            else:
                out.append(
                    Result(
                        f"voice {lang}", True, f"{name} is not in use yet; {len(files)} files still to add"
                    )
                )
    return out


def check_asr(settings: Settings) -> Result:
    """Says whether CT_ASR asks for the model and whether its folder and libraries are there. The model is
    not loaded: that is gigabytes on an 8 GB laptop."""
    if settings.asr == "off":
        return Result("speech", True, "CT_ASR=off: no transcripts, the review queue says so (explicit mode)")
    from .speech.asr import SpeechError, check_runtime, model_dir

    try:
        check_runtime()
    except SpeechError as e:
        return Result("speech", False, f"CT_ASR={settings.asr}: {e}")
    return Result(
        "speech", True, f"CT_ASR={settings.asr}: model folder {model_dir()} found (not loaded here)"
    )


def check_asr_loads(settings: Settings) -> Result:
    """--with-asr: load the speech model once and transcribe the first clip of data/demo/clips. Either the
    model, the libraries or the clip being missing is a failure that says which."""
    name = "speech load"
    if settings.asr == "off":
        return Result(name, False, "--with-asr was asked for, but CT_ASR=off means there is no model to load")
    from .speech.asr import SpeechError, load_transcriber

    clips = settings.data_dir / "demo" / "clips"
    first = next(iter(sorted(clips.glob("*.wav"))), None)
    began = time.monotonic()
    try:
        transcriber = load_transcriber(settings)
        transcriber.load()
        loaded = time.monotonic() - began
        if first is None:
            return Result(
                name,
                False,
                f"the model loaded in {loaded:.0f} s, but there is no .wav clip in {clips} to transcribe "
                "(make them with scripts/make_prompt_audio.py clips-plan, then generate --target clips)",
            )
        heard = transcriber.transcribe(first.read_bytes(), "hi")
    except SpeechError as e:
        return Result(name, False, str(e))
    except Exception as e:
        return Result(name, False, f"{type(e).__name__}: {e}")
    if not heard.text.strip():
        return Result(name, False, f"the model loaded in {loaded:.0f} s but heard nothing in {first.name}")
    return Result(
        name,
        True,
        f"loaded once in {loaded:.0f} s; {first.name} ({heard.seconds} s of audio) heard as: {heard.text}",
    )


async def check_telephony(ctx: AppContext) -> Result:
    """Start the phone transport once and stop it. The check connects under an application name of its own:
    Asterisk unregisters an application when any one of its event sockets closes, so a check under the real name
    would cut the link of a server that is running (found on Ubuntu 24.04 with Asterisk 20; the server would not
    take missed calls again until restarted)."""
    from .api.app import _telephony

    what = ctx.settings.telephony
    probe = _telephony(ctx.settings.model_copy(update={"ari_app": f"{ctx.settings.ari_app}-preflight"}))
    try:
        await probe.start()
    except Exception as e:
        with contextlib.suppress(Exception):
            await probe.stop()  # close what the failed start opened
        return Result("telephony", False, f"CT_TELEPHONY={what}: {type(e).__name__}: {e}")
    await probe.stop()
    return Result("telephony", True, f"CT_TELEPHONY={what}: transport starts")


def check_scheduler(ctx: AppContext) -> Result:
    """Read the lock in the server's own time: a server with its demo clock jumped stamps its beat in that
    time, and comparing with the real time would call a live server stale."""
    from .api import server_lock

    info = server_lock.read(ctx, server_lock.stored_now(ctx))
    if info is None:
        return Result(
            "scheduler", True, "no scheduler holds the lock (no server is running on this database)"
        )
    if not info.fresh:
        return Result(
            "scheduler", True, f"the lock is stale ({int(abs(info.age_s))} s): the next server takes it over"
        )
    return Result(
        "scheduler",
        True,
        f"{info.holder} holds the lock (beat {int(abs(info.age_s))} s ago): a server is running",
    )


async def check_sim_call(settings: Settings) -> Result:
    from .api.app import build_context
    from .ivr.runner import CallRunner

    with tempfile.TemporaryDirectory(prefix="chaarteeke-preflight-") as tmp:
        root = Path(tmp)
        s = settings.model_copy(
            update={
                "mode": "demo",
                "telephony": "sim",
                "asr": "off",
                "scheduler": False,
                "db": root / "preflight.db",
                "var_dir": root / "var",
            }
        )
        ctx = build_context(s)
        ctx.runner = CallRunner(ctx)
        try:
            await ctx.telephony.start()
            await ctx.runner.start()
            status, answers = await sim_first_call(ctx)
        except Exception as e:
            return Result("sim call", False, f"{type(e).__name__}: {e}")
        finally:
            await ctx.runner.stop()
            await ctx.telephony.stop()
            ctx.db.close()
    if status != "completed":
        return Result("sim call", False, f"the first call ended as {status}")
    return Result(
        "sim call",
        True,
        f"missed call, first call completed, {len(answers)} answers saved (throwaway database)",
    )


async def run(settings: Settings, with_asr: bool = False) -> bool:
    from .api.app import build_context

    results: list[Result] = []

    def show(*rs: Result) -> None:
        for r in rs:
            results.append(r)
            print(_line(r))
            sys.stdout.flush()

    try:
        ctx = build_context(settings)
    except Exception as e:
        show(Result("startup", False, f"{type(e).__name__}: {e}"))
        return False
    try:
        show(check_database(ctx), check_data(ctx), *check_prompts(ctx), check_asr(settings))
        if with_asr:
            show(await asyncio.to_thread(check_asr_loads, settings))
        show(await check_telephony(ctx), check_scheduler(ctx))
    finally:
        ctx.db.close()
    show(await check_sim_call(settings))
    failed = [r for r in results if not r.ok]
    print(
        f"{len(results) - len(failed)} of {len(results)} checks passed"
        + (f"; {len(failed)} failed" if failed else "")
    )
    return not failed
