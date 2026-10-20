#!/usr/bin/env python3
"""The real exchange path with no phone hardware: a SIP softphone stands in for the patient's phone.

  scripts/sip_e2e.sh            the way to run it (checks the tools, then runs this file)

What runs, all on this machine (SIP and RTP on its own network address, because baresip skips loopback;
ARI and the app on loopback):

  Asterisk (a private build; the repo's telephony/asterisk configs) .. the exchange
  baresip ................................................ the patient's phone, registered to Asterisk
  the Chaar Teeke app in demo mode with CT_TELEPHONY=ari ..... AriAdapter and CallRunner, unchanged

The softphone gives a missed call, the clinic user links it over the HTTP API, the app's first call rings the
softphone, and a driver then presses the keys a patient would and speaks a recorded clip, using baresip's
control interface. The driver learns what the app is playing from a second, read-only listener on Asterisk's
event socket, so a key is pressed while its question plays and not at a guessed time. Nothing here changes
what the app does: the listener sends no command, and the app's own adapter is the only thing that places
the call, plays the prompts, reads the keys and fetches the recording.

No fallbacks: a step that cannot run stops the run and says which one. Nothing stands in for it.

The speech model is off by default (CT_ASR=off, the labelled mode). With --asr (or CT_TEST_SIP_ASR=1) the app
runs with CT_ASR=indicconformer and CT_ASR_MODEL_DIR from the environment (there is no default folder), the
rig waits until the app says the model is loaded, and the place the patient speaks crosses the exchange as 8 kHz
RTP, is recorded by Asterisk, read by the model, matched (with keyword spotting) and placed or queued. The app
deletes the clip and the words the moment it places a report by itself, so the rig keeps a hard link to the file
the app saved (scripts/sip_e2e_heard.py reads it afterwards, with the model, to show what was heard).

The softphone is the one stand-in, and the report says what it cannot show: no cellular network, no SIM, no
caller-ID delay, no billing, no real handset audio. See docs/TELEPHONY.md.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import resource
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "telephony" / "asterisk" / "etc" / "asterisk"
CONVERT = ROOT / "telephony" / "scripts" / "convert_prompts.sh"
DEFAULT_PREFIX = Path.home() / "chaar-teeke-build" / "work" / "asterisk-prefix"

# What a patient presses, by the question that is playing (the numbers are the menu keys in ivr/flow.py).
# FC_PLACE is not here: the patient speaks to it (see Driver.on_event).
KEYS_BY_PROMPT = {
    "FC_CONSENT_Q": "1",  # yes, send me reminders
    "FC_QCONSENT": "1",  # yes, ask me the questions
    "FC_PLACETYPE": "1",  # home or street
    "FC_ANIMAL": "1",  # a street dog
    "FC_NOTCH": "1",  # the right-ear notch was seen
    "FC_SICK": "2",  # no signs of rabies
}
EXPECTED_ANSWERS = {
    "consent_reminders": "yes",
    "consent_questions": "yes",
    "place": "recorded",
    "place_type": "home_street",
    "animal": "notched",
    "signs": "no",
}
PLACE_PROMPT = "FC_PLACE"
LISTEN_BEFORE_KEY_S = 1.0  # how long a patient listens to a question before pressing the key
SPEAK_AFTER_BEEP_S = (
    0.5  # the beep is 0.4 s (telephony/asterisk/sounds/beep.wav); a patient speaks just after it
)
SILENCE_S = 240  # the softphone's line is silent until the driver gives it the clip
NOT_RUN = "not run"


class RigError(Exception):
    """A step of the rig that could not run. The message says which, in plain words."""


# --------------------------------------------------------------------------- settings
@dataclass
class Plan:
    prefix: Path
    run_dir: Path
    lang: str = "hi"
    sip_port: int = 15060  # Asterisk's SIP (UDP, on this machine's own address)
    phone_port: int = 15070  # the softphone's SIP
    ctrl_port: int = 14444  # the softphone's control interface
    ari_port: int = 18088
    app_port: int = 18080
    rtp_start: int = 31000
    rtp_end: int = 31100
    patient_number: str = "+919999909995"  # in the reserved +91 99999 0xxxx range, never a real phone
    clip: Path = ROOT / "data" / "demo" / "clips" / "1.wav"
    asr: bool = False  # opt in: the app runs with the speech model (CT_TEST_SIP_ASR=1 or --asr)
    model_dir: str = ""  # CT_ASR_MODEL_DIR for the app; required with asr, there is no default
    expect_ward: int | None = (
        None  # with asr: the ward the clip names; the run fails if it is not set by itself
    )
    baresip: str = "baresip"
    keep: bool = False
    local_tz: str = "Asia/Kolkata"  # CT_LOCAL_TZ for the app; scenario() may change it (see choose_local_tz)
    clip_seconds: float = 0.0  # set by prepare_answer
    lan_address: str = (
        ""  # this machine's own IPv4 address on its default route; filled in by find_lan_address
    )
    ari_password: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    softphone_password: str = field(default_factory=lambda: secrets.token_urlsafe(12))

    @property
    def ast(self) -> Path:
        return self.run_dir / "ast"

    @property
    def conf(self) -> Path:
        return self.ast / "asterisk.conf"

    @property
    def asterisk_bin(self) -> Path:
        return self.prefix / "sbin" / "asterisk"

    @property
    def ari_url(self) -> str:
        return f"http://127.0.0.1:{self.ari_port}"

    @property
    def app_url(self) -> str:
        return f"http://127.0.0.1:{self.app_port}"

    @property
    def sip_uri(self) -> str:
        return f"sip:100@{self.lan_address}:{self.sip_port}"

    @property
    def digits(self) -> str:
        return re.sub(r"\D", "", self.patient_number)[-10:]


def plan_from_env(env: dict[str, str] | None = None) -> Plan:
    e = os.environ if env is None else env
    prefix = Path(e.get("ASTERISK_PREFIX") or DEFAULT_PREFIX)
    run_dir = Path(e.get("CT_SIP_E2E_DIR") or ROOT / "var" / "sip-e2e") / time.strftime("%Y%m%d-%H%M%S")
    plan = Plan(prefix=prefix, run_dir=run_dir)
    for name, key in (
        ("sip_port", "CT_SIP_E2E_SIP_PORT"),
        ("phone_port", "CT_SIP_E2E_PHONE_PORT"),
        ("ctrl_port", "CT_SIP_E2E_CTRL_PORT"),
        ("ari_port", "CT_SIP_E2E_ARI_PORT"),
        ("app_port", "CT_SIP_E2E_APP_PORT"),
        ("rtp_start", "CT_SIP_E2E_RTP_START"),
        ("rtp_end", "CT_SIP_E2E_RTP_END"),
    ):
        if e.get(key):
            setattr(plan, name, int(e[key]))
    plan.lang = e.get("CT_SIP_E2E_LANG", plan.lang)
    plan.asr = e.get("CT_TEST_SIP_ASR") == "1"
    plan.model_dir = e.get("CT_ASR_MODEL_DIR", "")
    if e.get("CT_TEST_SIP_EXPECT_WARD"):
        plan.expect_ward = int(e["CT_TEST_SIP_EXPECT_WARD"])
    plan.keep = e.get("CT_SIP_E2E_KEEP") == "1"
    return plan


# --------------------------------------------------------------------------- pure helpers
def netstring(payload: bytes) -> bytes:
    return str(len(payload)).encode() + b":" + payload + b","


def read_netstrings(buffer: bytearray) -> list[bytes]:
    """Take every whole netstring off the front of `buffer` and leave a partial one in it."""
    out: list[bytes] = []
    while True:
        colon = buffer.find(b":")
        if colon < 0:
            return out
        head = bytes(buffer[:colon])
        if not head.isdigit():
            raise RigError(
                f"the softphone's control socket sent something that is not a netstring: {head[:20]!r}"
            )
        size = int(head)
        end = colon + 1 + size
        if len(buffer) < end + 1:
            return out
        if buffer[end : end + 1] != b",":
            raise RigError("the softphone's control socket sent a netstring with no closing comma")
        out.append(bytes(buffer[colon + 1 : end]))
        del buffer[: end + 1]


_SEGMENT = re.compile(r"\.\d+$")


def prompt_id(media_uri: str | None) -> str | None:
    """'sound:chaar/hi/FC_NOTICE.0' -> 'FC_NOTICE'. Slot clips ('chaar/hi/slot/...') have no prompt id."""
    if not media_uri or not media_uri.startswith("sound:chaar/"):
        return None
    parts = media_uri.removeprefix("sound:").split("/")
    if len(parts) != 3:
        return None
    return _SEGMENT.sub("", parts[2])


def substitute_once(text: str, pattern: str, new: str, what: str) -> str:
    """Replace the one place a shipped file says `pattern`. A file that no longer has it is an error, not a
    silent no-op, so the run can never use a setting other than the one it reports."""
    found = re.findall(pattern, text, flags=re.MULTILINE)
    if len(found) != 1:
        raise RigError(f"the shipped Asterisk config has {len(found)} places for {what}, expected 1")
    return re.sub(pattern, lambda _m: new, text, flags=re.MULTILINE)


def render_pjsip(text: str, plan: Plan) -> str:
    """The shipped file with four values changed. baresip cannot use 127.0.0.1 (it ignores loopback addresses),
    so for this test Asterisk's SIP and audio use this machine's own address instead of loopback. The softphone
    exchange then only accepts SIP from that address (an ACL), and its password is made for this run."""
    text = substitute_once(
        text, r"^bind=127\.0\.0\.1:\d+", f"bind={plan.lan_address}:{plan.sip_port}", "the SIP bind"
    )
    text = substitute_once(
        text,
        r"^callerid=\"Test Patient\" <\+?\d+>",
        f'callerid="Test Patient" <{plan.patient_number}>',
        "the test patient's caller ID",
    )
    text = substitute_once(
        text,
        r"^password=dev-only-softphone$",
        f"password={plan.softphone_password}",
        "the softphone password",
    )
    acl = f"\n[test-acl]\ntype=acl\ndeny=0.0.0.0/0.0.0.0\npermit={plan.lan_address}/255.255.255.255\n"
    return text + acl  # a pjsip.conf acl section applies to every incoming SIP request


def render_rtp(text: str, plan: Plan) -> str:
    text = substitute_once(text, r"^rtpstart=\d+", f"rtpstart={plan.rtp_start}", "rtpstart")
    return substitute_once(text, r"^rtpend=\d+", f"rtpend={plan.rtp_end}", "rtpend")


def render_http(text: str, plan: Plan) -> str:
    return substitute_once(text, r"^bindport=\d+", f"bindport={plan.ari_port}", "the ARI port")


def render_ari(text: str, plan: Plan) -> str:
    return substitute_once(text, r"^password=.*$", f"password={plan.ari_password}", "the ARI password")


def render_asterisk_conf(plan: Plan) -> str:
    ast, pre = plan.ast, plan.prefix
    return "\n".join(
        [
            "[directories]",
            f"astcachedir => {ast / 'cache'}",
            f"astetcdir => {ast / 'etc' / 'asterisk'}",
            f"astmoddir => {pre / 'lib' / 'asterisk' / 'modules'}",
            f"astvarlibdir => {ast / 'lib'}",
            f"astdbdir => {ast / 'lib'}",
            f"astkeydir => {pre / 'var' / 'lib' / 'asterisk'}",
            f"astdatadir => {ast / 'lib'}",
            f"astagidir => {ast / 'lib' / 'agi-bin'}",
            f"astspooldir => {ast / 'spool'}",
            f"astrundir => {ast / 'run'}",
            f"astlogdir => {ast / 'log'}",
            f"astsbindir => {pre / 'sbin'}",
            "",
            "[options]",
            "documentation_language = en_US",
            "",
        ]
    )


def render_baresip_config(plan: Plan, module_dir: Path) -> str:
    bs = plan.run_dir / "baresip"
    return "\n".join(
        [
            f"audio_player aufile,{bs / 'heard.wav'}",
            f"audio_source aufile,{bs / 'silence.wav'}",
            f"audio_alert aufile,{bs / 'alert.wav'}",
            f"module_path {module_dir}",
            "module g711.so",
            "module aufile.so",
            "module account.so",
            "module menu.so",
            "module ctrl_tcp.so",
            f"ctrl_tcp_listen 127.0.0.1:{plan.ctrl_port}",
            # baresip skips loopback addresses, so the softphone and Asterisk use this machine's own address
            f"sip_listen {plan.lan_address}:{plan.phone_port}",
            "sip_transports udp",
            "",
        ]
    )


def render_baresip_account(plan: Plan) -> str:
    return (
        f"<sip:softphone@{plan.lan_address}:{plan.sip_port};transport=udp>"
        f";auth_pass={plan.softphone_password};regint=60;answermode=auto;audio_codecs=PCMU/8000/1\n"
    )


def find_lan_address() -> str:
    """This machine's IPv4 address on its default route (nothing is sent). The softphone needs an address that
    is not loopback, so a machine with no network address cannot run this test."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # a documentation address; connect() on UDP only picks a route
            address = s.getsockname()[0]
        except OSError as e:
            raise RigError(f"this machine has no network address for the softphone to use ({e})") from e
    if address.startswith("127."):
        raise RigError("this machine has no network address for the softphone to use (only loopback)")
    return address


