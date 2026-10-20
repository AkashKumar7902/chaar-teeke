"""Rules about the repository itself, checked on the files as they are.

These are the rules that are cheap to break by accident and expensive to find later: one clock, a strict
CSP, the words the screens never say, the honesty labels, no real phone numbers or personal emails, what the
phone exchange's configuration may log and keep, the licence files, and clean commit messages.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from chaarteeke.policy import load_policy
from chaarteeke.stats import coverage

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str):
    """A module from scripts/, loaded by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


# the patterns and allowances for numbers and mailboxes live in one place, shared with the gate's history scan
forbidden = _script("forbidden")
WEB = ROOT / "web"
I18N = WEB / "i18n"
PAGES = sorted(WEB.glob("*.html"))
BUNDLES = sorted(I18N.glob("*.json"))

# the words no screen says (docs/DESIGN.md); "sterilized" is the same word spelled the other way
NEVER_WORDS = [
    "safe",
    "clear",
    "all fine",
    "rabies detected",
    "diagnosis",
    "fully sterilised",
    "fully sterilized",
]

SKIP_DIRS = {"audio", "clips", "vendor", "fonts", "node_modules", "__pycache__", ".venv", ".git", "var"}
TEXT_SUFFIXES = {
    ".html", ".css", ".js", ".mjs", ".json", ".geojson", ".md", ".py", ".sql", ".txt", ".svg", ".yml",
    ".yaml", ".toml", ".sh", ".example", ".conf", "",
}  # fmt: skip


def _ids(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


def _text_files(*roots: str) -> list[Path]:
    """Text files under the given repo folders (or single files), without binary, vendored or generated ones."""
    out: list[Path] = []
    for name in roots:
        base = ROOT / name
        if base.is_file():
            out.append(base)
            continue
        for p in sorted(base.rglob("*")):
            if (
                p.is_file()
                and p.suffix.lower() in TEXT_SUFFIXES
                and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS)
            ):
                out.append(p)
    return out


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strings(node, path: str = ""):
    """(key path, string) for every string in a parsed i18n bundle, `_meta` excluded."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k != "_meta" or path:
                yield from _strings(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def _bundle(name: str) -> dict[str, str]:
    return dict(_strings(json.loads(_read(I18N / name))))


def _never_pattern(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(word).replace(r'\ ', r'\s+')}(?!\w)", re.IGNORECASE)


class _Html(HTMLParser):
    """Collects what the CSP and the screens care about from one page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline_scripts: list[str] = []
        self.script_srcs: list[str] = []
        self.style_tags = 0
        self.style_attrs: list[str] = []
        self.handlers: list[str] = []
        self.text: list[str] = []
        self._in: str | None = None
        self._buf: list[str] = []
        self._script_has_src = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for name in a:
            if name == "style":
                self.style_attrs.append(f"<{tag} style=...>")
            if name.startswith("on"):
                self.handlers.append(f"<{tag} {name}=...>")
        if tag == "style":
            self.style_tags += 1
        if tag == "script":
            self._script_has_src = "src" in a
            if "src" in a:
                self.script_srcs.append(a["src"] or "")
        if tag in ("script", "style"):
            self._in, self._buf = tag, []

    def handle_endtag(self, tag):
        if tag == self._in:
            body = "".join(self._buf).strip()
            if tag == "script" and body:
                self.inline_scripts.append(body[:60])
            self._in = None

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)
        elif data.strip():
            self.text.append(data.strip())


def _parse(page: Path) -> _Html:
    parser = _Html()
    parser.feed(_read(page))
    parser.close()
    return parser


# --------------------------------------------------------------------------------------------- the clock
CLOCK_FILES = {"clock.py", "fake_ari.py"}  # the clock itself, and the fake PBX that imitates its own clock
NOW_CALL = re.compile(r"\b(?:datetime|date)\.(?:now|today|utcnow)\(")


def test_nothing_but_the_clock_reads_the_system_time():
    offenders = []
    for p in sorted((ROOT / "chaarteeke").rglob("*.py")):
        if p.name in CLOCK_FILES:
            continue
        for n, line in enumerate(_read(p).splitlines(), 1):
            if NOW_CALL.search(line):
                offenders.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, "use chaarteeke.clock.now():\n" + "\n".join(offenders)


