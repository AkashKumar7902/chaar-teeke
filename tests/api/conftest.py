"""Fixtures for the API lane.

A demo database is seeded once per session (on the fixed clock) and copied for each test. Each test gets an
app built with `create_app(start_background=False)` over its own copy of the data folder, whose big files
are links to the real ones. The prompt audio is a tiny test voice, never the real recordings.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from api_support import AnchoredClock, Who, read_logins, ward_numbers, write_voice
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from chaarteeke import clock
from chaarteeke.api import auth
from chaarteeke.api.app import build_context, create_app
from chaarteeke.config import DATA_DIR, Settings
from chaarteeke.demo import seed as seeder

PILOT_SECRET = "pilot-session-key-of-forty-one-characters"  # pilot mode wants at least 32
T0 = datetime(2026, 10, 19, 4, 30, tzinfo=UTC)  # Monday 19 Oct 2026, 10:00 IST
LINKED = ("wards.geojson", "gazetteer.json", "clusters.json", "clinics.json", "regimens.json", "policy.json")


@pytest.fixture(scope="session")
def phone_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory, phone_key) -> Path:
    """The default demo seed in a database file, built on the fixed clock."""
    root = tmp_path_factory.mktemp("seeded")
    settings = Settings(
        _env_file=None, mode="demo", db=root / "seed.db", var_dir=root / "var", asr="off",
        secret_key="seed", phone_key=phone_key, data_dir=DATA_DIR,
    )  # fmt: skip
    with clock.use_clock(clock.FixedClock(T0)):
        ctx = build_context(settings)
        seeder.seed(ctx)
        ctx.db.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        ctx.db.close()
    return root / "seed.db"


@pytest.fixture(scope="session")
def logins(seeded_db) -> dict[str, str]:
    """The staff passwords of the seeded database, read from the file the seed wrote (var/demo_logins.txt)."""
    return read_logins(seeded_db.parent / "var" / "demo_logins.txt")


@pytest.fixture(scope="session")
def voice_dir(tmp_path_factory) -> Path:
    """A complete test voice (tiny WAV files for every prompt, clinic and ward). Read-only for tests."""
    root = tmp_path_factory.mktemp("voice")
    write_voice(root / "audio", DATA_DIR / "prompts" / "prompts.json")
    return root / "audio"


def make_data_dir(root: Path, audio: Path | None) -> Path:
    data = root / "data"
    (data / "prompts").mkdir(parents=True)
    for name in LINKED:
        (data / name).symlink_to(DATA_DIR / name)
    (data / "prompts" / "prompts.json").symlink_to(DATA_DIR / "prompts" / "prompts.json")
    if audio is not None:
        (data / "prompts" / "audio").symlink_to(audio)
    return data


@pytest.fixture
def clock_t0():
    """Time stands still at T0. Building an app installs its own clock, so the `app` fixture puts this one
    back; an app made by hand with `make_app` needs `clock.install(clock_t0)` after it."""
    c = clock.FixedClock(T0)
    with clock.use_clock(c):
        yield c


@pytest.fixture
def make_app(tmp_path, seeded_db, phone_key, voice_dir) -> Callable[..., object]:
    """make_app(voice=True, seeded=True, background=False, **settings) -> the FastAPI app. The clock is left as the app set it;
    the `clock_t0` or `moving_clock` fixtures replace it afterwards."""
    saved = clock.current()
    apps = []

    def build(*, voice: bool = True, seeded: bool = True, background: bool = False, **overrides):
        n = len(apps)
        root = tmp_path / f"app{n}"
        root.mkdir()
        db = root / "app.db"
        if seeded:
            shutil.copy(seeded_db, db)
        audio = voice_dir if voice else root / "audio"
        if not voice:
            audio.mkdir()
        fields = dict(
            _env_file=None, mode="demo", db=db, var_dir=root / "var", asr="off", telephony="sim",
            secret_key="test-secret", phone_key=phone_key, scheduler=False, data_dir=make_data_dir(root, audio),
        )  # fmt: skip
        if overrides.get("mode") == "pilot":  # pilot refuses a short session key and a missing public address
            fields.update(secret_key=PILOT_SECRET, public_url="http://ct.example.org")
        fields.update(overrides)
        app = create_app(Settings(**fields), start_background=background)
        apps.append(app)
        return app

    yield build
    for app in apps:
        app.state.ctx.db.close()
    clock.install(saved)


@pytest.fixture
def app(make_app, clock_t0):
    built = make_app()
    clock.install(clock_t0)  # the app put its own clock in; time stands still for the test
    return built


@pytest.fixture
def ctx(app):
    return app.state.ctx


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def moving_clock():
    """A clock that runs in real time but started at T0, so the demo clock can be jumped."""
    with clock.use_clock(AnchoredClock(T0)) as c:
        yield c


@pytest.fixture
def people(ctx, logins) -> dict[str, Who]:
    """One session for every kind of person: staff by role, a visitor for each view, and nobody."""
    out: dict[str, Who] = {"out": Who(None, None)}
    for role in ("clinic", "abc", "vet", "oversight", "reviewer", "admin"):
        token, csrf = auth.sign_in(ctx, role, logins[role])
        out[role] = Who(token, csrf)
    for view in auth.VISITOR_VIEWS:
        token, csrf = auth.enter_as_visitor(ctx, view)
        out[f"visitor-{view}"] = Who(token, csrf)
    return out


@pytest.fixture
def wards(ctx) -> list[int]:
    return ward_numbers(ctx)


@pytest.fixture
def people_for(logins):
    """people_for(app, role) -> a login for that role on an app made by hand."""

    def make(app, role: str) -> Who:
        token, csrf = auth.sign_in(app.state.ctx, role, logins[role])
        return Who(token, csrf)

    return make