def number_forms(number: str) -> list[str]:
    digits = re.sub(r"\D", "", number)[-10:]
    return [digits, f"{digits[:5]} {digits[5:]}", f"{digits[:5]}-{digits[5:]}"]


def find_number_leaks(paths: list[Path], number: str) -> list[str]:
    """Where the test patient's number appears in these files (as 'file:line', never the line)."""
    forms = number_forms(number)
    hits = []
    for path in paths:
        if not path.is_file():
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if any(f in line for f in forms):
                hits.append(f"{path.name}:{n}")
    return hits


def write_silence(path: Path, seconds: int, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * rate * seconds)


def wav_stats(data: bytes) -> tuple[float, float]:
    """(seconds, root-mean-square level 0..1) of a PCM16 mono WAV."""
    import io

    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise RigError("a recording is not 16-bit PCM")
        frames = w.readframes(w.getnframes())
        rate = w.getframerate()
    n = len(frames) // 2
    if n == 0:
        return 0.0, 0.0
    import array

    samples = array.array("h")
    samples.frombytes(frames[: n * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    rms = (sum(s * s for s in samples) / n) ** 0.5 / 32768
    return n / rate, rms


# --------------------------------------------------------------------------- processes
class Procs:
    """The processes this run started, each stopped by its own PID and no other."""

    def __init__(self, run_dir: Path) -> None:
        self.items: list[tuple[str, subprocess.Popen]] = []
        self.pidfile = run_dir / "pids"

    def start(
        self, name: str, argv: list[str], log: Path, env: dict[str, str] | None = None, cwd=None, pre=None
    ):
        log.parent.mkdir(parents=True, exist_ok=True)
        out = open(log, "ab")  # noqa: SIM115 (lives as long as the process)
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env, cwd=cwd,
            start_new_session=True, preexec_fn=pre,
        )  # fmt: skip
        out.close()
        self.items.append((name, proc))
        with open(self.pidfile, "a") as f:
            f.write(f"{proc.pid} {name}\n")
        return proc

    def stop(self, name: str, grace_s: float = 8.0) -> None:
        for n, proc in list(self.items):
            if n != name or proc.poll() is not None:
                continue
            proc.terminate()
            try:
                proc.wait(grace_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)

    def stop_all(self) -> None:
        for name, _ in reversed(self.items):
            self.stop(name)


def raise_open_files() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 10240 if hard == resource.RLIM_INFINITY else min(10240, hard)
    resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))


def port_is_free(port: int, kind: int = socket.SOCK_STREAM, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, kind) as s:
        if kind == socket.SOCK_STREAM:
            s.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
            )  # a port in TIME_WAIT is free to listen on
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def check_ports(plan: Plan) -> None:
    lan = plan.lan_address or find_lan_address()
    busy = [
        f"{name} {port}"
        for name, port, kind, host in (
            ("Asterisk SIP", plan.sip_port, socket.SOCK_DGRAM, lan),
            ("the softphone's SIP", plan.phone_port, socket.SOCK_DGRAM, lan),
            ("the softphone's control", plan.ctrl_port, socket.SOCK_STREAM, "127.0.0.1"),
            ("ARI", plan.ari_port, socket.SOCK_STREAM, "127.0.0.1"),
            ("the app", plan.app_port, socket.SOCK_STREAM, "127.0.0.1"),
            ("RTP", plan.rtp_start, socket.SOCK_DGRAM, lan),
        )
        if not port_is_free(port, kind, host)
    ]
    if busy:
        raise RigError(
            f"these ports are already in use: {', '.join(busy)} (change them with CT_SIP_E2E_*_PORT)"
        )