def test_the_clock_exception_list_is_real():
    for name in CLOCK_FILES:
        assert list((ROOT / "chaarteeke").rglob(name)), f"{name} is exempt but does not exist"


# --------------------------------------------------------------------------------------------- the pages
def test_there_are_pages_to_check():
    assert PAGES, "no web/*.html found"


@pytest.mark.parametrize("page", PAGES, ids=_ids(PAGES))
def test_page_has_no_inline_script_or_style(page):
    html = _parse(page)
    assert not html.inline_scripts, f"{page.name}: inline script body {html.inline_scripts}"
    assert not html.style_tags, f"{page.name}: <style> element"
    assert not html.style_attrs, f"{page.name}: style attribute {html.style_attrs}"
    assert not html.handlers, f"{page.name}: inline event handler {html.handlers}"


@pytest.mark.parametrize("page", PAGES, ids=_ids(PAGES))
def test_page_loads_only_same_origin_scripts(page):
    for src in _parse(page).script_srcs:
        assert src and not re.match(
            r"^(?:[a-z][a-z0-9+.-]*:|//)", src, re.IGNORECASE
        ), f"{page.name}: script from another origin: {src}"


# --------------------------------------------------------------------------------------------- words
@pytest.mark.parametrize("bundle", BUNDLES, ids=_ids(BUNDLES))
def test_no_never_on_screen_word_in_strings(bundle):
    hits = []
    for key, value in _strings(json.loads(_read(bundle))):
        for word in NEVER_WORDS:
            if _never_pattern(word).search(value):
                hits.append(f'{key}: "{word}" in "{value[:80]}"')
    assert not hits, f"{bundle.name}:\n" + "\n".join(hits)


@pytest.mark.parametrize("page", PAGES, ids=_ids(PAGES))
def test_no_never_on_screen_word_in_page_text(page):
    text = " ".join(_parse(page).text)
    hits = [w for w in NEVER_WORDS if _never_pattern(w).search(text)]
    assert not hits, f"{page.name}: {hits}"


# --------------------------------------------------------------------------------------------- honesty pins
COMMON = sorted(I18N.glob("common.*.json"))
PHONE = sorted(I18N.glob("phone.*.json"))
PRIVACY = sorted(I18N.glob("privacy.*.json"))
PROMISE = "phone numbers leave the reminder system 7 days after the course"


@pytest.mark.parametrize("bundle", COMMON, ids=_ids(COMMON))
def test_demo_data_label_is_in_every_common_bundle(bundle):
    assert "DEMO DATA" in _bundle(bundle.name).get("frame.demo.label", "")


@pytest.mark.parametrize("bundle", PHONE, ids=_ids(PHONE))
def test_simulator_label_is_in_every_phone_bundle(bundle):
    assert "SIMULATOR" in _bundle(bundle.name).get("phone.simulatorLabel", "")


def test_ward_matches_are_called_an_estimate():
    assert "estimate" in _bundle("common.en.json").get("status.location.matched", "").lower()


def test_the_claimed_band_is_labelled_78_to_80():
    policy = load_policy()
    _, label = coverage._claim(SimpleNamespace(policy=policy))
    assert label.startswith("78-80%"), label
    assert "expected only if sterilised and unsterilised dogs bite equally" in label


def test_machine_voice_draft_is_said_somewhere_people_read():
    homes = [ROOT / "chaarteeke" / "api" / "routes_admin.py", *sorted(I18N.glob("admin.*.json"))]
    homes += sorted((ROOT / "data" / "prompts").glob("README*"))
    said = [p.relative_to(ROOT) for p in homes if p.is_file() and "machine voice draft" in _read(p).lower()]
    assert (
        said
    ), f"'machine voice draft' is in none of: {[str(p.relative_to(ROOT)) for p in homes if p.is_file()]}"


def _promise_keys(bundles: list[Path]) -> set[str]:
    en = [b for b in bundles if b.name.endswith(".en.json")]
    return {k for b in en for k, v in _bundle(b.name).items() if PROMISE in v.lower()}


