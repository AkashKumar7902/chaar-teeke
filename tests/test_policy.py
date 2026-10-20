"""data/policy.json is pinned by version: the same version must always hash to the same bytes.

The rule (CLAUDE.md, rule 7): a pin is never edited and never removed. A change to any threshold needs a
new "version" in data/policy.json and a new line in PINS below, and the old lines stay. The pins in git's
HEAD must all still be here, so removing or rewriting one fails even when the file still matches itself.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from chaarteeke.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
POLICY_FILE = ROOT / "data" / "policy.json"

# version -> sha256 of the bytes of data/policy.json at that version. Add lines; never change or delete one.
PINS: dict[str, str] = {
    "2026.10.2": "6eef3a2512403c9a1fdb71befdb695ee1f188b170e137c59c69f3ec7ec9fdebc",
    "2026.10.3": "96b327b9a98d3ff78cf2f8061be1d4ebfd82a4c7ae84134bc7966c27e6514fd3",
    "2026.10.4": "df0400f4692419e5b91dbcbf36aabae3063b42cec03d8ff7b689ae6e9c2caba0",
    "2026.10.5": "d49e0aeb7bffdc84f1b7d186e79e891b02a8ff9e988ce6f761bd5284efd571bc",
    "2026.10.6": "1c9950bc8f871cef6d9c4b911166881e65ac48bc5175d48cf81049d4c9a8193b",
    "2026.10.7": "b33b9595374ac37eb997d4940c008eec7a1391c5189254fdbc594629fe118702",
    "2026.10.8": "d2dedfe9b79251b2629378027058901b1f5f78dfaed85ba7bbb7310d62890b45",
    "2026.10.9": "51220a993af14218a9a1dc2fabb2b1746297cf68b06e15c8fb2a52c9e14711ef",
}

PIN_LINE = re.compile(r'^\s*"(\d{4}\.\d+\.\d+)":\s*"([0-9a-f]{64})",?\s*$', re.MULTILINE)


def test_the_pins_look_like_pins():
    for version, digest in PINS.items():
        assert re.fullmatch(r"\d{4}\.\d+\.\d+", version), version
        assert re.fullmatch(r"[0-9a-f]{64}", digest), f"{version}: not a sha256"


def test_the_current_policy_version_is_pinned():
    version = load_policy().version
    assert version in PINS, (
        f"data/policy.json is version {version} and has no pin. Add "
        f'"{version}": "{hashlib.sha256(POLICY_FILE.read_bytes()).hexdigest()}" to PINS in tests/test_policy.py.'
    )


def test_the_current_policy_matches_its_pin():
    policy = load_policy()
    pinned = PINS.get(policy.version)
    if pinned is None:
        pytest.fail(f"policy version {policy.version} has no pin (see test above)")
    assert policy.digest == pinned, (
        f"data/policy.json changed but its version is still {policy.version}. A threshold change needs a new "
        "version string in the file and a new pin here; never edit an existing pin."
    )


def _pins_in(source: str) -> dict[str, str]:
    return dict(PIN_LINE.findall(source.split("PINS: dict[str, str] = {", 1)[-1].split("}", 1)[0]))


def test_a_pin_is_never_edited_or_removed():
    """Every pin committed in any version of this file is still here with the digest it first had.

    HEAD alone is not enough: in CI the checked-out commit is HEAD, so the file always equals it. Reading the
    whole history of the file means an edit or removal is caught there too.
    """
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("not a git checkout")

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)

    log = git("log", "--format=%H", "--reverse", "--", "tests/test_policy.py")
    if log.returncode != 0 or not log.stdout.split():
        pytest.skip("tests/test_policy.py is not committed yet")
    first: dict[str, str] = {}
    for sha in log.stdout.split():
        shown = git("show", f"{sha}:tests/test_policy.py")
        if shown.returncode == 0:  # a commit that deleted the file has nothing to read
            for version, digest in _pins_in(shown.stdout).items():
                first.setdefault(version, digest)
    assert first, "found no pins in the committed tests/test_policy.py; has the file layout changed?"
    for version, digest in first.items():
        assert PINS.get(version) == digest, f"pin {version} was committed and may not be edited or removed"


def test_the_pin_reader_reads_this_files_own_layout():
    assert _pins_in(Path(__file__).read_text(encoding="utf-8")) == PINS