# --------------------------------------------------------------------------- Asterisk
def build_asterisk(plan: Plan) -> None:
    """The prefix's stock config files, then the repo's shipped ones on top (four of them with one value
    changed each, listed in the report), and the private directories one run needs."""
    if not plan.asterisk_bin.is_file():
        raise RigError(
            f"no Asterisk at {plan.asterisk_bin} (set ASTERISK_PREFIX to a private Asterisk build)"
        )
    if not plan.lan_address:
        plan.lan_address = find_lan_address()
    etc = plan.ast / "etc" / "asterisk"
    etc.mkdir(parents=True)
    stock = plan.prefix / "etc" / "asterisk"
    for f in stock.iterdir():
        if f.is_file():
            shutil.copy2(f, etc / f.name)
    shipped = {p.name: p.read_text() for p in SHIPPED.glob("*.conf")}
    shipped.pop("quectel.conf", None)  # the dongle driver is not built here
    shipped["pjsip.conf"] = render_pjsip(shipped["pjsip.conf"], plan)
    shipped["rtp.conf"] = render_rtp(shipped["rtp.conf"], plan)
    shipped["http.conf"] = render_http(shipped["http.conf"], plan)
    shipped["ari.conf"] = render_ari(shipped["ari.conf"], plan)
    for name, text in shipped.items():
        (etc / name).write_text(text)
    plan.conf.write_text(render_asterisk_conf(plan))
    for d in ("cache", "spool", "run", "log"):
        (plan.ast / d).mkdir(exist_ok=True)
    lib = plan.ast / "lib"
    lib.mkdir()
    (lib / "sounds" / "chaar").mkdir(parents=True)
    # the 'beep' that "After the beep, say ..." in the first call's place question promises (the adapter asks
    # for beep=true); Asterisk's own core sounds are not installed in this private build
    shutil.copy2(ROOT / "telephony" / "asterisk" / "sounds" / "beep.wav", lib / "sounds" / "beep.wav")
    (plan.ast / "spool" / "recording").mkdir(parents=True)
    for child in (plan.prefix / "var" / "lib" / "asterisk").iterdir():
        if child.name not in ("sounds", "astdb.sqlite3"):
            (lib / child.name).symlink_to(child)


def convert_prompts(plan: Plan) -> tuple[int, float, bool]:
    """The app's own prompt audio for the call's language, through telephony/scripts/convert_prompts.sh.
    Returns (files, seconds, reused). The converted files are kept next to the runs under a name made from the
    content of every source file and of the script, so a changed prompt or script is converted afresh and the
    same input is not converted twice."""
    src = ROOT / "data" / "prompts" / "audio" / plan.lang
    if not src.is_dir():
        raise RigError(f"no prompt audio for '{plan.lang}' in {src}")
    digest = hashlib.sha256(CONVERT.read_bytes())
    for f in sorted(src.rglob("*")):
        if f.is_file():
            digest.update(str(f.relative_to(src)).encode() + b"\0" + f.read_bytes())
    kept = plan.run_dir.parent / f"sounds-{plan.lang}-{digest.hexdigest()[:16]}"
    began = time.monotonic()
    reused = (kept / plan.lang).is_dir()
    if not reused:
        staging = plan.run_dir / "prompts_in"
        shutil.copytree(src, staging / plan.lang)
        building = plan.run_dir.parent / f".building-{os.getpid()}"
        shutil.rmtree(building, ignore_errors=True)
        done = subprocess.run(
            ["bash", str(CONVERT), str(staging), str(building)], capture_output=True, text=True, timeout=900
        )
        if done.returncode != 0:
            raise RigError(f"convert_prompts.sh failed: {done.stderr.strip()[:300]}")
        shutil.rmtree(staging)
        os.replace(building, kept)
    sounds = plan.ast / "lib" / "sounds" / "chaar"
    (sounds / plan.lang).symlink_to(kept / plan.lang)
    return len(list((kept / plan.lang).rglob("*.wav"))), time.monotonic() - began, reused