@pytest.mark.parametrize("group", [PHONE, PRIVACY], ids=["phone", "privacy"])
def test_the_deletion_promise_is_on_the_page_in_every_language(group):
    keys = _promise_keys(group)
    assert keys, f"{PROMISE!r} is not in the English {group[0].name.split('.')[0]} bundle"
    for bundle in group:
        strings = _bundle(bundle.name)
        for key in keys:
            assert "7" in strings.get(key, ""), f"{bundle.name}: {key} has no 7-day promise"


# --------------------------------------------------------------------------------------------- numbers and mail
# The patterns, the reserved +91 99999 0xxxx demo pattern, the few made-up numbers code and tests may use and the
# two published clinic contacts are in scripts/forbidden.py, so the gate's history scan applies the same rules.
PHONE_RE = forbidden.PHONE_RE
RESERVED_DEMO = forbidden.RESERVED_DEMO
EMAIL_RE = forbidden.EMAIL_RE
PERSONAL_MAIL = forbidden.PERSONAL_MAIL


def _top_level_files() -> list[Path]:
    return [
        p for p in ROOT.glob("*") if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES and p.name != "uv.lock"
    ]


def test_no_real_looking_indian_mobile_numbers():
    """Everywhere a number could be served, dialled or copied: screens, docs, data, code, tests and the phone
    exchange's configuration. Only +91 99999 0xxxx, a short list of made-up numbers (code and tests only) and
    the published clinic contacts (data/clinics.json) may appear."""
    offenders = []
    files = _text_files("web", "docs", "data", "chaarteeke", "scripts", "tests", "telephony", ".github")
    files += _top_level_files()
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        for n, line in enumerate(_read(p).splitlines(), 1):
            for m in PHONE_RE.finditer(line):
                if not forbidden.number_allowed(m.group(1) + m.group(2), rel):
                    offenders.append(f"{rel}:{n}: {m.group(0)}")
    assert not offenders, "only the reserved +91 99999 0xxxx pattern may appear:\n" + "\n".join(offenders)


def test_the_number_pattern_catches_what_it_should():
    assert PHONE_RE.search("call +91 98765 43210")
    assert PHONE_RE.search("call 9876543210")
    assert PHONE_RE.search("call 91-98765-43210")
    assert not PHONE_RE.search("coordinates 74.8573612345")
    assert not PHONE_RE.search("id 12345678901234")
    assert not PHONE_RE.search("node/6895197036")
    assert RESERVED_DEMO.match("9999901234")


def test_no_personal_email_addresses():
    offenders = []
    files = _text_files("web", "docs", "data", "chaarteeke", "scripts", "tests", "telephony", ".github")
    files += _top_level_files()
    for p in files:
        for n, line in enumerate(_read(p).splitlines(), 1):
            for m in EMAIL_RE.finditer(line):
                if m.group(1).lower() in PERSONAL_MAIL:
                    offenders.append(f"{p.relative_to(ROOT)}:{n}: {m.group(0)}")
    assert not offenders, "personal mailboxes do not belong in the repository:\n" + "\n".join(
        dict.fromkeys(offenders)
    )


# Numbers the scan must refuse are built from pieces, so this file holds no number the scan would flag.
SOMEONES_NUMBER = "70000" + "12345"
UNLISTED_MADE_UP = "90000" + "00099"


def test_the_allowances_for_numbers_are_narrow():
    ok = forbidden.number_allowed
    assert ok("9999901234", "web/js/x.js")  # the reserved demo pattern is allowed anywhere
    assert ok("9876543210", "tests/core/test_x.py") and ok("9876543210", "chaarteeke/telephony/numbers.py")
    assert not ok(
        "9876543210", "telephony/asterisk/test/extensions_test.conf"
    )  # config a real exchange may load
    assert not ok("9876543210", "docs/TELEPHONY.md") and not ok("9876543210", "web/js/pages/phone.js")
    assert not ok(UNLISTED_MADE_UP, "data/clinics.json")  # no real number is kept in the data either
    assert not ok(UNLISTED_MADE_UP, "docs/PILOT.md")
    assert not ok(UNLISTED_MADE_UP, "tests/core/test_x.py")  # only the listed made-up numbers


