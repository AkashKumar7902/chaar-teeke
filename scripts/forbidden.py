#!/usr/bin/env python3
"""Personal contact details that must not be in this repository, now or anywhere in its history.

  forbidden.py history     scan every commit reachable from a branch, tag or remote ref

The patterns and the allowances live here once: tests/test_repo_rules.py scans the files as they are with the
same code, and scripts/gate.sh runs `history` so a number or mailbox that was committed and later deleted
still fails. A shallow clone cannot be scanned: it prints a note saying so and passes, so CI fetches the
whole history (fetch-depth: 0).

What counts: a personal mailbox (gmail, yahoo, rediffmail ...), and an Indian mobile number. What is allowed:
the reserved demo pattern +91 99999 0xxxx, a few patterned numbers that are nobody's (in tests and code only),
and the two clinic contacts the National Rabies Control Programme publishes (in data/clinics.json only).
A match is printed with most of it hidden: this output goes to logs.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 10 digits starting 6-9, with or without +91 / 91 / 0091 and a space or hyphen in the middle
PHONE_RE = re.compile(r"(?<![\w./])(?:(?:\+|00)?91[\s-]?)?([6-9]\d{4})[\s-]?(\d{5})(?!\w|\.\d)")
RESERVED_DEMO = re.compile(r"^999990\d{4}$")  # +91 99999 0xxxx: never dialled
# Made-up numbers that the tests and a docstring or two use because they look like a number (the textbook
# example, runs of one digit). Nobody's, and only where code and tests may hold them.
NOBODYS_NUMBERS = {
    "9876543210", "9876543211", "9876543212", "9123456780", "9000000001", "9000000002", "9888888888",
    "9999999999", "9199999099",
}  # fmt: skip
NOBODYS_NUMBERS_IN = {"tests", "chaarteeke", "scripts"}
# No real number may stay anywhere: only the reserved demo pattern and the listed made-up numbers pass.
PUBLISHED_CLINIC_CONTACTS: dict[str, set[str]] = {}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
PERSONAL_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in", "ymail.com", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "rediffmail.com", "rediff.com", "icloud.com", "me.com",
    "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com", "zoho.com",
}  # fmt: skip

# folders and files the history scan leaves out: vendored, generated or binary
SKIP_DIRS = {"audio", "clips", "vendor", "fonts", "node_modules", "__pycache__", ".venv", ".git", "var"}
SKIP_FILES = {"uv.lock"}


@dataclass(frozen=True)
class Hit:
    where: str
    kind: str  # "number" or "mailbox"
    shown: str  # the match with most of it hidden

    def __str__(self) -> str:
        return f"{self.where}: {self.kind} {self.shown}"


def hide(text: str) -> str:
    keep = 2 if len(text) < 12 else 3
    return f"{text[:keep]}...{text[-keep:]}"


def number_allowed(digits: str, path: str) -> bool:
    if RESERVED_DEMO.match(digits):
        return True
    top = path.split("/", 1)[0]
    if digits in NOBODYS_NUMBERS and top in NOBODYS_NUMBERS_IN:
        return True
    return digits in PUBLISHED_CLINIC_CONTACTS.get(path, ())


def hits_in_line(line: str, path: str) -> list[tuple[str, str]]:
    """(kind, matched text) for each personal mailbox or real-looking number in the line."""
    found = []
    for m in PHONE_RE.finditer(line):
        if not number_allowed(m.group(1) + m.group(2), path):
            found.append(("number", m.group(0)))
    for m in EMAIL_RE.finditer(line):
        if m.group(1).lower() in PERSONAL_MAIL:
            found.append(("mailbox", m.group(0)))
    return found


# ------------------------------------------------------------------------------------------------ history
def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")


def skipped_path(path: str) -> bool:
    parts = path.split("/")
    return parts[-1] in SKIP_FILES or bool(set(parts) & SKIP_DIRS)


def scan_history(root: Path = ROOT) -> tuple[str, list[Hit]]:
    """('', hits) after scanning every commit reachable from a branch, tag or remote ref; (note, []) when
    there is nothing to scan (not a git checkout, or a shallow clone that has only part of the history)."""
    if git(root, "rev-parse", "--git-dir").returncode != 0:
        return "not a git checkout: history not scanned", []
    if git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        return "shallow clone: history not scanned (fetch the whole history to scan it)", []
    refs = ("--branches", "--tags", "--remotes")
    hits: list[Hit] = []

    # who made each commit, and what they wrote about it
    meta = git(root, "log", *refs, "--format=%h%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1e")
    if meta.returncode != 0:
        raise RuntimeError(f"git log failed: {meta.stderr.strip()}")
    for record in meta.stdout.split("\x1e"):
        if not record.strip():
            continue
        sha, author, committer, message = record.strip().split("\x1f", 3)
        for field, text in (("author", author), ("committer", committer), ("message", message)):
            for line in text.splitlines():
                hits += [Hit(f"{sha} {field}", kind, hide(s)) for kind, s in hits_in_line(line, "")]

    # every line any commit added
    patch = git(root, "log", *refs, "-p", "-m", "--no-color", "--format=@@commit %h")
    if patch.returncode != 0:
        raise RuntimeError(f"git log -p failed: {patch.stderr.strip()}")
    sha = path = ""
    for line in patch.stdout.splitlines():
        if line.startswith("@@commit "):
            sha, path = line.split(" ", 1)[1], ""
        elif line.startswith("+++ "):
            path = line[4:].removeprefix("b/") if line != "+++ /dev/null" else ""
        elif line.startswith("+") and path and not skipped_path(path):
            hits += [Hit(f"{sha} {path}", kind, hide(s)) for kind, s in hits_in_line(line[1:], path)]
    return "", list(dict.fromkeys(hits))


def history_stage() -> int:
    note, hits = scan_history()
    if note:
        print(f"history: {note}")
        return 0
    if hits:
        print(f"history: {len(hits)} personal contact detail(s) in the commits (shown hidden):")
        for h in hits:
            print(f"  {h}")
        print("history: rewrite the branch into clean history before the first push (owner's decision)")
        return 1
    print("history: no personal mailbox or real-looking number in any commit")
    return 0


def main(argv: list[str]) -> int:
    if argv != ["history"]:
        print("usage: forbidden.py history", file=sys.stderr)
        return 2
    return history_stage()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