def ast_cli(plan: Plan, command: str) -> str:
    done = subprocess.run(
        [str(plan.asterisk_bin), "-C", str(plan.conf), "-rx", command],
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    return done.stdout


def ari_header(plan: Plan) -> dict[str, str]:
    import base64

    token = base64.b64encode(f"chaar:{plan.ari_password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def start_asterisk(plan: Plan, procs: Procs, session: aiohttp.ClientSession) -> float:
    began = time.monotonic()
    procs.start(
        "asterisk",
        [str(plan.asterisk_bin), "-f", "-C", str(plan.conf)],
        plan.ast / "log" / "console.log",
        cwd=plan.ast,
        pre=raise_open_files,
    )
    headers = ari_header(plan)
    for _ in range(600):
        proc = procs.items[-1][1]
        if proc.poll() is not None:
            raise RigError(f"Asterisk exited at start (code {proc.returncode}); see {plan.ast / 'log'}")
        try:
            async with session.get(f"{plan.ari_url}/ari/asterisk/info", headers=headers) as r:
                if r.status == 200:
                    break
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.5)
    else:
        raise RigError("Asterisk did not answer on ARI within 300 seconds")
    return time.monotonic() - began


def stop_asterisk(plan: Plan, procs: Procs) -> None:
    with contextlib.suppress(Exception):
        ast_cli(plan, "core stop now")
    for _, proc in procs.items:
        if proc.args and str(plan.asterisk_bin) in map(str, proc.args):
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
    procs.stop("asterisk")


# --------------------------------------------------------------------------- the softphone
class Softphone:
    """baresip as the patient's phone, driven over its TCP control interface (JSON in netstrings)."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.events: list[tuple[float, dict]] = []
        self._waiters: list[tuple[Callable[[dict], bool], asyncio.Future]] = []
        self._replies: dict[str, asyncio.Future] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._n = 0

    @staticmethod
    def module_dir(binary: str) -> Path:
        exe = shutil.which(binary)
        if not exe:
            raise RigError("baresip is not installed (brew install baresip)")
        found = Path(exe).resolve().parent.parent / "lib" / "baresip" / "modules"
        if not (found / "ctrl_tcp.so").is_file():
            raise RigError(f"baresip has no ctrl_tcp module under {found}")
        return found

    def write_files(self, modules: Path) -> Path:
        d = self.plan.run_dir / "baresip"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config").write_text(render_baresip_config(self.plan, modules))
        (d / "accounts").write_text(render_baresip_account(self.plan))
        write_silence(d / "silence.wav", SILENCE_S)
        write_silence(d / "alert.wav", 1)  # the ring tone file the phone would write; it is never played
        return d

    async def connect(self) -> None:
        for _ in range(100):
            try:
                reader, self._writer = await asyncio.open_connection("127.0.0.1", self.plan.ctrl_port)
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise RigError("the softphone's control port did not open")
        self._reader_task = asyncio.create_task(self._read(reader), name="softphone-events")

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        if self._writer:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()

    async def _read(self, reader: asyncio.StreamReader) -> None:
        buffer = bytearray()
        while chunk := await reader.read(65536):
            buffer.extend(chunk)
            for raw in read_netstrings(buffer):
                msg = json.loads(raw)
                now = time.monotonic()
                if msg.get("event"):
                    self.events.append((now, msg))
                    for check, fut in list(self._waiters):
                        if not fut.done() and check(msg):
                            fut.set_result((now, msg))
                elif (token := msg.get("token")) in self._replies:
                    self._replies.pop(token).set_result(msg)

    async def command(self, name: str, params: str = "", timeout: float = 5.0) -> dict:
        self._n += 1
        token = f"t{self._n}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._replies[token] = fut
        self._writer.write(
            netstring(json.dumps({"command": name, "params": params, "token": token}).encode())
        )
        await self._writer.drain()
        reply = await asyncio.wait_for(fut, timeout)
        if not reply.get("ok", True):
            raise RigError(f"the softphone refused '{name}': {reply.get('data', '')[:200]}")
        return reply

    async def wait_for(self, kind: str, timeout: float, after: float = 0.0) -> tuple[float, dict]:
        """The first event of this type at or after the monotonic time `after`."""
        for at, ev in self.events:
            if ev.get("type") == kind and at >= after:
                return at, ev
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (lambda ev: ev.get("type") == kind, fut)
        self._waiters.append(entry)
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError as e:
            raise RigError(f"the softphone saw no {kind} within {timeout:g} s") from e
        finally:
            self._waiters.remove(entry)

    def seen(self, kind: str, after: float = 0.0) -> list[tuple[float, dict]]:
        return [(at, ev) for at, ev in self.events if ev.get("type") == kind and at >= after]


# --------------------------------------------------------------------------- the listener on ARI
@dataclass
class Seen:
    t: float  # monotonic seconds on this machine
    kind: str
    channel: str | None = None
    detail: dict = field(default_factory=dict)


class AriListener:
    """A second, read-only connection to Asterisk's event socket, as the app 'ctwatch'. It never sends a command.
    It keeps only what the checks need (no caller numbers), and calls `on_event` for each event so the driver
    can press a key while a question plays."""

    def __init__(self, plan: Plan, session: aiohttp.ClientSession, on_event: Callable[[Seen], None]) -> None:
        self.plan, self.session, self.on_event = plan, session, on_event
        self.seen: list[Seen] = []
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self.error: BaseException | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ari-listener")
        await asyncio.wait_for(self._ready.wait(), 10)
        if self.error:
            raise RigError(f"the listener could not join ARI: {self.error}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        # A different app name: Asterisk gives a named app's events to one connection only, and that one is the
        # app's own adapter. subscribeAll makes this listener see the same channels without controlling them.
        url = (
            f"{self.plan.ari_url}/ari/events?app=ctwatch&subscribeAll=true"
            f"&api_key=chaar:{self.plan.ari_password}"
        )
        try:
            async with self.session.ws_connect(url, heartbeat=20) as ws:
                self._ready.set()
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._take(json.loads(msg.data))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = e
            self._ready.set()

    def _take(self, ev: dict) -> None:
        kind = ev.get("type", "")
        ch = ev.get("channel") or {}
        detail: dict = {}
        if kind == "ChannelCreated":
            dial = ch.get("dialplan") or {}
            detail = {"name": ch.get("name"), "context": dial.get("context"), "exten": dial.get("exten")}
        elif kind == "StasisStart":
            detail = {"args": ev.get("args"), "state": ch.get("state")}
        elif kind in ("PlaybackStarted", "PlaybackContinuing", "PlaybackFinished"):
            pb = ev.get("playback") or {}
            detail = {
                "id": pb.get("id"), "media": pb.get("media_uri"), "next": pb.get("next_media_uri"),
                "state": pb.get("state"), "target": pb.get("target_uri"),
            }  # fmt: skip
        elif kind == "ChannelDtmfReceived":
            detail = {"digit": ev.get("digit"), "duration_ms": ev.get("duration_ms")}
        elif kind in ("RecordingStarted", "RecordingFinished", "RecordingFailed"):
            rec = ev.get("recording") or {}
            detail = {
                "name": rec.get("name"), "state": rec.get("state"), "duration": rec.get("duration"),
                "talking": rec.get("talking_duration"), "silence": rec.get("silence_duration"),
                "target": rec.get("target_uri"), "cause": rec.get("cause"),
            }  # fmt: skip
        elif kind == "ChannelStateChange":
            detail = {"state": ch.get("state")}
        elif kind == "ChannelDestroyed":
            detail = {"cause": ev.get("cause"), "cause_txt": ev.get("cause_txt"), "state": ch.get("state")}
        else:
            return
        item = Seen(
            time.monotonic(),
            kind,
            ch.get("id") or (detail.get("target") or "").removeprefix("channel:") or None,
            detail,
        )
        self.seen.append(item)
        self.on_event(item)

    def of(self, kind: str) -> list[Seen]:
        return [s for s in self.seen if s.kind == kind]


# --------------------------------------------------------------------------- the app
class Api:
    """The app's HTTP API as one signed-in staff user."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.http = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.csrf = ""

    async def close(self) -> None:
        await self.http.close()

    async def sign_in(self, username: str, password: str) -> None:
        me = await self.post("/api/auth/signin", {"username": username, "password": password})
        self.csrf = me["csrf"]

    async def _send(self, method: str, path: str, body: dict | None = None) -> bytes:
        headers = {"X-CT-CSRF": self.csrf} if self.csrf else {}
        async with self.http.request(method, self.base + path, json=body, headers=headers) as r:
            data = await r.read()
            if r.status >= 300:
                raise RigError(f"{method} {path} answered {r.status}: {data.decode(errors='replace')[:200]}")
            return data

    async def get(self, path: str) -> dict | list | None:
        data = await self._send("GET", path)
        return json.loads(data) if data else None

    async def get_bytes(self, path: str) -> bytes:
        return await self._send("GET", path)

    async def post(self, path: str, body: dict | None = None) -> dict:
        data = await self._send("POST", path, body or {})
        return json.loads(data) if data else {}


class App:
    """The Chaar Teeke server as a child process: demo mode, CT_TELEPHONY=ari against this run's Asterisk, its
    own port, its own database and var folder, and no speech model (CT_ASR=off, the labelled mode) unless the
    run asked for it (plan.asr)."""

    def __init__(self, plan: Plan, procs: Procs) -> None:
        self.plan, self.procs = plan, procs
        self.dir = plan.run_dir / "app"
        self.log = self.dir / "app.log"

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("CT_")}
        env.update(
            CT_MODE="demo",
            CT_TELEPHONY="ari",
            CT_ARI_URL=self.plan.ari_url,
            CT_ARI_USER="chaar",
            CT_ARI_PASSWORD=self.plan.ari_password,
            CT_ARI_ENDPOINT="PJSIP/softphone",
            CT_ASR="indicconformer" if self.plan.asr else "off",
            CT_DB=str(self.dir / "chaarteeke.db"),
            CT_VAR_DIR=str(self.dir / "var"),
            CT_HOST="127.0.0.1",
            CT_PORT=str(self.plan.app_port),
            CT_LOCAL_TZ=self.plan.local_tz,
        )
        if self.plan.asr:
            env["CT_ASR_MODEL_DIR"] = self.plan.model_dir
        return env

    def seed(self) -> dict[str, str]:
        """The demo data, which brings the clinics and the staff logins. Returns username -> password."""
        self.dir.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(
            [sys.executable, "-m", "chaarteeke.main", "seed-demo"],
            cwd=ROOT, env=self.env(), capture_output=True, text=True, timeout=300,
        )  # fmt: skip
        if done.returncode != 0:
            raise RigError(f"seed-demo failed: {(done.stderr or done.stdout).strip()[:300]}")
        logins = {}
        for line in done.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in ("clinic", "reviewer", "admin"):
                logins[parts[0]] = parts[1]
        if set(logins) != {"clinic", "reviewer", "admin"}:
            raise RigError("seed-demo did not print the clinic, reviewer and admin logins")
        return logins

    def start(self) -> None:
        self.procs.start(
            "app", [sys.executable, "-m", "chaarteeke.main", "serve"], self.log, env=self.env(), cwd=ROOT
        )

    async def wait_up(self, api: Api) -> None:
        proc = self.procs.items[-1][1]
        for _ in range(240):
            if proc.poll() is not None:
                raise RigError(f"the app exited at start (code {proc.returncode}); see {self.log}")
            try:
                await api.get("/api/mode")
                return
            except (RigError, aiohttp.ClientError):
                await asyncio.sleep(0.5)
        raise RigError("the app did not answer within 120 seconds")

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.dir / 'chaarteeke.db'}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn


class ClipKeeper:
    """Keeps a hard link to every file the app saves in its clips folder, so the recording can still be read
    after the app deletes it (it deletes the clip at once when it places a report by itself). It only looks and
    links: it never writes into the app's folder and never touches a file's contents. A link is the same file
    under a second name, so what it keeps is exactly what the app read."""

    EVERY_S = 0.005

    def __init__(self, watched: Path, keep_in: Path, keep_names: bool = False) -> None:
        self.watched, self.keep_in, self.keep_names = watched, keep_in, keep_names
        self.kept: list[Path] = []
        self.error: str | None = None
        self._seen: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="clip-keeper", daemon=True)

    def start(self) -> None:
        self.keep_in.mkdir(parents=True, exist_ok=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sweep()
            time.sleep(self.EVERY_S)
        self.sweep()

    def sweep(self) -> None:
        with self._lock:
            self._sweep()

    def _sweep(self) -> None:
        try:
            names = [e for e in os.scandir(self.watched) if e.is_file(follow_symlinks=False)]
        except FileNotFoundError:
            return  # the app makes the folder when it saves its first clip
        except OSError as e:
            self.error = f"{type(e).__name__}: {e}"
            return
        for entry in names:
            try:
                inode = entry.stat(follow_symlinks=False).st_ino
                if inode in self._seen:
                    continue
                target = self.keep_in / (entry.name if self.keep_names else f"{len(self.kept) + 1}.wav")
                os.link(entry.path, target)
                self._seen.add(inode)
                self.kept.append(target)
            except FileNotFoundError:
                continue  # renamed or deleted between the listing and the link: the next sweep sees the new name
            except OSError as e:
                self.error = f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- the patient
def prepare_answer(plan: Plan) -> Path:
    """The place answer the patient speaks: the clip, then silence, so the phone keeps sending audio after the
    words (a real handset does, and Asterisk ends a recording on 'silence' only from audio it receives)."""
    out = plan.run_dir / "baresip" / "place_answer.wav"
    plain = plan.run_dir / "baresip" / "clip_8k.wav"
    done = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(plan.clip), "-ac", "1", "-ar", "8000",
         "-c:a", "pcm_s16le", str(plain)],
        capture_output=True, text=True,
    )  # fmt: skip
    if done.returncode != 0:
        raise RigError(f"ffmpeg could not read the clip {plan.clip.name}: {done.stderr.strip()[:200]}")
    with wave.open(str(plain)) as w:
        speech = w.readframes(w.getnframes())
        seconds = w.getnframes() / w.getframerate()
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(speech + b"\x00\x00" * 8000 * 30)
    plain.unlink()
    plan.clip_seconds = seconds
    return out