def test_a_personal_mailbox_or_number_is_found_and_shown_hidden():
    mailbox = "someone" + "@" + "gmail.com"
    hits = forbidden.hits_in_line(f"write to {mailbox} or call +91 " + SOMEONES_NUMBER + "", "docs/x.md")
    assert sorted(kind for kind, _ in hits) == ["mailbox", "number"]
    assert (
        forbidden.hits_in_line("info" + "@" + "example.org is an organisation's mailbox", "docs/x.md") == []
    )
    shown = forbidden.Hit("abc1234 docs/x.md", "number", forbidden.hide("+91 " + SOMEONES_NUMBER))
    assert SOMEONES_NUMBER[:5] not in str(shown) and str(shown).startswith("abc1234 docs/x.md: number +91")


def _commit(
    repo: Path, files: dict[str, str], message: str = "work", email: str = "dev@users.noreply.github.com"
):
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "dev",
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": "dev",
        "GIT_COMMITTER_EMAIL": email,
    }
    for cmd in (["add", "-A"], ["commit", "-q", "-m", message, "--no-gpg-sign"]):
        subprocess.run(["git", "-C", str(repo), *cmd], env=env, check=True, capture_output=True)


@pytest.fixture
def scratch_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, capture_output=True)
    return repo


def test_the_history_scan_finds_what_a_later_commit_deleted(scratch_repo):
    mailbox = "someone" + "@" + "rediffmail.com"
    _commit(
        scratch_repo, {"docs/a.md": f"contact {mailbox}\n", "docs/b.md": "call +91 " + SOMEONES_NUMBER + "\n"}
    )
    _commit(scratch_repo, {"docs/a.md": "contact the office\n", "docs/b.md": "call the office\n"})
    note, hits = forbidden.scan_history(scratch_repo)
    assert note == ""
    found = sorted((h.where.split()[1], h.kind) for h in hits)
    assert found == [("docs/a.md", "mailbox"), ("docs/b.md", "number")]
    assert not any(
        "someone" in str(h) or SOMEONES_NUMBER[:5] in str(h) for h in hits
    ), "a match must be shown hidden"


def test_the_history_scan_reads_commit_authors_and_messages(scratch_repo):
    _commit(scratch_repo, {"a.txt": "x\n"}, message="by hand", email="me" + "@" + "gmail.com")
    _commit(scratch_repo, {"b.txt": "y\n"}, message="ring +91 " + SOMEONES_NUMBER + " about it")
    _, hits = forbidden.scan_history(scratch_repo)
    assert {h.where.split()[1] for h in hits} == {"author", "committer", "message"}


def test_the_history_scan_passes_clean_history(scratch_repo):
    _commit(
        scratch_repo,
        {
            "web/x.txt": "demo +91 99999 01234\n",
            "tests/test_x.py": "NUMBER = '+919876543210'\n",
            "web/vendor/lib.js": "mail" + "@" + "gmail.com\n",  # vendored code is not ours to scan
        },
    )
    assert forbidden.scan_history(scratch_repo) == ("", [])


def test_the_history_scan_says_it_skipped_a_shallow_clone(scratch_repo, tmp_path):
    _commit(scratch_repo, {"a.txt": "one\n"})
    _commit(scratch_repo, {"a.txt": "two\n"})
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", scratch_repo.as_uri(), str(clone)],
        check=True,
        capture_output=True,
    )
    note, hits = forbidden.scan_history(clone)
    assert "shallow" in note and hits == []


def test_the_gate_scans_the_history():
    assert "scripts/forbidden.py history" in _read(ROOT / "scripts" / "gate.sh")


def test_the_gate_reports_history_findings_as_a_warning_not_a_failure():
    """Commits already on origin/main cannot be fixed by a change; rewriting history is the owner's decision."""
    gate = _read(ROOT / "scripts" / "gate.sh")
    assert 'warn_step "history (no contacts)" uv run --frozen python scripts/forbidden.py history' in gate
    assert "with a warning" in gate  # the summary keeps it visible


