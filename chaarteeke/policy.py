"""Operator policy: every threshold lives in data/policy.json, never in code."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import DATA_DIR


class Policy:
    def __init__(self, raw: dict[str, Any], digest: str) -> None:
        self.raw = raw
        self.version: str = raw["version"]
        self.digest = digest

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.raw[section]


def load_policy(path: Path = DATA_DIR / "policy.json") -> Policy:
    data = path.read_bytes()
    return Policy(json.loads(data), hashlib.sha256(data).hexdigest())


@lru_cache(maxsize=1)
def get_policy() -> Policy:
    return load_policy()