@dataclass
class Press:
    t: float
    digit: str
    prompt: str


class Driver:
    """What the patient does. It reacts to what Asterisk reports the app is playing: a key goes in while its
    question plays, and the clip is spoken when the recording starts."""

    def __init__(self, plan: Plan, phone: Softphone, answer: Path) -> None:
        self.plan, self.phone, self.answer = plan, phone, answer
        self.todo = dict(KEYS_BY_PROMPT)
        self.presses: list[Press] = []
        self.spoken_at: float | None = None
        self.problems: list[str] = []
        self._tasks: set[asyncio.Task] = set()
        self._place_heard = False

    def on_event(self, seen: Seen) -> None:
        if seen.kind in ("PlaybackStarted", "PlaybackContinuing"):
            pid = prompt_id(seen.detail.get("media"))
            if pid == PLACE_PROMPT:
                self._place_heard = True
            elif pid in self.todo:
                self._later(LISTEN_BEFORE_KEY_S, self._press(self.todo.pop(pid), pid))
        elif seen.kind == "RecordingStarted" and self._place_heard and self.spoken_at is None:
            self.spoken_at = time.monotonic()  # set now so a second start cannot speak twice
            self._later(SPEAK_AFTER_BEEP_S, self._speak())

    def _later(self, delay: float, coro) -> None:
        async def run() -> None:
            await asyncio.sleep(delay)
            try:
                await coro
            except Exception as e:
                self.problems.append(f"{type(e).__name__}: {e}")

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _press(self, digit: str, prompt: str) -> None:
        self.presses.append(Press(time.monotonic(), digit, prompt))
        await self.phone.command("sndcode", digit)

    async def _speak(self) -> None:
        await self.phone.command("ausrc", f"aufile,{self.answer}")

    async def settle(self) -> None:
        await asyncio.gather(*self._tasks, return_exceptions=True)


# --------------------------------------------------------------------------- the report
@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    measures: dict[str, object] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    began: float = field(default_factory=time.monotonic)

    def step(self, text: str) -> None:
        line = f"[{time.monotonic() - self.began:6.1f} s] {text}"
        self.steps.append(line)
        print(line, flush=True)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, bool(ok), detail))
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}", flush=True)
        return bool(ok)

    def measure(self, name: str, value: object) -> None:
        self.measures[name] = value

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)

    def as_json(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in self.checks],
            "measures": self.measures,
            "notes": self.notes,
            "steps": self.steps,
        }


def seconds(a: float, b: float) -> float:
    return round(b - a, 2)


# --------------------------------------------------------------------------- the run
RECORD_MODULES = (
    "cdr_csv", "cdr_custom", "cdr_manager", "cdr_sqlite3_custom", "cel_custom", "cel_manager",
    "cel_sqlite3_custom", "res_cdrel_custom",
)  # fmt: skip
QUESTION_PROMPTS = ("FC_HELLO", "FC_NOTICE", *KEYS_BY_PROMPT, PLACE_PROMPT)