# --------------------------------------------------------------------------------------------- the phone exchange
# Asterisk is the one place a patient's number could be written to disk by something other than our app: its
# log, its call detail records and its event log. The configuration may not log the caller, keeps the record
# modules off, and rotates what is left after 7 days.
ASTERISK = ROOT / "telephony" / "asterisk" / "etc" / "asterisk"
ASTERISK_CONFS = sorted((ROOT / "telephony").rglob("*.conf"))
LOGGING_APP = re.compile(r"\b(?:Log|Verbose|NoOp)\s*\(", re.IGNORECASE)
CALLER = re.compile(r"CALLERID\s*\(|CONNECTEDLINE\s*\(|CDR\s*\(\s*(?:src|clid)", re.IGNORECASE)
# modules.conf cannot noload by wildcard, so every call-record and event-log module is named
RECORD_MODULES = {
    "cdr_csv.so", "cdr_custom.so", "cdr_sqlite3_custom.so", "cdr_manager.so",
    "cel_custom.so", "cel_sqlite3_custom.so", "cel_manager.so",
    # the database and RADIUS backends the Debian and Ubuntu package loads (seen on Ubuntu 24.04)
    "cdr_adaptive_odbc.so", "cdr_odbc.so", "cdr_pgsql.so", "cdr_radius.so", "cdr_tds.so",
    "cel_odbc.so", "cel_pgsql.so", "cel_radius.so", "cel_tds.so",
}  # fmt: skip
# the log levels that carry numbers (CLIP lines, channel names, dialled digits)
CHATTY_LEVELS = {"verbose", "debug", "dtmf", "fax"}


def _conf_lines(path: Path):
    """(line number, code) for each line of an Asterisk .conf that is not blank or a comment."""
    for n, line in enumerate(_read(path).splitlines(), 1):
        code = line.split(";", 1)[0]
        if code.strip():
            yield n, code


def test_there_are_asterisk_configs_to_check():
    names = {p.name for p in ASTERISK_CONFS}
    assert {"extensions.conf", "modules.conf", "logger.conf", "extensions_test.conf"} <= names


def test_no_asterisk_config_logs_the_caller():
    offenders = [
        f"{p.relative_to(ROOT)}:{n}: {code.strip()}"
        for p in ASTERISK_CONFS
        for n, code in _conf_lines(p)
        if LOGGING_APP.search(code) and CALLER.search(code)
    ]
    assert not offenders, (
        "the caller's number must not reach Asterisk's log; log the event only:\n" + "\n".join(offenders)
    )


def test_the_caller_pattern_catches_what_it_should():
    def logs_caller(code: str) -> bool:
        return bool(LOGGING_APP.search(code) and CALLER.search(code))

    assert logs_caller(" same => n,Log(ERROR,lost a call from ${CALLERID(num)})")
    assert logs_caller(" same => n,NoOp(${CALLERID(all)})")
    assert logs_caller(" same => n,Verbose(1,${CDR(src)})")
    assert not logs_caller(" same => n,Set(CALLERID(num)=)")  # setting the test caller is not logging it
    assert not logs_caller(" same => n,Log(ERROR,ARI app chaar not running: a missed call was NOT recorded)")


def test_asterisk_keeps_no_call_records():
    noloaded = {
        m.group(1)
        for _, code in _conf_lines(ASTERISK / "modules.conf")
        if (m := re.match(r"\s*noload\s*=>\s*(\S+)", code))
    }
    assert not RECORD_MODULES - noloaded, f"modules.conf must noload: {sorted(RECORD_MODULES - noloaded)}"
    cdr = ASTERISK / "cdr.conf"
    assert cdr.is_file(), "telephony/asterisk/etc/asterisk/cdr.conf is missing"
    code = "\n".join(c for _, c in _conf_lines(cdr))
    enable = re.search(r"^\s*enable\s*=\s*(\w+)", code, re.MULTILINE)
    assert (
        enable and enable.group(1).lower() == "no"
    ), "cdr.conf [general] must say enable=no (the option is 'enable')"


def test_asterisk_does_not_load_the_old_sip_driver():
    """chan_sip binds UDP 5060 on every address; res_pjsip is the SIP stack here, on 127.0.0.1 only."""
    noloaded = {
        m.group(1)
        for _, code in _conf_lines(ASTERISK / "modules.conf")
        if (m := re.match(r"\s*noload\s*=>\s*(\S+)", code))
    }
    assert "chan_sip.so" in noloaded


