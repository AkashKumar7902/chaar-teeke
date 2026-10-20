"""Helpers for the IVR tests: test voice files, a fast policy, a scripted browser phone, a world builder."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import math
import wave
from collections import deque
from pathlib import Path

from chaarteeke import clock, enrol
from chaarteeke.config import DATA_DIR
from chaarteeke.ivr.prompts import PATIENT_LANGS, PromptCatalogue
from chaarteeke.models import EnrolIn
from chaarteeke.policy import Policy, load_policy
from chaarteeke.telephony.sim import SimPort

REAL_PROMPTS = DATA_DIR / "prompts" / "prompts.json"
CLINIC = "cln_a"
VISIT = "09:00"


def tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0")
    return buf.getvalue()


def speech_wav(seconds: float = 1.0, rate: int = 16000, amp: int = 8000) -> bytes:
    n = int(seconds * rate)
    frames = b"".join(
        int(amp * math.sin(2 * math.pi * 300 * i / rate)).to_bytes(2, "little", signed=True) for i in range(n)
    )
    return _wav(frames, rate)


def silent_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    return _wav(b"\0\0" * int(seconds * rate), rate)


def _wav(frames: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def fast_policy(**calls) -> Policy:
    """The real policy with a short key wait and a short ring wait."""
    base = load_policy()
    raw = copy.deepcopy(base.raw)
    raw["calls"].update({"no_input_wait_s": 0.05, "ring_timeout_s": 1})
    raw["calls"].update(calls)
    return Policy(raw, "test-" + base.digest)


TEST_CLINICS = [CLINIC, "cln_demo_visitor"]


TEST_WARDS = [3, 7]


def prompts_without_qconsent() -> str:
    """The real prompts.json with FC_QCONSENT taken out, for the test that a call refuses while it is undefined."""
    raw = json.loads(REAL_PROMPTS.read_text(encoding="utf-8"))
    raw["prompts"] = [p for p in raw["prompts"] if p["id"] != "FC_QCONSENT"]
    return json.dumps(raw, ensure_ascii=False)


def _prompts_with_qconsent() -> str:
    """The real prompts.json. If FC_QCONSENT (the questions-consent line) is not in it yet, a test-only line
    is added so the flows can run."""
    raw = json.loads(REAL_PROMPTS.read_text(encoding="utf-8"))
    if "FC_QCONSENT" not in {p["id"] for p in raw["prompts"]}:
        line = "Test line: answer four questions about the bite? Press 1 to answer, 2 to skip."
        raw["prompts"].append(
            {
                "id": "FC_QCONSENT",
                "call": "first call",
                "purpose": "test double",
                "keys": {"1": "answer", "2": "skip"},
                "text": dict.fromkeys(("en", "hi", "doi", "ur"), line),
                "review_status": {},
                "notes": "",
            }
        )
    return json.dumps(raw, ensure_ascii=False)


def write_voice(cat: PromptCatalogue, audio_dir: Path, langs=(*PATIENT_LANGS, "en")) -> None:
    for lang in langs:
        for item in cat.manifest(lang):
            path = audio_dir / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(tiny_wav())


class FakeSocket:
    """The server's end of a browser WebSocket, driven by a scripted Browser."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.on_send = None
        self.closed = False

    async def receive(self) -> dict:
        return await self.incoming.get()

    async def send_json(self, message: dict) -> None:
        if self.closed:
            raise RuntimeError("socket closed")
        self.sent.append(message)
        if self.on_send:
            self.on_send(message)

    async def close(self, code: int = 1000) -> None:
        self.closed = True

    def push(self, message: dict) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(message)})

    def push_bytes(self, data: bytes) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "bytes": data})

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect"})


class Browser:
    """A person on the browser phone. `script` maps a prompt id to the keys pressed after hearing it, in
    order: a digit presses it, None stays silent. `hangup_on` ends the call when that prompt plays."""

    def __init__(
        self,
        sock: FakeSocket,
        script=None,
        recordings=None,
        *,
        answer=True,
        hangup_on=None,
        stall_on_play=False,
    ) -> None:
        self.sock = sock
        self.set_script(script)
        self.recordings = deque(recordings or [])
        self.answer = answer
        self.hangup_on = hangup_on
        self.stall_on_play = stall_on_play
        self.heard: list[list[str]] = []  # prompt ids of every play, in order
        self.urls: list[list[str]] = []
        self.records: list[dict] = []
        sock.on_send = self.on_message

    def set_script(self, script) -> None:
        self.script = {k: deque(v) for k, v in (script or {}).items()}

    @staticmethod
    def prompt_ids(urls: list[str]) -> list[str]:
        """Prompt ids in a play, one per prompt: its segment files (.0, .1 ...) count once."""
        seen: list[tuple[str, str]] = []
        for url in urls:
            parts = url.split("/")
            if "slot" in parts:
                continue
            key = (parts[-2], parts[-1].removesuffix(".wav").split(".")[0])
            if not seen or seen[-1] != key:
                seen.append(key)
        return [pid for _lang, pid in seen]

    @property
    def flat(self) -> list[str]:
        return [i for plays in self.heard for i in plays]

    def on_message(self, msg: dict) -> None:
        kind = msg["type"]
        if kind == "ring" and self.answer:
            self.sock.push({"type": "answer"})
        elif kind == "ring":
            self.sock.push({"type": "reject"})
        elif kind == "play":
            ids = self.prompt_ids(msg["urls"])
            self.heard.append(ids)
            self.urls.append(msg["urls"])
            if self.stall_on_play:
                return
            if self.hangup_on and self.hangup_on in ids:
                self.sock.push({"type": "hangup"})
                return
            self.sock.push({"type": "played", "playback_id": msg["playback_id"], "ok": True})
            for pid in reversed(ids):
                if self.script.get(pid):
                    key = self.script[pid].popleft()
                    if key is not None:
                        self.sock.push({"type": "dtmf", "digit": key})
                    break
        elif kind == "record":
            self.records.append(msg)
            self.sock.push_bytes(self.recordings.popleft() if self.recordings else b"")