async def poll(find, what: str, timeout: float, every: float = 0.1):
    """Call `find` (a coroutine function) until it returns something true; its value is returned."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found = await find()
        if found:
            return found
        await asyncio.sleep(every)
    raise RigError(f"{what} did not happen within {timeout:g} s")


def choose_local_tz(now: datetime, window: tuple[str, str], default: str = "Asia/Kolkata") -> str:
    """The app's CT_LOCAL_TZ for this run. The calling window (policy calls.window_*) is kept as it is; when the
    clock on this machine is outside it, or too near its edge for a three-minute run, the app is told to count
    its local time in a zone where it is the early afternoon, instead of moving its clock or its policy."""

    def minutes(text: str) -> int:
        hh, mm = text.split(":")
        return int(hh) * 60 + int(mm)

    start, end = minutes(window[0]), minutes(window[1])

    def margin(zone: str) -> int:
        local = now.astimezone(ZoneInfo(zone))
        at = local.hour * 60 + local.minute
        return min(at - start, end - at)

    if margin(default) >= 15:
        return default
    best = max((f"Etc/GMT{h:+d}" for h in range(-12, 13)), key=margin)
    if margin(best) < 15:
        raise RigError("no time zone puts this minute inside the calling window")
    return best


@dataclass
class Rig:
    """Everything a run opened, so teardown can close it in the right order."""

    http: aiohttp.ClientSession | None = None
    phone: Softphone | None = None
    listener: AriListener | None = None
    app: App | None = None
    apis: list[Api] = field(default_factory=list)
    driver: Driver | None = None
    t: dict[str, float] = field(default_factory=dict)
    report_id: str | None = None
    course_id: str | None = None
    call_id: str | None = None
    keeper: ClipKeeper | None = None
    ast_keeper: ClipKeeper | None = None


def check_speech_setup(plan: Plan) -> None:
    """With the speech model asked for, its folder must be named and complete before anything is started."""
    if not plan.model_dir:
        raise RigError(
            "CT_TEST_SIP_ASR=1 needs CT_ASR_MODEL_DIR set to the IndicConformer folder (there is no default folder)"
        )
    assets = Path(plan.model_dir) / "assets"
    names = ("preprocessor.ts", "encoder.onnx", "ctc_decoder.onnx", "vocab.json", "language_masks.json")
    missing = [n for n in names if not (assets / n).is_file()]
    if missing:
        raise RigError(f"the speech model folder {plan.model_dir} lacks assets/{', assets/'.join(missing)}")


async def scenario(plan: Plan, rep: Report, procs: Procs, rig: Rig) -> None:
    if plan.asr:
        check_speech_setup(plan)
    plan.lan_address = find_lan_address()
    check_ports(plan)
    policy_calls = json.loads((ROOT / "data" / "policy.json").read_text())["calls"]
    plan.local_tz = choose_local_tz(
        datetime.now(UTC), (policy_calls["window_start_local"], policy_calls["window_end_local"])
    )
    if plan.local_tz != "Asia/Kolkata":
        here = datetime.now(UTC).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%H:%M")
        rep.notes.append(
            f"It was {here} in Asia/Kolkata, outside or near the edge of the calling window "
            f"({policy_calls['window_start_local']} to {policy_calls['window_end_local']}), so the app ran with "
            f"CT_LOCAL_TZ={plan.local_tz}, where it was early afternoon. The window and the clock are as shipped."
        )
    rep.step(f"run folder {plan.run_dir}")
    build_asterisk(plan)
    files, took, reused = convert_prompts(plan)
    rep.measure("prompt_files", files)
    rep.measure("prompt_conversion_s", None if reused else round(took, 1))
    rep.measure("prompt_conversion_reused", reused)
    rig.http = aiohttp.ClientSession()
    rep.step("starting Asterisk")
    rep.measure("asterisk_start_s", round(await start_asterisk(plan, procs, rig.http), 1))
    modules = ast_cli(plan, "module show like cdr") + ast_cli(plan, "module show like cel")
    loaded = [m for m in RECORD_MODULES if f"{m}.so" in modules]
    rep.check("no call-record module is loaded", not loaded, ", ".join(loaded))

    rep.step("starting the softphone (the patient's phone)")
    rig.phone = phone = Softphone(plan)
    bs_dir = phone.write_files(Softphone.module_dir(plan.baresip))
    answer = prepare_answer(plan)
    procs.start("baresip", [plan.baresip, "-f", str(bs_dir), "-v", "-T", "-c"], bs_dir / "baresip.log")
    await phone.connect()

    async def registered():
        return "softphone/sip:softphone@" in ast_cli(plan, "pjsip show contacts")

    # the softphone registers while it starts, before the control socket is open, so ask Asterisk, not the phone
    await poll(registered, "the softphone to register to Asterisk", 30, every=0.5)
    rep.check("softphone registered to Asterisk", True)

    rig.driver = driver = Driver(plan, phone, answer)
    rig.listener = listener = AriListener(plan, rig.http, driver.on_event)
    await listener.start()

    rep.step(
        "starting the app (demo mode, CT_TELEPHONY=ari, "
        + ("CT_ASR=indicconformer, the speech model ON)" if plan.asr else "CT_ASR=off)")
    )
    rig.app = app = App(plan, procs)
    logins = app.seed()
    app.start()
    t_app_start = time.monotonic()
    if plan.asr:
        rig.keeper = ClipKeeper(app.dir / "var" / "clips", plan.run_dir / "captured")
        rig.keeper.start()
        # Asterisk's own copy of every recording attempt (the app deletes each after it fetches it)
        rig.ast_keeper = ClipKeeper(
            plan.ast / "spool" / "recording", plan.run_dir / "captured_asterisk", keep_names=True
        )
        rig.ast_keeper.start()
    clinic = Api(plan.app_url)
    reviewer = Api(plan.app_url)
    admin = Api(plan.app_url)
    rig.apis += [clinic, reviewer, admin]
    await app.wait_up(clinic)
    await clinic.sign_in("clinic", logins["clinic"])
    await reviewer.sign_in("reviewer", logins["reviewer"])
    await admin.sign_in("admin", logins["admin"])

    async def telephony_up():
        status = await admin.get("/api/system/status")
        return next((c for c in status["checks"] if c["name"] == "telephony"), {}).get("ok")

    await poll(telephony_up, "the app's telephony link", 30, every=0.5)
    rep.check("the app's phone line is up through ARI", True)

    if plan.asr:

        async def speech_ready():
            status = await admin.get("/api/system/status")
            speech = next((c for c in status["checks"] if c["name"] == "speech"), {})
            if speech.get("code") == "speech.failed":
                raise RigError(f"the speech model did not load: {speech.get('detail')}")
            return speech.get("ok")

        rep.step("waiting for the speech model to load (about 30 s, 2.2 GB)")
        await poll(speech_ready, "the speech model to load", 240, every=0.5)
        rep.measure("app_start_to_speech_ready_s", seconds(t_app_start, time.monotonic()))
        rep.check("the speech model is loaded in the app", True)

    config = await clinic.get("/api/clinic/config")
    if plan.lang not in config["langs"]:
        raise RigError(f"the clinic cannot call in '{plan.lang}': its voice files are incomplete")
    today = (await clinic.get("/api/clinic/today"))["local_date"]
    course = await clinic.post(
        "/api/courses",
        {
            "clinic_id": config["clinic_id"], "register_no": "SIP-E2E-1", "patient_type": "new",
            "regimen_id": config["default_regimen"], "lang": plan.lang, "day0": today, "eligibility": "adult",
        },
    )  # fmt: skip
    rig.course_id = course["id"]

    # ------------------------------------------------------- the missed call
    rep.step("the patient gives a missed call (the softphone dials the missed-call extension)")
    seen_at: dict[str, float] = {}
    # the demo data already holds unlinked missed calls; ours is the one that is new
    before = {u["id"] for u in (await clinic.get("/api/clinic/today"))["unlinked"]}

    async def watch_missed() -> None:
        while "missed_visible" not in seen_at:
            rows = [u for u in (await clinic.get("/api/clinic/today"))["unlinked"] if u["id"] not in before]
            if rows:
                seen_at["missed_visible"] = time.monotonic()
                seen_at["missed_id"] = rows[0]["id"]  # type: ignore[assignment]
                seen_at["missed_last4"] = rows[0]["phone_last4"]  # type: ignore[assignment]
                seen_at["missed_transport"] = rows[0]["transport"]  # type: ignore[assignment]
                return
            await asyncio.sleep(0.1)

    watcher = asyncio.create_task(watch_missed())
    t_dial = rig.t["dial"] = time.monotonic()
    await phone.command("dial", plan.sip_uri)
    t_closed, closed = await phone.wait_for("CALL_CLOSED", 30, after=t_dial)
    rig.t["rejected"] = t_closed
    try:
        await asyncio.wait_for(watcher, 20)
    except TimeoutError as e:
        raise RigError("the missed call did not appear in the clinic's list within 20 s") from e
    rig.t["missed_visible"] = seen_at["missed_visible"]  # type: ignore[assignment]
    rep.check(
        "the missed call was rejected, never answered",
        not phone.seen("CALL_ESTABLISHED", t_dial),
        f"the phone's call closed {seconds(t_dial, t_closed)} s after dialling, answer: {closed.get('param')}",
    )
    incoming = [
        s
        for s in listener.of("ChannelCreated")
        if s.detail.get("context") == "from-softphone" and s.detail.get("exten") == "100"
    ]
    went_up = [
        s
        for s in listener.of("ChannelStateChange")
        if incoming and s.channel == incoming[0].channel and s.detail.get("state") == "Up"
    ]
    kinds = [s.kind for s in listener.seen if incoming and s.channel == incoming[0].channel]
    rep.check("Asterisk never answered it", bool(incoming) and not went_up, "its events: " + ", ".join(kinds))
    rep.check(
        "it appears as an unlinked missed call showing the last four digits only",
        seen_at["missed_last4"] == plan.digits[-4:] and seen_at["missed_transport"] == "ari",
        "shown as ****" + str(seen_at["missed_last4"]),
    )
    rep.measure("missed_call_dial_to_listed_s", seconds(t_dial, rig.t["missed_visible"]))
    rep.measure("missed_call_dial_to_rejected_s", seconds(t_dial, t_closed))

    # ----------------------------------------------------------------- link
    rep.step("the clinic links the missed call to the enrolled course")
    rig.t["link"] = time.monotonic()
    await clinic.post(f"/api/missed-calls/{seen_at['missed_id']}/link", {"course_id": rig.course_id})
    # ------------------------------------------------------------ first call
    rep.step("waiting for the app's first call to ring the softphone")
    t_ring, _ = await phone.wait_for("CALL_INCOMING", 120, after=t_dial + 1)
    rig.t["ring"] = t_ring
    t_up, _ = await phone.wait_for("CALL_ESTABLISHED", 30, after=t_ring)
    rig.t["answered"] = t_up
    rep.step("the softphone answered; the app is playing prompts and the patient is pressing keys")
    t_end, _ = await phone.wait_for("CALL_CLOSED", 300, after=t_up)
    rig.t["ended"] = t_end
    await driver.settle()
    rep.step("the call is over; reading what the app saved")

    def done() -> sqlite3.Row | None:
        with app.db() as conn:
            return conn.execute(
                "SELECT c.* FROM calls c WHERE c.course_id=? AND c.kind='first' AND c.status NOT IN"
                " ('scheduled','dialing','ringing','answered')",
                (rig.course_id,),
            ).fetchone()

    async def finished():
        return done()

    call = await poll(finished, "the first call to be finished in the database", 60, every=0.5)
    rig.call_id = call["id"]
    await poll(_review_settled(app, rig), "the place recording to be handed to review", 30, every=0.5)
    await check_results(plan, rep, rig, app, reviewer, clinic)


def _review_settled(app: App, rig: Rig):
    async def find():
        with app.db() as conn:
            row = conn.execute(
                "SELECT pc.asr_status FROM place_captures pc JOIN bite_reports r ON r.id=pc.report_id"
                " WHERE r.course_id=?",
                (rig.course_id,),
            ).fetchone()
        return row is not None and row["asr_status"] != "pending"

    return find


async def check_results(plan: Plan, rep: Report, rig: Rig, app: App, reviewer: Api, clinic: Api) -> None:
    listener, driver, t = rig.listener, rig.driver, rig.t
    with app.db() as conn:
        call = conn.execute("SELECT * FROM calls WHERE id=?", (rig.call_id,)).fetchone()
        answers = {
            r["question"]: r["value"]
            for r in conn.execute("SELECT * FROM answers WHERE call_id=?", (rig.call_id,))
        }
        report = conn.execute("SELECT * FROM bite_reports WHERE course_id=?", (rig.course_id,)).fetchone()
        capture = (
            conn.execute("SELECT * FROM place_captures WHERE report_id=?", (report["id"],)).fetchone()
            if report
            else None
        )
        course = conn.execute("SELECT * FROM courses WHERE id=?", (rig.course_id,)).fetchone()
        missed = conn.execute("SELECT status, course_id FROM missed_calls").fetchall()
    rig.report_id = report["id"] if report else None

    # ---- the call row
    rep.check(
        "the call row is completed",
        call["status"] == "completed",
        f"status {call['status']}" + (f", error: {call['error']}" if call["error"] else ""),
    )
    rep.check("the call went out over the phone line, not the browser phone", call["transport"] == "ari")
    rep.check(
        "answered_at, ended_at and a hang-up cause are set",
        all(call[k] for k in ("answered_at", "ended_at", "hangup_cause")),
        f"cause {call['hangup_cause']}",
    )
    rep.check(
        "the missed call row is linked to the course",
        any(m["status"] == "linked" and m["course_id"] == rig.course_id for m in missed),
    )
    rep.check("consent to reminders is on the course", bool(course["consent_reminders_at"]))

    # ---- answers
    wrong = {q: (answers.get(q), want) for q, want in EXPECTED_ANSWERS.items() if answers.get(q) != want}
    rep.check(
        "every answer the patient gave was saved",
        not wrong,
        "; ".join(f"{q}: saved {a!r}, pressed for {w!r}" for q, (a, w) in wrong.items())
        or f"{len(EXPECTED_ANSWERS)} answers",
    )
    rep.check(
        "the bite report exists with the same answers",
        bool(report)
        and (report["place_type"], report["animal"], report["signs"]) == ("home_street", "notched", "no"),
        f"{report['place_type']}/{report['animal']}/{report['signs']}" if report else "no report",
    )

    # ---- keys
    sent = driver.presses
    got = [s for s in listener.of("ChannelDtmfReceived") if s.t >= t["answered"] - 1]
    lost = len(sent) - len(got)
    in_order = [p.digit for p in sent] == [g.detail["digit"] for g in got]
    rep.check(
        "every key reached Asterisk in order (RFC 4733), none lost",
        lost == 0 and in_order,
        f"sent {len(sent)}, received {len(got)}",
    )
    rep.measure("dtmf_sent", len(sent))
    rep.measure("dtmf_received_by_asterisk", len(got))
    rep.measure("dtmf_lost", lost)
    if lost == 0:
        delays = [round((g.t - p.t) * 1000) for p, g in zip(sent, got, strict=True)]
        rep.measure(
            "dtmf_press_to_asterisk_ms",
            {"min": min(delays), "max": max(delays), "median": sorted(delays)[len(delays) // 2]},
        )
        rep.measure("dtmf_duration_ms_as_received", sorted({g.detail.get("duration_ms") for g in got}))
    rep.check(
        "the keys were pressed while their questions played",
        not driver.problems and not driver.todo,
        "; ".join(driver.problems) or ("never played: " + ", ".join(driver.todo) if driver.todo else ""),
    )

    # ---- prompts over RTP
    started = [s for s in listener.seen if s.kind == "PlaybackStarted" and s.t >= t["answered"] - 1]
    finished = [s for s in listener.seen if s.kind == "PlaybackFinished" and s.t >= t["answered"] - 1]
    heard = [prompt_id(s.detail.get("media")) for s in started]
    names = sorted({h for h in heard if h})
    rep.check(
        "the prompts were played to the phone", all(q in names for q in QUESTION_PROMPTS), ", ".join(names)
    )
    # one play command is one playback id, however many sound files (segments, slot clips) it chains
    plays = {s.detail["id"] for s in started}
    ended_done = {s.detail["id"] for s in finished if s.detail.get("state") == "done"}
    rep.check(
        "every play ended normally, to its end or cut short by a key",
        bool(plays) and plays == ended_done,
        f"{len(ended_done)} of {len(plays)}",
    )
    rep.measure("plays", len(plays))
    rep.measure("sound_files_played", len(started))
    # ---- recording
    rec_done = [s for s in listener.seen if s.kind == "RecordingFinished"]
    rec = rec_done[0] if rec_done else None
    rep.check(
        "Asterisk recorded the spoken place",
        bool(rec) and (rec.detail.get("duration") or 0) > 0,
        f"duration {rec.detail.get('duration')} s, its talking time {rec.detail.get('talking')} s "
        "(whole seconds, information only: the app decides whether there was speech from the audio)"
        if rec
        else "no recording finished",
    )
    if rec:
        rep.measure("recording_duration_s", rec.detail.get("duration"))
        rep.measure("recording_talking_s", rec.detail.get("talking"))
        rep.measure("recording_silence_s", rec.detail.get("silence"))
        rep.measure("clip_length_s", round(plan.clip_seconds, 2))
    if plan.asr:
        await _check_speech_outcome(plan, rep, rig, app, reviewer, report, capture)
    else:
        rep.check(
            "the recording reached the app and was handed to review",
            bool(capture) and capture["asr_status"] == "off" and report["location_status"] == "needs_review",
            f"asr {capture['asr_status']}, place {report['location_status']}"
            if capture
            else "no capture row",
        )
        if rig.report_id:
            data = await reviewer.get_bytes(f"/api/review/{rig.report_id}/audio")
            length, level = wav_stats(data)
            rep.measure("review_clip_s", round(length, 2))
            rep.measure("review_clip_rms", round(level, 3))
            rep.check(
                "the reviewer can fetch the clip and it holds speech",
                data[:4] == b"RIFF" and length > 0.5 and level > 0.005,
                f"{length:.1f} s, level {level:.3f}",
            )
    stored = await _stored_recordings(plan, rig.http)
    rep.check("Asterisk's own copy of the recording was deleted", stored == [], f"{len(stored)} left")

    # ---- timing
    rep.measure("missed_call_to_ring_s", seconds(t["dial"], t["ring"]))
    rep.measure("link_to_ring_s", seconds(t["link"], t["ring"]))
    rep.measure("ring_to_answer_s", seconds(t["ring"], t["answered"]))
    rep.measure("call_length_s_softphone", seconds(t["answered"], t["ended"]))
    rep.measure("call_seconds_listened_in_app", round(call["seconds_listened"] or 0, 1))
    policy = json.loads((ROOT / "data" / "policy.json").read_text())
    rep.measure("policy_first_call_delay_s", policy["calls"]["first_call_delay_s"])


def spoken_outcome(capture: sqlite3.Row | None, report: sqlite3.Row | None) -> dict:
    """What the app did with the spoken place, in plain fields (no clip, no phone number). `transcript` and
    `candidates` are None when the app placed the report by itself: it deletes both the moment it does."""
    if capture is None or report is None:
        return {"state": "no capture row"}
    cands = json.loads(capture["candidates_json"]) if capture["candidates_json"] else None
    return {
        "state": "placed by itself" if report["location_status"] == "matched" else report["location_status"],
        "ward_no": report["ward_no"],
        "asr_status": capture["asr_status"],
        "asr_model": capture["asr_model"],
        "voiced_s": capture["voiced_s"],
        "matcher_confidence": capture["confidence"],
        "transcript": capture["transcript"],
        "candidates": [
            {
                "ward_no": c["ward_no"],
                "ward_name": c["ward_name"],
                "label": c["label"],
                "evidence": c["evidence"],
            }
            for c in cands
        ]
        if cands is not None
        else None,
        "error": capture["asr_error"],
    }


async def _check_speech_outcome(
    plan: Plan, rep: Report, rig: Rig, app: App, reviewer: Api, report, capture
) -> None:
    """With the speech model on: the recording crossed the exchange, was read and was placed or queued."""
    outcome = spoken_outcome(capture, report)
    rep.measure("place_outcome", outcome)
    floor = json.loads((ROOT / "data" / "policy.json").read_text())["calls"]["speech_min_voiced_s"]
    rep.check(
        "the app measured speech in the recording it kept",
        bool(capture) and (capture["voiced_s"] or 0) >= floor,
        f"voiced {capture['voiced_s'] if capture else None} s, minimum {floor} s",
    )
    rep.check(
        "the speech model read the recording and the place was settled",
        bool(capture)
        and capture["asr_status"] == "done"
        and report["location_status"] in ("matched", "needs_review"),
        f"asr {capture['asr_status']}, place {report['location_status']}" if capture else "no capture row",
    )
    placed = bool(report) and report["location_status"] == "matched"
    if plan.expect_ward is not None:
        rep.check(
            f"the place was set by itself to ward {plan.expect_ward}",
            placed and report["ward_no"] == plan.expect_ward,
            f"{outcome['state']}, ward {report['ward_no'] if report else None}, "
            f"matcher {outcome.get('matcher_confidence')}",
        )
    if placed:
        folder = app.dir / "var" / "clips"
        left = [p.name for p in folder.iterdir()] if folder.is_dir() else []
        rep.check(
            "the clip and the words were deleted when the place was set by itself",
            capture["audio_path"] is None
            and capture["clip_deleted_at"] is not None
            and capture["transcript"] is None
            and capture["candidates_json"] is None
            and not left,
            f"{len(left)} files left in the clips folder",
        )
    elif rig.report_id:
        data = await reviewer.get_bytes(f"/api/review/{rig.report_id}/audio")
        length, level = wav_stats(data)
        rep.check(
            "the reviewer can fetch the clip and it holds speech",
            data[:4] == b"RIFF" and length > 0.5 and level > 0.005,
            f"{length:.1f} s, level {level:.3f}",
        )
    # what the app was given, kept as a file for scripts/sip_e2e_heard.py
    keeper = rig.keeper
    if keeper:
        keeper.sweep()
        rep.check(
            "the file the app received was kept for the listening pass",
            len(keeper.kept) == 1 and keeper.error is None,
            keeper.error or f"{len(keeper.kept)} file(s)",
        )
        if keeper.kept:
            length, level = wav_stats(keeper.kept[0].read_bytes())
            rep.measure("received_clip_s", round(length, 2))
            rep.measure("received_clip_rms", round(level, 3))
    if rig.ast_keeper:
        rig.ast_keeper.sweep()
        attempts = []
        for path in sorted(rig.ast_keeper.kept):
            length, level = wav_stats(path.read_bytes())
            attempts.append({"file": path.name, "seconds": round(length, 2), "level": round(level, 3)})
        rep.measure("asterisk_recording_attempts", attempts)
        if len(attempts) > 1 and attempts[0]["level"] > 0.005:
            rep.notes.append(
                "The app took the first recording as silent, but Asterisk's own file holds speech "
                f"(level {attempts[0]['level']}, {attempts[0]['seconds']} s). The app asked the place question "
                "again and the patient did not repeat it, so the spoken place was lost."
            )
    # recording finished (Asterisk) to place settled (the app's own audit row)
    done = [s for s in rig.listener.seen if s.kind == "RecordingFinished"] if rig.listener else []
    with app.db() as conn:
        row = conn.execute(
            "SELECT at FROM audit WHERE object_id=? AND action IN ('review.matched','review.queued')"
            " ORDER BY seq DESC LIMIT 1",
            (rig.report_id,),
        ).fetchone()
    if done and row:
        settled = datetime.fromisoformat(row["at"].replace("Z", "+00:00")).timestamp()
        finished_wall = done[-1].t + (
            time.time() - time.monotonic()
        )  # the last recording is the one the app kept
        rep.measure("recording_finished_to_place_settled_s", round(settled - finished_wall, 2))


async def _stored_recordings(plan: Plan, http: aiohttp.ClientSession) -> list:
    async with http.get(f"{plan.ari_url}/ari/recordings/stored", headers=ari_header(plan)) as r:
        return json.loads(await r.text() or "[]") if r.status == 200 else [f"status {r.status}"]


async def teardown(plan: Plan, procs: Procs, rig: Rig) -> None:
    for api in rig.apis:
        with contextlib.suppress(Exception):
            await api.close()
    if rig.driver:
        with contextlib.suppress(Exception):
            await rig.driver.settle()
    procs.stop("app", grace_s=15)
    for keeper in (rig.keeper, rig.ast_keeper):
        if keeper:
            with contextlib.suppress(Exception):
                keeper.stop()
    if rig.listener:
        with contextlib.suppress(Exception):
            await rig.listener.stop()
    if rig.phone:
        with contextlib.suppress(Exception):
            await rig.phone.close()
    procs.stop("baresip")
    if rig.http:
        await rig.http.close()
    if any(n == "asterisk" for n, _ in procs.items):
        stop_asterisk(plan, procs)


def write_timeline(plan: Plan, rig: Rig) -> None:
    """One file with what Asterisk reported, what the softphone saw and what the driver pressed, in time order.
    Caller numbers are never in it: the listener drops them and the softphone's events are cut to their type."""
    rows: list[tuple[float, str]] = []
    if rig.listener:
        for s in rig.listener.seen:
            rows.append((s.t, f"asterisk   {s.kind} {s.channel or ''} {json.dumps(s.detail, default=str)}"))
    if rig.phone:
        for at, ev in rig.phone.events:
            rows.append((at, f"softphone  {ev.get('type')}"))
    if rig.driver:
        for p in rig.driver.presses:
            rows.append((p.t, f"driver     pressed {p.digit} during {p.prompt}"))
        if rig.driver.spoken_at:
            rows.append((rig.driver.spoken_at, "driver     spoke the clip"))
    for name, at in rig.t.items():
        rows.append((at, f"script     {name}"))
    rows.sort()
    if rows:
        zero = rows[0][0]
        (plan.run_dir / "timeline.txt").write_text(
            "\n".join(f"{at - zero:8.3f}  {text}" for at, text in rows) + "\n"
        )


