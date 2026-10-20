"""The 8 analysis clusters (data/clusters.json) and the ward table (data/wards.geojson)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import DATA_DIR
from .errors import StatsError


@dataclass(frozen=True)
class Cluster:
    id: str
    name: str
    ward_nos: tuple[int, ...]
    population: int  # sum of population_2016_est where the source has one
    wards_without_population: tuple[int, ...]


@dataclass(frozen=True)
class WardInfo:
    ward_no: int
    name: str
    population: int | None
    area_km2: float


class Clusters:
    note: str

    def __init__(self, source: dict[str, Any] | Path | str) -> None:
        raw = source if isinstance(source, dict) else json.loads(Path(source).read_text())
        self.note = raw["note"]
        self._clusters = [
            Cluster(
                id=c["id"],
                name=c["name"],
                ward_nos=tuple(c["ward_nos"]),
                population=int(c["population_2016_est"]),
                wards_without_population=tuple(c["wards_without_population"]),
            )
            for c in raw["clusters"]
        ]
        self._by_id = {c.id: c for c in self._clusters}
        self._by_ward = {w: c.id for c in self._clusters for w in c.ward_nos}
        if len(self._by_id) != len(self._clusters):
            raise ValueError("duplicate cluster id")

    @classmethod
    def load(cls, path: Path | None = None) -> Clusters:
        return cls(path or DATA_DIR / "clusters.json")

    def for_ward(self, ward_no: int) -> str:
        try:
            return self._by_ward[ward_no]
        except KeyError as e:
            raise StatsError("unknown_ward", f"Ward {ward_no} is not in any analysis area") from e

    def all(self) -> list[Cluster]:
        return list(self._clusters)

    def get(self, cluster_id: str) -> Cluster:
        try:
            return self._by_id[cluster_id]
        except KeyError as e:
            raise StatsError("unknown_cluster", "There is no such area") from e


@lru_cache(maxsize=4)
def ward_table(path: Path | None = None) -> dict[int, WardInfo]:
    """Ward number -> name, population and area, read once from the ward outlines file."""
    raw = json.loads((path or DATA_DIR / "wards.geojson").read_text())
    out: dict[int, WardInfo] = {}
    for f in raw["features"]:
        p = f["properties"]
        pop = p.get("population_2016_est")
        out[int(p["ward_no"])] = WardInfo(
            int(p["ward_no"]),
            str(p["ward_name"]),
            int(pop) if pop is not None else None,
            float(p["area_km2"]),
        )
    return out