class Phone:
    def __init__(self, sim: SimPort, session, sock: FakeSocket, browser: Browser, task: asyncio.Task) -> None:
        self.sim, self.session, self.sock, self.browser, self.task = sim, session, sock, browser, task

    @property
    def number(self) -> str:
        return f"sim:{self.session.id}"

    async def missed_call(self) -> None:
        self.sock.push({"type": "missed_call"})

    async def close(self) -> None:
        self.sock.disconnect()
        await asyncio.wait_for(self.task, 2)


async def open_phone(ictx, script=None, recordings=None, *, clinic_id=None, **kw) -> Phone:
    """A browser phone on the test context. `clinic_id`: opened from that clinic's own view."""
    sim: SimPort = ictx.telephony
    session = sim.create_session(
        "visitor:test",
        missed_calls_per_minute=ictx.policy["visitor"]["phone_missed_calls_per_minute"],
        clinic_id=clinic_id,
    )
    sock = FakeSocket()
    browser = Browser(sock, script, recordings, **kw)
    task = asyncio.create_task(sim.attach(session.id, sock))
    await asyncio.sleep(0)
    return Phone(sim, session, sock, browser, task)


async def until(predicate, timeout: float = 3.0):
    """Wait (in short real sleeps) until predicate() is truthy; return its value."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for the condition")


class World:
    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def clinic(self, clinic_id=CLINIC, vaccine_out=0, phone=None, visit_time=VISIT) -> str:
        """A clinic with the visit time it reads out (None: not set up) and, optionally, a phone number."""
        with self.ctx.db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO clinics(id, name, short_name, ward_no, default_regimen, default_lang,"
                " arv_status, vaccine_out, is_demo, visit_time) VALUES (?,?,?,?, 'id_trc', 'hi', 'confirmed', ?, 0, ?)",
                (clinic_id, "Test clinic", "Test", 3, vaccine_out, visit_time),
            )
            if phone:
                conn.execute("UPDATE clinics SET phone=? WHERE id=?", (phone, clinic_id))
        return clinic_id

    def pilot(self) -> None:
        """Pilot mode: a sim: missed call stays unlinked until a clinic links it (no demo auto-link)."""
        self.ctx.settings = self.ctx.settings.model_copy(update={"mode": "pilot"})

    def course(
        self,
        phone: Phone,
        *,
        regimen="id_trc",
        lang="hi",
        eligibility="adult",
        day0=None,
        clinic=CLINIC,
        register_no="R1",
    ) -> str:
        """A linked, clinic-made course whose number is the browser phone's. Returns the course id."""

        self.clinic(clinic)
        today = clock.local_today(self.ctx.settings.local_tz)
        made = enrol.create_course(
            self.ctx,
            EnrolIn(
                clinic_id=clinic,
                register_no=register_no,
                regimen_id=regimen,
                lang=lang,
                day0=(day0 or today).isoformat(),
                eligibility=eligibility,
            ),
            "staff:test",
        )
        res = enrol.record_missed_call(self.ctx, phone.number, "sim", clock.now())
        assert res.status == "unlinked"
        enrol.link(self.ctx, res.missed_call_id, made.id, "staff:test")
        return made.id

    def call(self, course_id: str, kind: str | None = None):
        sql, params = "SELECT * FROM calls WHERE course_id=?", [course_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        return self.ctx.db.query(sql + " ORDER BY scheduled_for, attempt", tuple(params))

    def add_call(self, course_id: str, kind: str, dose_no: int | None = None, attempt: int = 1) -> str:
        with self.ctx.db.transaction() as conn:
            call_id = enrol.new_call(
                conn,
                course_id=course_id,
                kind=kind,
                job_key=f"test:{kind}:{course_id}:{dose_no}:{attempt}",
                scheduled_for=clock.now(),
                dose_no=dose_no,
                attempt=attempt,
                transport="sim",
            )
        return call_id

    def answers(self, course_id: str) -> dict[str, str]:
        rows = self.ctx.db.query(
            "SELECT a.question, a.value FROM answers a JOIN calls c ON c.id=a.call_id WHERE c.course_id=? ORDER BY a.id",
            (course_id,),
        )
        return {r["question"]: r["value"] for r in rows}


def noise_wav(seconds: float, rms: float, rate: int = 8000, seed: int = 7) -> bytes:
    """Uniform noise with the given RMS on a -1..1 scale (line hiss, not speech)."""
    import random

    rng = random.Random(seed)
    peak = rms * math.sqrt(3) * 32768  # uniform in [-peak, peak] has RMS peak / sqrt(3)
    frames = b"".join(
        int(rng.uniform(-peak, peak)).to_bytes(2, "little", signed=True) for _ in range(int(seconds * rate))
    )
    return _wav(frames, rate)


def tone_burst_wav(
    total_s: float, burst_s: float, rate: int = 8000, amp: int = 8000, lead_s: float = 0.3
) -> bytes:
    """Silence, then `burst_s` of a 300 Hz tone, then silence up to `total_s`: a short word in a quiet line."""
    n, lead, burst = int(total_s * rate), int(lead_s * rate), int(burst_s * rate)
    frames = b"".join(
        int(amp * math.sin(2 * math.pi * 300 * i / rate)).to_bytes(2, "little", signed=True)
        if lead <= i < lead + burst
        else b"\0\0"
        for i in range(n)
    )
    return _wav(frames, rate)