def after_stop(plan: Plan, rep: Report, rig: Rig, procs: Procs) -> None:
    """Checks that need the processes gone and their files closed."""
    write_timeline(plan, rig)
    alive = [n for n, p in procs.items if p.poll() is None]
    rep.check("Asterisk, the softphone and the app are all stopped", not alive, ", ".join(alive))
    logs = [plan.ast / "log" / n for n in ("console.log", "messages.log", "queue_log")]
    logs += [plan.run_dir / "app" / "app.log"]
    leaks = find_number_leaks(logs, plan.patient_number)
    present = [p for p in logs if p.is_file()]
    missing = [p.name for p in logs if p.name != "queue_log" and not p.is_file()]  # queue_log may not exist
    rep.check(
        "the patient's number is in no Asterisk or app log",
        not leaks and not missing,
        ", ".join(leaks)
        or (f"not found: {', '.join(missing)}" if missing else f"read: {', '.join(p.name for p in present)}"),
    )
    spill = [
        p.name for p in (plan.ast / "log").iterdir() if re.search(r"cdr|cel|master", p.name, re.IGNORECASE)
    ] + [p.name for p in (plan.ast / "spool").rglob("*") if p.is_file()]
    rep.check("Asterisk wrote no call records and left no recording files", not spill, ", ".join(spill))
    heard = plan.run_dir / "baresip" / "heard.wav"
    if heard.is_file():
        raw = heard.read_bytes()
        try:
            length, level = wav_stats(raw)
        except Exception:  # the writer was cut off before it finished the header: count the samples
            length = max(len(raw) - 44, 0) / 2 / 8000
        rep.measure("audio_heard_by_phone_s", round(length, 1))
        rep.check("the phone received audio over RTP", length > 5, f"{length:.1f} s heard")


