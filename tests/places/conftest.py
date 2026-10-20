"""Fixtures for the speech and places lane."""

from __future__ import annotations

import pytest
from places_support import FakeTranscriber, World, real_gazetteer

from chaarteeke.stats.clusters import Clusters


@pytest.fixture(scope="session")
def gaz():
    """The real gazetteer without a database (no learned names). Built once; the index is cached."""
    g = real_gazetteer()
    g.index()
    return g


@pytest.fixture
def pctx(ctx):
    """The shared ctx with the real gazetteer (reading this test's aliases) and the analysis clusters."""
    ctx.gazetteer = real_gazetteer(ctx.db)
    ctx.clusters = Clusters.load()
    return ctx


@pytest.fixture
def world(pctx):
    return World(pctx)


@pytest.fixture
def says():
    """Make a transcriber that says one thing."""

    def make(text: str, **kw) -> FakeTranscriber:
        return FakeTranscriber(text=text, **kw)

    return make