def test_asterisk_logs_are_not_verbose_and_are_rotated_for_7_days():
    levels = {
        level.strip().lower()
        for _, code in _conf_lines(ASTERISK / "logger.conf")
        if "=>" in code
        for level in code.split("=>", 1)[1].split(",")
    }
    assert (
        not levels & CHATTY_LEVELS
    ), f"logger.conf writes {sorted(levels & CHATTY_LEVELS)}: they carry numbers"
    rotation = [
        p
        for p in (ROOT / "telephony").rglob("*")
        if p.is_file() and "logrotate" in p.relative_to(ROOT).as_posix().lower()
    ]
    assert rotation, "no logrotate file under telephony/ (7-day retention for /var/log/asterisk)"
    text = "\n".join(_read(p) for p in rotation)
    assert "/var/log/asterisk" in text
    assert re.search(r"^\s*maxage\s+7\b", text, re.MULTILINE) or (
        re.search(r"^\s*daily\b", text, re.MULTILINE) and re.search(r"^\s*rotate\s+7\b", text, re.MULTILINE)
    ), "the logrotate file must keep 7 days (maxage 7, or daily with rotate 7)"


def test_the_day_one_checklist_checks_the_record_modules():
    assert "module show like cdr" in _read(ROOT / "docs" / "TELEPHONY.md")


# --------------------------------------------------------------------------------------------- prompts that promise
def test_the_already_signed_up_message_says_what_a_further_missed_call_does():
    """README: a further missed call after the 'you are already signed up' message stops everything. The patient
    hears that message once, so it must say so: a repeat missed call is not harmless."""
    prompts = json.loads(_read(ROOT / "data" / "prompts" / "prompts.json"))["prompts"]
    text = next(e for e in prompts if e["id"] == "ENR_DUP")["text"]
    assert "stop" in text["en"].lower() and "missed call" in text["en"].lower(), text["en"]
    for lang, word in (("hi", "मिस्ड कॉल"), ("doi", "मिस्ड कॉल"), ("ur", "مسڈ کال")):
        assert word in text[lang], f"ENR_DUP {lang} does not mention a missed call"


# --------------------------------------------------------------------------------------------- shares and their interval
# SPEC 10 and docs/DESIGN.md: a notched-share percentage is never shown without its interval and the
# could-not-see count. The pages are checked as built by scripts/ui_check.mjs (check 6); these pins hold the
# words and the call sites that check cannot see, so a new place that formats a share has to be looked at.
FORMATS_A_PERCENT = {
    "web/js/core/format.js",  # defines it
    "web/js/ui/share.js",  # the share component: interval and could-not-see in the same section
    "web/js/pages/team/lib.js",  # shareLine and unsureLine
    "web/js/pages/team.js",  # the bounds sentence inside the (i) popover, beside its share
}


def _js_files() -> list[Path]:
    return [p for p in _text_files("web/js") if p.suffix == ".js"]


def test_every_place_that_formats_a_percent_is_known():
    users = {
        p.relative_to(ROOT).as_posix() for p in _js_files() if re.search(r"\bformatPercent\s*\(", _read(p))
    }
    assert users <= FORMATS_A_PERCENT, (
        f"{sorted(users - FORMATS_A_PERCENT)} formats a percentage. If it is a notched share, show the interval "
        "and 'Could not see' beside it (shareDisplay, shareLine + unsureLine) and add the file to FORMATS_A_PERCENT."
    )


def test_every_one_line_share_is_followed_by_its_could_not_see_count():
    offenders = []
    for p in _js_files():
        text = _read(p)
        calls = [ln for ln in text.splitlines() if re.search(r"\b(?:shareLine|shareText)\s*\(", ln)]
        calls = [ln for ln in calls if not re.match(r"\s*export\s+(?:async\s+)?function\b", ln)]
        if calls and not re.search(r"\b(?:unsureLine|shareNotes)\s*\(", text):
            offenders.append(p.relative_to(ROOT).as_posix())
    assert not offenders, f"these print a share line and never its could-not-see count: {offenders}"


def test_the_share_component_names_its_interval_and_could_not_see():
    text = _read(ROOT / "web" / "js" / "ui" / "share.js")
    assert 't("share.interval"' in text and 't("share.couldNotSee")' in text


