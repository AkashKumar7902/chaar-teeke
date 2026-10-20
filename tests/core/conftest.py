"""Fixtures for the core lane."""

from __future__ import annotations

import pytest
from core_support import FakeRunner, World


@pytest.fixture
def world(ctx):
    return World(ctx)


@pytest.fixture
def runner(ctx):
    ctx.runner = FakeRunner(ctx)
    return ctx.runner


@pytest.fixture
def events(ctx):
    """Every bus event published during the test, as (type, data)."""
    q = ctx.bus.subscribe()

    def drain():
        out = []
        while not q.empty():
            ev = q.get_nowait()
            out.append((ev.type, ev.data))
        return out

    return drain
