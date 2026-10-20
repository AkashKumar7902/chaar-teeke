#!/usr/bin/env python3
"""The two checks scripts/smoke.sh runs around `chaar-teeke preflight`.

  smoke_checks.py env          which database is checked, and whether its folder and var/ can be written
  smoke_checks.py url [URL]    talk to a running server over HTTP the way a visitor's browser does

`env` prints the database path first, so a check never reads as if it covered another file. It creates and
writes nothing: a folder that does not exist yet is checked against the nearest one that does.

`url` is the stage the offline preflight cannot be: it needs the server running. It checks the landing page,
a visitor sign-in, the demo label, the coverage with its first share, the ward map data, the vendored map
library (against web/vendor/*/SHA256SUMS), one stream frame from /api/events, and that the demo clock is on
real time. URL defaults to http://127.0.0.1:$CT_PORT (CT_PORT from the environment or .env, else 8080).
Each check prints one line; the exit code is 1 if any fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The folder that holds the database and the call clips needs room for both; the speech model is elsewhere.
MIN_FREE_MB = 500
STREAM_WAIT_S = 5
HTTP_WAIT_S = 10
MAP_LIBRARY = "/vendor/maplibre-gl-4.7.1/maplibre-gl.js"


class Failed(Exception):
    """A check that did not pass; the message is the plain-words reason."""


def line(ok: bool, name: str, detail: str) -> bool:
    print(f"{'ok  ' if ok else 'FAIL'}  {name:<11} {detail}")
    sys.stdout.flush()
    return ok


# ------------------------------------------------------------------------------------------------ env
def nearest_existing(path: Path) -> Path:
    for p in (path, *path.parents):
        if p.exists():
            return p
    return Path(path.anchor or ".")


def folder_check(name: str, folder: Path) -> bool:
    base = nearest_existing(folder)
    where = str(folder) if base == folder else f"{folder} (not there yet; will be made under {base})"
    if not os.access(base, os.W_OK):
        return line(False, name, f"{where} cannot be written to")
    free_mb = shutil.disk_usage(base).free // (1024 * 1024)
    if free_mb < MIN_FREE_MB:
        return line(False, name, f"{where}: only {free_mb} MB free, {MIN_FREE_MB} MB needed")
    return line(True, name, f"{where} is writable, {free_mb / 1024:.1f} GB free")


def env_stage() -> bool:
    from chaarteeke.config import Settings

    s = Settings()
    print(f"database: {s.db}")
    sys.stdout.flush()
    results = [folder_check("var dir", s.var_dir)]
    if s.db.parent != s.var_dir:
        results.append(folder_check("db dir", s.db.parent))
    return all(results)


# ------------------------------------------------------------------------------------------------ url
class Client:
    """One visitor's browser: it keeps the session cookie the server sets."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def open(self, path: str, body: dict | None = None, wait: float = HTTP_WAIT_S):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data, headers={"Content-Type": "application/json"} if data else {}
        )
        try:
            return self.opener.open(req, timeout=wait)
        except urllib.error.HTTPError as e:
            raise Failed(f"{path} answered HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise ConnectionError(f"nothing answers at {self.base} ({e.reason})") from None
        except TimeoutError:
            raise Failed(f"{path} did not answer in {wait:.0f} s") from None

    def read(self, path: str, body: dict | None = None) -> tuple[bytes, str]:
        with self.open(path, body) as r:
            return r.read(), r.headers.get("Content-Type", "")

    def json(self, path: str, body: dict | None = None):
        raw, kind = self.read(path, body)
        if "json" not in kind:
            raise Failed(f"{path} answered {kind or 'no content type'}, not JSON")
        return json.loads(raw)


def check_landing(c: Client) -> str:
    raw, kind = c.read("/")
    if "text/html" not in kind or b"<html" not in raw.lower():
        raise Failed(f"/ answered {kind or 'no content type'}, not a page")
    return f"/ is a page ({len(raw)} bytes)"


def check_visitor(c: Client) -> str:
    me = c.json("/api/auth/visitor", {"as": "abc"})
    if me.get("role") != "visitor":
        raise Failed(f"signed in as {me.get('role')!r}, expected a visitor")
    return "a visitor can sign in without a login"


def check_demo_label(c: Client) -> str:
    mode = c.json("/api/mode").get("mode")
    if mode != "demo":
        raise Failed(f"/api/mode says {mode!r}: the demo needs CT_MODE=demo")
    frame = c.json("/i18n/common.en.json").get("frame", {}).get("demo", {})
    if "DEMO DATA" not in frame.get("label", ""):
        raise Failed("the frame bundle has no DEMO DATA label")
    return f"mode demo, frame label {frame['label']!r}"


def check_coverage(c: Client) -> str:
    city = c.json("/api/coverage").get("city", {})
    if city.get("status") != "ok":
        raise Failed(f"the city share is {city.get('status')!r}, not ok (is the demo data seeded?)")
    if not city.get("n", 0) > 0:
        raise Failed("the city share rests on no answers (n = 0)")
    return f"city share ok on n = {city['n']}"


def check_wards(c: Client) -> str:
    wards = c.json("/data/wards.geojson")
    n = len(wards.get("features", []))
    if wards.get("type") != "FeatureCollection" or n == 0:
        raise Failed("/data/wards.geojson has no ward polygons")
    return f"{n} ward polygons"


def check_map_library(c: Client) -> str:
    raw, _ = c.read(MAP_LIBRARY)
    sums = ROOT / "web" / "vendor" / Path(MAP_LIBRARY).parent.name / "SHA256SUMS"
    want = {
        name: digest for digest, name in (ln.split() for ln in sums.read_text().splitlines() if ln.strip())
    }
    got = hashlib.sha256(raw).hexdigest()
    if got != want[Path(MAP_LIBRARY).name]:
        raise Failed(f"{MAP_LIBRARY} does not match web/vendor SHA256SUMS")
    return f"{Path(MAP_LIBRARY).name} served, checksum matches"


def check_stream(c: Client) -> str:
    with c.open("/api/events", wait=STREAM_WAIT_S) as r:
        kind = r.headers.get("Content-Type", "")
        if "text/event-stream" not in kind:
            raise Failed(f"/api/events answered {kind or 'no content type'}, not an event stream")
        try:
            first = r.readline().decode().strip()
        except TimeoutError:
            raise Failed(f"/api/events sent nothing in {STREAM_WAIT_S} s") from None
    if not first:
        raise Failed("/api/events closed without a frame")
    return f"stream open, first frame {first!r}"


def check_clock(c: Client) -> str:
    offset = c.json("/api/demo/state").get("offset_seconds")
    if offset != 0:
        raise Failed(
            f"the demo clock is {offset} s off real time: press Reset demo data (uv run chaar-teeke reset-demo)"
        )
    return "demo clock on real time (offset 0 s)"


URL_CHECKS: list[tuple[str, Callable[[Client], str]]] = [
    ("page", check_landing),
    ("visitor", check_visitor),
    ("demo label", check_demo_label),
    ("coverage", check_coverage),
    ("wards", check_wards),
    ("map js", check_map_library),
    ("stream", check_stream),
    ("clock", check_clock),
]


def default_url() -> str:
    port = os.environ.get("CT_PORT")
    if not port:
        from chaarteeke.config import Settings

        port = str(Settings().port)
    return f"http://127.0.0.1:{port}"


def url_stage(base: str | None) -> bool:
    base = base or default_url()
    client = Client(base)
    print(f"server: {base}")
    results = []
    for name, check in URL_CHECKS:
        try:
            results.append(line(True, name, check(client)))
        except ConnectionError as e:
            line(False, name, f"{e}; start the server first")
            return False
        except (Failed, json.JSONDecodeError, KeyError) as e:
            results.append(line(False, name, str(e) or type(e).__name__))
    print(f"{sum(results)} of {len(results)} server checks passed")
    return all(results)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in ("env", "url") or len(argv) > 2:
        print(
            (__doc__ or "").strip().split("\n\n")[0] + "\n\nusage: smoke_checks.py env | url [URL]",
            file=sys.stderr,
        )
        return 2
    ok = env_stage() if argv[0] == "env" else url_stage(argv[1] if len(argv) > 1 else None)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