def test_share_lines_carry_the_interval_and_the_could_not_see_word_in_every_language():
    checked = set()
    for common in COMMON:
        lang = common.name.split(".")[1]
        if not any((I18N / f"{area}.{lang}.json").is_file() for area in ("team", "oversight")):
            continue  # a patient-only language (Dogri, Urdu) shows no share
        words = _bundle(common.name)
        interval = words.get("share.interval", "").replace("{range}", "").strip()
        could = words.get("share.couldNotSee", "").strip()
        assert interval and "{range}" in words["share.interval"], f"{common.name}: share.interval"
        assert could, f"{common.name}: share.couldNotSee"
        for area in ("team", "oversight"):
            if not (I18N / f"{area}.{lang}.json").is_file():
                continue
            strings = _bundle(f"{area}.{lang}.json")
            line = strings.get(f"{area}.shareLine", "")
            for slot in ("{pct}", "{range}", "{n}"):
                assert slot in line, f"{area}.{lang}.json: {area}.shareLine has no {slot}"
            assert interval in line, f"{area}.{lang}.json: {area}.shareLine does not say '{interval}'"
            count = strings.get(f"{area}.couldNotSee", "")
            assert count.startswith(could) and "{n}" in count, f"{area}.{lang}.json: {area}.couldNotSee"
            checked.add((area, lang))
    assert {("team", "en"), ("team", "hi"), ("oversight", "en"), ("oversight", "hi")} <= checked


# --------------------------------------------------------------------------------------------- licences
@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_licence_files_exist(name):
    assert (ROOT / name).is_file() and _read(ROOT / name).strip()


@pytest.mark.xfail(
    not (ROOT / "docs" / "DATA-LICENCES.md").is_file(),
    reason="docs/DATA-LICENCES.md (every dataset and model with its licence) is not written yet",
    strict=True,
)
def test_data_licences_file_exists():
    path = ROOT / "docs" / "DATA-LICENCES.md"
    assert path.is_file() and _read(path).strip()


# --------------------------------------------------------------------------------------------- git
TRAILER = re.compile(r"^(co-authored-by|claude-session)\s*:", re.IGNORECASE | re.MULTILINE)


def test_no_assistant_trailers_in_commit_messages():
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("not a git checkout")
    log = subprocess.run(
        ["git", "-C", str(ROOT), "log", "--format=@@commit %h%n%B"], capture_output=True, text=True
    )
    if log.returncode != 0:
        pytest.skip(f"git log failed: {log.stderr.strip()}")
    bad = []
    for block in log.stdout.split("@@commit ")[1:]:
        sha, _, body = block.partition("\n")
        if TRAILER.search(body):
            bad.append(sha)
    assert not bad, f"Co-Authored-By or Claude-Session trailer in commit(s): {bad}"


# --------------------------------------------------------------------------------------------- the gate itself
@pytest.mark.parametrize("name", ["gate.sh", "smoke.sh", "smoke_checks.py", "forbidden.py"])
def test_the_scripts_people_run_are_executable(name):
    path = ROOT / "scripts" / name
    assert path.is_file() and os.access(path, os.X_OK), f"scripts/{name} must be executable (chmod +x)"


# --------------------------------------------------------------------------------------------- the server check
smoke_checks = _script("smoke_checks")


