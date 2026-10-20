"""The cluster build: eight contiguous, balanced, deterministic groups of wards."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from chaarteeke.config import DATA_DIR, ROOT
from chaarteeke.stats.clusters import Clusters
from chaarteeke.stats.errors import StatsError


@pytest.fixture(scope="module")
def build():
    spec = importlib.util.spec_from_file_location("build_clusters", ROOT / "scripts" / "build_clusters.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_clusters"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built(build):
    wards = build.load_wards()
    return build, wards, build.build(wards)


def test_eight_clusters_cover_every_ward_once(built):
    _, wards, doc = built
    assert [c["id"] for c in doc["clusters"]] == [f"c{i}" for i in range(1, 9)]
    nos = [n for c in doc["clusters"] for n in c["ward_nos"]]
    assert sorted(nos) == sorted(w.no for w in wards) and len(nos) == len(set(nos)) == 75


def test_every_cluster_is_contiguous(built):
    build, wards, doc = built
    by_no = {w.no: w for w in wards}
    for c in doc["clusters"]:
        assert build.is_connected(set(c["ward_nos"]), by_no), c["id"]


def test_neighbours_come_from_shared_edges_and_the_detached_ward_is_bridged(built):
    _, wards, doc = built
    assert doc["bridges"] and doc["bridges"][0]["ward_no"] == 71
    assert doc["bridges"][0]["gap_m"] < 500
    assert all(w.neighbours for w in wards)


def test_clusters_are_balanced(built):
    _, _, doc = built
    weights = [c["weight"] for c in doc["clusters"]]
    assert max(weights) / min(weights) < 1.3


def test_names_are_plain_and_distinct(built):
    _, _, doc = built
    names = [c["name"] for c in doc["clusters"]]
    assert len(set(names)) == 8
    assert all(" and " in n and "(" not in n and "/" not in n for n in names)
    assert doc["note"] == "Analysis areas, not JMC zones."


def test_build_is_deterministic(build, built):
    _, _, first = built
    again = build.build(build.load_wards())
    assert build.render(again) == build.render(first)


def test_committed_file_is_what_the_script_builds(build, built):
    _, _, doc = built
    assert (DATA_DIR / "clusters.json").read_text() == build.render(doc)


def test_population_matches_the_source(built):
    _, wards, doc = built
    assert sum(c["population_2016_est"] for c in doc["clusters"]) == sum(w.population or 0 for w in wards)
    missing = sorted(n for c in doc["clusters"] for n in c["wards_without_population"])
    assert missing == [72, 73, 74, 75]


def test_clusters_class_looks_wards_up():
    clusters = Clusters.load()
    assert len(clusters.all()) == 8
    assert clusters.for_ward(27) == next(c.id for c in clusters.all() if 27 in c.ward_nos)
    assert clusters.get("c1").name
    assert clusters.note.startswith("Analysis areas")
    with pytest.raises(StatsError) as e:
        clusters.for_ward(999)
    assert e.value.code == "unknown_ward"
    with pytest.raises(StatsError):
        clusters.get("c99")


def test_clusters_accepts_a_dict_or_a_path():
    raw = json.loads(Path(DATA_DIR / "clusters.json").read_text())
    assert Clusters(raw).all() == Clusters(DATA_DIR / "clusters.json").all()
