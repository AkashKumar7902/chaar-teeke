"""The composition root's bundle. Library code takes what it needs from here as arguments."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .crypto import PhoneVault
from .db import Database
from .events import EventBus
from .policy import Policy


@dataclass
class AppContext:
    settings: Settings
    db: Database
    policy: Policy
    vault: PhoneVault
    bus: EventBus
    telephony: Any = None  # chaarteeke.telephony.model.TelephonyPort, set by main/app startup
    transcriber: Any = None  # chaarteeke.speech.asr.Transcriber or None when CT_ASR=off
    prompts: Any = None  # chaarteeke.ivr.prompts.PromptCatalogue
    gazetteer: Any = None  # chaarteeke.places.gazetteer.Gazetteer
    clusters: Any = None  # chaarteeke.stats.clusters.Clusters
    runner: Any = None  # chaarteeke.ivr.runner.CallRunner
    scheduler: Any = None  # chaarteeke.scheduler.Scheduler
    extras: dict[str, Any] = field(default_factory=dict)
