"""Rabies post-exposure regimens (data/regimens.json) and the dose calendar they give.

Day 0 is the date of the first dose (DOMAIN R3). The clinic chooses the regimen (R2); nothing here
infers, switches or shortens one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from .config import DATA_DIR


@dataclass(frozen=True)
class Regimen:
    id: str
    name: str
    route: str
    days: tuple[int, ...]
    enabled: bool

    @property
    def doses(self) -> int:
        return len(self.days)


@dataclass(frozen=True)
class DoseSpec:
    dose_no: int
    offset_days: int
    due_date: date


def load_regimens(path: Path = DATA_DIR / "regimens.json") -> dict[str, Regimen]:
    raw = json.loads(path.read_text())
    out: dict[str, Regimen] = {}
    for r in raw["regimens"]:
        days = tuple(int(d) for d in r["days"])
        if not days or days[0] != 0 or list(days) != sorted(set(days)):
            raise ValueError(f"regimen {r['id']}: days must start at 0 and increase")
        out[r["id"]] = Regimen(r["id"], r["name"], r["route"], days, bool(r["enabled"]))
    return out


@lru_cache(maxsize=1)
def regimens() -> dict[str, Regimen]:
    return load_regimens()


def get(regimen_id: str) -> Regimen:
    try:
        return regimens()[regimen_id]
    except KeyError as e:
        raise KeyError(f"unknown regimen {regimen_id}") from e


def schedule(regimen: Regimen, day0: date) -> list[DoseSpec]:
    return [DoseSpec(i + 1, d, day0 + timedelta(days=d)) for i, d in enumerate(regimen.days)]
