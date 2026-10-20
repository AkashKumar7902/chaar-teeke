"""Shared by the stats tests (a unique module name, because tests/ has several conftest.py files)."""

from __future__ import annotations

from datetime import UTC, datetime

from cryptography.fernet import Fernet

from chaarteeke.config import Settings
from chaarteeke.context import AppContext
from chaarteeke.crypto import PhoneVault
from chaarteeke.db import Database
from chaarteeke.events import EventBus
from chaarteeke.policy import load_policy
from chaarteeke.stats.clusters import Clusters

T0 = datetime(2026, 10, 19, 4, 30, tzinfo=UTC)  # Monday 19 Oct 2026, 10:00 IST (same as tests/conftest.py)


def build_ctx(tmp_path, mode: str = "demo") -> AppContext:
    settings = Settings(
        _env_file=None,
        mode=mode,
        db=tmp_path / "t.db",
        var_dir=tmp_path / "var",
        telephony="sim",
        asr="off",
        secret_key="test-secret",
        phone_key=Fernet.generate_key().decode(),
    )
    db = Database(":memory:")
    db.migrate()
    ctx = AppContext(
        settings=settings, db=db, policy=load_policy(), vault=PhoneVault(settings.phone_key), bus=EventBus()
    )
    ctx.clusters = Clusters.load()
    return ctx
