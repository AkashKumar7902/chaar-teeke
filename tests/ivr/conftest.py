"""Fixtures for the IVR lane: a test voice (tiny WAV files for every prompt), a fast policy, a world."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from ivr_support import (
    TEST_CLINICS,
    TEST_WARDS,
    VISIT,
    World,
    _prompts_with_qconsent,
    fast_policy,
    write_voice,
)

from chaarteeke.ivr.prompts import PromptCatalogue
from chaarteeke.ivr.runner import CallRunner
from chaarteeke.policy import Policy
from chaarteeke.telephony.sim import SimPort


@pytest.fixture
def policy() -> Policy:
    return fast_policy()


@pytest.fixture(scope="session")
def voice_template(tmp_path_factory) -> tuple[Path, Path]:
    """prompts.json and a complete test voice, built once and shared."""
    root = tmp_path_factory.mktemp("voice")
    pj = root / "prompts.json"
    pj.write_text(_prompts_with_qconsent(), encoding="utf-8")
    write_voice(
        PromptCatalogue(pj, root / "audio", clinic_ids=TEST_CLINICS, ward_nos=TEST_WARDS, times=[VISIT]),
        root / "audio",
    )
    return pj, root / "audio"


@pytest.fixture
def prompts_json(voice_template) -> Path:
    return voice_template[0]


@pytest.fixture
def audio_dir(voice_template) -> Path:
    """The shared test voice. Do not edit it: use `damage` to remove a file for one test."""
    return voice_template[1]


@pytest.fixture
def damage(audio_dir):
    """Remove voice files for the length of one test; they come back afterwards."""
    removed: list[tuple[Path, bytes]] = []

    def remove(*relative: str) -> None:
        for rel in relative:
            path = audio_dir / rel
            removed.append((path, path.read_bytes()))
            path.unlink()

    yield remove
    for path, data in removed:
        path.write_bytes(data)


@pytest.fixture
def catalogue(prompts_json, audio_dir) -> PromptCatalogue:
    return PromptCatalogue(
        prompts_json, audio_dir, clinic_ids=TEST_CLINICS, ward_nos=TEST_WARDS, times=[VISIT]
    )


@pytest_asyncio.fixture
async def ictx(ctx, catalogue, policy):
    """The shared test context with a fast policy, the test voice, the browser phone and a runner."""
    ctx.policy = policy
    ctx.prompts = catalogue
    ctx.telephony = SimPort()
    ctx.runner = CallRunner(ctx)
    await ctx.runner.start()
    yield ctx
    await ctx.runner.stop()
    await ctx.telephony.stop()


@pytest.fixture
def world(ictx) -> World:
    return World(ictx)
