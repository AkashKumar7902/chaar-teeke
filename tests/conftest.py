"""Shared fixtures: an in-memory database on a fixed clock with a fresh context."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from chaarteeke import clock
from chaarteeke.config import Settings
from chaarteeke.context import AppContext
from chaarteeke.crypto import PhoneVault
from chaarteeke.db import Database
from chaarteeke.events import EventBus
from chaarteeke.policy import load_policy

# Monday 19 Oct 2026, 10:00 IST
T0 = datetime(2026, 10, 19, 4, 30, tzinfo=UTC)


@pytest.fixture
def fixed_clock():
    c = clock.FixedClock(T0)
    with clock.use_clock(c):
        yield c


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        mode="demo",
        db=tmp_path / "t.db",
        var_dir=tmp_path / "var",
        telephony="sim",
        asr="off",
        secret_key="test-secret",
        phone_key=Fernet.generate_key().decode(),
    )


@pytest.fixture
def ctx(settings, fixed_clock):
    db = Database(":memory:")
    db.migrate()
    c = AppContext(
        settings=settings, db=db, policy=load_policy(), vault=PhoneVault(settings.phone_key), bus=EventBus()
    )
    yield c
    db.close()