def _fake_server(overrides: dict[str, object] | None = None):
    """A tiny HTTP server that answers the way a healthy demo does; `overrides` replaces one path's answer."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    library = smoke_checks.MAP_LIBRARY
    answers: dict[str, object] = {
        "/": ("text/html", b"<!doctype html><html><body>Chaar Teeke</body></html>"),
        "/api/auth/visitor": {"role": "visitor"},
        "/api/mode": {"mode": "demo"},
        "/i18n/common.en.json": {"frame": {"demo": {"label": "DEMO DATA:"}}},
        "/api/coverage": {"city": {"status": "ok", "n": 12}},
        "/data/wards.geojson": {"type": "FeatureCollection", "features": [{}]},
        library: ("text/javascript", (ROOT / "web" / library.removeprefix("/")).read_bytes()),
        "/api/events": ("text/event-stream", b"retry: 5000\n\n"),
        "/api/demo/state": {"offset_seconds": 0},
        **(overrides or {}),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            answer = answers.get(self.path)
            if answer is None:
                self.send_error(404)
                return
            kind, body = (
                answer if isinstance(answer, tuple) else ("application/json", json.dumps(answer).encode())
            )
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _server_check(overrides=None) -> tuple[bool, str]:
    import contextlib
    import io

    server, url = _fake_server(overrides)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            ok = smoke_checks.url_stage(url)
    finally:
        server.shutdown()
        server.server_close()
    return ok, out.getvalue()


def test_the_server_check_passes_a_healthy_demo():
    ok, out = _server_check()
    assert ok, out
    assert "8 of 8 server checks passed" in out


@pytest.mark.parametrize(
    ("path", "answer", "named"),
    [
        ("/api/demo/state", {"offset_seconds": 3600}, "clock"),
        ("/api/coverage", {"city": {"status": "too_few", "n": 4}}, "coverage"),
        ("/api/coverage", {"city": {"status": "ok", "n": 0}}, "coverage"),
        ("/api/mode", {"mode": "pilot"}, "demo label"),
        ("/i18n/common.en.json", {"frame": {"demo": {"label": "Demo"}}}, "demo label"),
        ("/data/wards.geojson", {"type": "FeatureCollection", "features": []}, "wards"),
        ("/vendor/maplibre-gl-4.7.1/maplibre-gl.js", ("text/javascript", b"// changed"), "map js"),
        ("/api/events", ("application/json", b"{}"), "stream"),
        ("/api/auth/visitor", {"role": "clinic"}, "visitor"),
    ],
)
def test_the_server_check_fails_and_names_what_is_wrong(path, answer, named):
    ok, out = _server_check({path: answer})
    assert not ok
    assert any(line.startswith("FAIL") and named in line for line in out.splitlines()), out


def test_the_server_check_fails_when_nothing_answers():
    server, url = _fake_server()
    server.shutdown()
    server.server_close()
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert not smoke_checks.url_stage(url)
    assert "nothing answers" in out.getvalue()


def test_smoke_runs_the_server_check_only_when_asked():
    smoke = _read(ROOT / "scripts" / "smoke.sh")
    assert "--url" in smoke and "smoke_checks.py url" in smoke and "smoke_checks.py env" in smoke


def test_ci_runs_the_gate_on_push_and_pull_request_without_secrets():
    ci = _read(ROOT / ".github" / "workflows" / "ci.yml")
    assert "scripts/gate.sh" in ci
    assert re.search(r"^\s*push:", ci, re.MULTILINE) and re.search(r"^\s*pull_request:", ci, re.MULTILINE)
    assert "secrets." not in ci, "the gate runs offline and needs no secret"
    assert "pull_request_target" not in ci, "it would run pull-request code with the repository's token"
    assert "fetch-depth: 0" in ci, "the commit-trailer test reads the whole history"


# --- where the product came from is not written anywhere in the repository ------------------------------------
# The words are built from pieces so that this file does not contain them either.
_ORIGIN_WORDS = [
    "hack" + "athon",
    "j" + "sch",
    "fin" + "ale",
    "ju" + "dge",  # and every longer word that starts with it
    "pri" + "ze",
    "short" + "list",
    "lead" + "ljk",
    "myjammu" + ".in/",
    "jammu-smart-city-" + "hack" + "athon",
]
_NOT_SCANNED_DIRS = {".git", "var", "node_modules", ".venv", "__pycache__", ".ruff_cache", ".pytest_cache"}


def _origin_scan_files():
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _NOT_SCANNED_DIRS]
        for name in files:
            path = Path(folder) / name
            raw = path.read_bytes()
            if b"\0" in raw[:8192]:
                continue  # a binary file (audio, fonts, images)
            yield path, raw.decode("utf-8", errors="ignore").lower()


def test_no_tracked_text_file_says_where_the_product_came_from():
    found = [
        f"{path.relative_to(ROOT)}: {word}"
        for path, text in _origin_scan_files()
        for word in _ORIGIN_WORDS
        if word in text
    ]
    assert found == [], "\n".join(found)


def test_no_file_name_says_where_the_product_came_from():
    names = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _NOT_SCANNED_DIRS]
        names += [
            str((Path(folder) / n).relative_to(ROOT))
            for n in [*files, *dirs]
            if any(word in n.lower() for word in _ORIGIN_WORDS)
        ]
    assert names == [], "\n".join(names)


def test_no_commit_message_says_where_the_product_came_from():
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    log = subprocess.run(
        ["git", "log", "--all", "--format=%an %ae %cn %ce %s %b"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.lower()
    assert [word for word in _ORIGIN_WORDS if word in log] == []