async def run(plan: Plan) -> Report:
    plan.run_dir.mkdir(parents=True, exist_ok=True)
    rep, rig, procs = Report(), Rig(), Procs(plan.run_dir)
    try:
        await scenario(plan, rep, procs, rig)
    except RigError as e:
        rep.check("the rig ran to the end", False, str(e))
    except Exception as e:
        rep.check("the rig ran to the end", False, f"{type(e).__name__}: {e}")
    finally:
        await teardown(plan, procs, rig)
        procs.stop_all()  # whatever teardown could not reach; each by its own PID
    after_stop(plan, rep, rig, procs)
    (plan.run_dir / "report.json").write_text(json.dumps(rep.as_json(), indent=2, default=str))
    if rep.ok and not plan.keep:
        tidy(plan)
    return rep


def tidy(plan: Plan) -> None:
    """A passing run keeps its report and timeline only: the rest holds a database, voice files and the
    softphone's own log (its view of the call shows the caller ID). CT_SIP_E2E_KEEP=1 keeps everything, and a
    failing run always does."""
    for name in ("ast", "app", "baresip", "prompts_in"):
        shutil.rmtree(plan.run_dir / name, ignore_errors=True)


def print_summary(rep: Report, plan: Plan) -> None:
    print("\nMeasured (this machine, one run, SIP and RTP on its own address, no cellular network):")
    for k, v in rep.measures.items():
        print(f"  {k}: {v}")
    for note in rep.notes:
        print(f"  note: {note}")
    failed = [n for n, ok, _ in rep.checks if not ok]
    print(
        f"\n{len(rep.checks) - len(failed)} of {len(rep.checks)} checks passed"
        + (f"; FAILED: {', '.join(failed)}" if failed else "")
    )
    print(
        f"report: {plan.run_dir / 'report.json'}"
        + ("" if rep.ok else f" (everything kept in {plan.run_dir})")
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SIP end-to-end run (see scripts/sip_e2e.sh)")
    ap.add_argument("--lang", help="the patient's language (default hi)")
    ap.add_argument("--clip", type=Path, help="the WAV the patient speaks for the place question")
    ap.add_argument(
        "--asr",
        action="store_true",
        help="run the app with the speech model (needs CT_ASR_MODEL_DIR; same as CT_TEST_SIP_ASR=1)",
    )
    ap.add_argument(
        "--expect-ward",
        type=int,
        help="with --asr: the ward the clip names; the run fails unless the app sets it by itself",
    )
    args = ap.parse_args(argv)
    plan = plan_from_env()
    if args.asr:
        plan.asr = True
    if args.expect_ward is not None:
        plan.expect_ward = args.expect_ward
    if plan.expect_ward is not None and not plan.asr:
        ap.error(
            "--expect-ward needs --asr (the speech model is off otherwise, so no place is set by itself)"
        )
    if args.lang:
        plan.lang = args.lang
    if args.clip:
        plan.clip = args.clip.resolve()
    rep = asyncio.run(run(plan))
    print_summary(rep, plan)
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
