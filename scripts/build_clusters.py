#!/usr/bin/env python3
"""Build data/clusters.json: 8 contiguous, population-balanced groups of Jammu's 75 wards.

The clusters are analysis areas for the coverage view, not JMC zones. The build is pure Python and
deterministic: the same data/wards.geojson always gives the same file.

How it works
  1. Two wards are neighbours when their outlines share an edge (both end points equal after snapping
     to 1e-5 degrees, about a metre). A ward that touches no other ward (Tawi Vihar / Sidhra is about
     200 m from the nearest ward across the Tawi) is joined to the nearest ward by vertex distance and
     the join is written to the file as a "bridge".
  2. Each ward gets a weight: population_2016_est where the source has one. Wards without it carry
     their area times the density of the five least dense populated wards (they are the rural fringe).
  3. Weighted k-means on the ward centres picks 8 seed wards, regions grow from the seeds one
     neighbouring ward at a time (always the lightest region first), then wards on a border move to the
     neighbouring region when that evens the weights without breaking either region apart.
  4. Clusters are numbered c1..c8 from north to south and named after their two heaviest wards, in
     English (name) and in Hindi (name_hi, built from the wards' own Hindi names).

Run:  uv run python scripts/build_clusters.py [--check]
"""

from __future__ import annotations

import json
import math
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WARDS_PATH = ROOT / "data" / "wards.geojson"
OUT_PATH = ROOT / "data" / "clusters.json"

K = 8
SNAP_DECIMALS = 5
PERIPHERAL_N = 5
CUT_PENALTY = 0.004
BRIDGE_MAX_M = 500.0
NOTE = "Analysis areas, not JMC zones."


@dataclass
class Ward:
    no: int
    name: str
    name_hi: str
    area_km2: float
    population: int | None
    rings: list[list[tuple[float, float]]]
    weight: float = 0.0
    centre: tuple[float, float] = (0.0, 0.0)  # (x, y) in metres on a local flat grid
    lonlat: tuple[float, float] = (0.0, 0.0)
    neighbours: set[int] = field(default_factory=set)


def short_name(name: str) -> str:
    """A plain place name: 'Barnai / Upper Dharmal' -> 'Barnai', 'Gandhi Nagar (North)' -> 'Gandhi Nagar'."""
    first = name.split("/")[0]
    return " ".join(re.sub(r"\(.*?(\)|$)", "", first).split())


# ----------------------------------------------------------------------------- geometry
def outer_rings(geometry: dict) -> list[list[tuple[float, float]]]:
    if geometry["type"] == "Polygon":
        polys = [geometry["coordinates"]]
    elif geometry["type"] == "MultiPolygon":
        polys = geometry["coordinates"]
    else:
        raise ValueError(f"unsupported geometry {geometry['type']}")
    return [[(float(x), float(y)) for x, y in poly[0]] for poly in polys]


def ring_centroid(ring: list[tuple[float, float]]) -> tuple[float, float, float]:
    """(cx, cy, signed area) by the shoelace formula."""
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:], strict=False):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if a == 0:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys), 0.0
    return cx / (3 * a), cy / (3 * a), a / 2


def ward_centre(rings: list[list[tuple[float, float]]]) -> tuple[float, float]:
    tot = sx = sy = 0.0
    for ring in rings:
        cx, cy, area = ring_centroid(ring)
        sx += cx * abs(area)
        sy += cy * abs(area)
        tot += abs(area)
    if tot == 0:
        pts = [p for r in rings for p in r]
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return sx / tot, sy / tot


def snap(p: tuple[float, float]) -> tuple[float, float]:
    return round(p[0], SNAP_DECIMALS), round(p[1], SNAP_DECIMALS)


def edge_keys(rings: list[list[tuple[float, float]]]) -> set[frozenset[tuple[float, float]]]:
    keys: set[frozenset[tuple[float, float]]] = set()
    for ring in rings:
        for p, q in zip(ring, ring[1:], strict=False):
            a, b = snap(p), snap(q)
            if a != b:
                keys.add(frozenset((a, b)))
    return keys


# ------------------------------------------------------------------------------- loading
def load_wards(path: Path = WARDS_PATH) -> list[Ward]:
    raw = json.loads(path.read_text())
    wards: list[Ward] = []
    for f in raw["features"]:
        p = f["properties"]
        rings = outer_rings(f["geometry"])
        pop = p.get("population_2016_est")
        wards.append(
            Ward(
                no=int(p["ward_no"]),
                name=str(p["ward_name"]),
                name_hi=str(p["name_hi"]),
                area_km2=float(p["area_km2"]),
                population=int(pop) if pop is not None else None,
                rings=rings,
            )
        )
    wards.sort(key=lambda w: w.no)
    if len({w.no for w in wards}) != len(wards):
        raise ValueError("duplicate ward numbers in wards.geojson")
    ref_lat = statistics.fmean(ring_centroid(r)[1] for w in wards for r in w.rings)
    for w in wards:
        lon, lat = ward_centre(w.rings)
        w.lonlat = (lon, lat)
        w.centre = (lon * 111_320 * math.cos(math.radians(ref_lat)), lat * 110_574)
    assign_weights(wards)
    return wards


def assign_weights(wards: list[Ward]) -> None:
    dens = sorted(w.population / w.area_km2 for w in wards if w.population)
    if not dens:
        raise ValueError("no ward carries a population figure")
    fringe_density = statistics.fmean(dens[:PERIPHERAL_N])
    for w in wards:
        w.weight = float(w.population) if w.population else w.area_km2 * fringe_density


# --------------------------------------------------------------------------- adjacency
def find_neighbours(wards: list[Ward]) -> list[dict]:
    """Fill ward.neighbours from shared edges; join detached groups. Returns the bridges made."""
    by_no = {w.no: w for w in wards}
    owners: dict[frozenset, list[int]] = defaultdict(list)
    for w in wards:
        for key in edge_keys(w.rings):
            owners[key].append(w.no)
    for nos in owners.values():
        for a in nos:
            for b in nos:
                if a != b:
                    by_no[a].neighbours.add(b)
    bridges: list[dict] = []
    while True:
        groups = components(wards)
        if len(groups) == 1:
            return bridges
        main = groups[0]
        for group in groups[1:]:
            a, b, gap = nearest_pair(by_no, group, main)
            if gap > BRIDGE_MAX_M:
                raise ValueError(f"ward {a} is {gap:.0f} m from the nearest ward: too far to bridge")
            by_no[a].neighbours.add(b)
            by_no[b].neighbours.add(a)
            bridges.append({"ward_no": a, "joined_to": b, "gap_m": round(gap)})
            break


def components(wards: list[Ward]) -> list[list[int]]:
    by_no = {w.no: w for w in wards}
    seen: set[int] = set()
    groups: list[list[int]] = []
    for w in wards:
        if w.no in seen:
            continue
        stack = [w.no]
        group: list[int] = []
        seen.add(w.no)
        while stack:
            n = stack.pop()
            group.append(n)
            for m in sorted(by_no[n].neighbours):
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        groups.append(sorted(group))
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups


def point_to_segment_m(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """Metres from point p to the segment a-b, on a flat local grid."""
    kx = 111_320 * math.cos(math.radians(p[1]))
    ky = 110_574
    px, py = (p[0] - a[0]) * kx, (p[1] - a[1]) * ky
    dx, dy = (b[0] - a[0]) * kx, (b[1] - a[1]) * ky
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, (px * dx + py * dy) / length))
    return math.hypot(px - t * dx, py - t * dy)


def ring_gap_m(
    from_rings: list[list[tuple[float, float]]], to_rings: list[list[tuple[float, float]]]
) -> float:
    """Smallest distance from a vertex of one ward to an edge of the other, either way round."""
    best = math.inf
    for src, dst in ((from_rings, to_rings), (to_rings, from_rings)):
        for ring in src:
            for p in ring:
                for other in dst:
                    for a, b in zip(other, other[1:], strict=False):
                        best = min(best, point_to_segment_m(p, a, b))
    return best


def bbox_gap_deg(a: Ward, b: Ward) -> float:
    """Gap between two wards' bounding boxes in degrees (0 when they overlap): a cheap way to rank."""

    def box(w: Ward) -> tuple[float, float, float, float]:
        pts = [p for r in w.rings for p in r]
        return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)

    (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = box(a), box(b)
    return math.hypot(max(bx0 - ax1, ax0 - bx1, 0.0), max(by0 - ay1, ay0 - by1, 0.0))


def nearest_pair(by_no: dict[int, Ward], group: list[int], others: list[int]) -> tuple[int, int, float]:
    """The closest (ward in group, ward in others), checking the 8 wards with the nearest bounding box."""
    best: tuple[float, int, int] | None = None
    for a in group:
        cands = sorted(others, key=lambda b: (bbox_gap_deg(by_no[a], by_no[b]), b))[:8]
        for b in cands:
            d = ring_gap_m(by_no[a].rings, by_no[b].rings)
            if best is None or (d, a, b) < best:
                best = (d, a, b)
    if best is None:
        raise ValueError("no wards to bridge")
    return best[1], best[2], best[0]


# -------------------------------------------------------------------------- partitioning
def is_connected(members: set[int], by_no: dict[int, Ward]) -> bool:
    if not members:
        return False
    start = min(members)
    seen = {start}
    stack = [start]
    while stack:
        n = stack.pop()
        for m in by_no[n].neighbours:
            if m in members and m not in seen:
                seen.add(m)
                stack.append(m)
    return len(seen) == len(members)


def pick_seeds(wards: list[Ward], k: int) -> list[int]:
    total = sum(w.weight for w in wards)
    mean = (
        sum(w.centre[0] * w.weight for w in wards) / total,
        sum(w.centre[1] * w.weight for w in wards) / total,
    )
    chosen = [min(wards, key=lambda w: (math.dist(w.centre, mean), w.no))]
    while len(chosen) < k:
        chosen.append(max(wards, key=lambda w: (min(math.dist(w.centre, c.centre) for c in chosen), -w.no)))
    centres = [c.centre for c in chosen]
    for _ in range(25):
        groups: list[list[Ward]] = [[] for _ in centres]
        for w in wards:
            i = min(range(len(centres)), key=lambda j: (math.dist(w.centre, centres[j]), j))
            groups[i].append(w)
        moved = []
        for i, g in enumerate(groups):
            if not g:
                moved.append(centres[i])
                continue
            t = sum(w.weight for w in g)
            moved.append(
                (sum(w.centre[0] * w.weight for w in g) / t, sum(w.centre[1] * w.weight for w in g) / t)
            )
        if moved == centres:
            break
        centres = moved
    seeds: list[int] = []
    for c in centres:
        pick = min((w for w in wards if w.no not in seeds), key=lambda w: (math.dist(w.centre, c), w.no))
        seeds.append(pick.no)
    return seeds


def grow_regions(wards: list[Ward], seeds: list[int]) -> dict[int, int]:
    by_no = {w.no: w for w in wards}
    owner: dict[int, int] = {s: i for i, s in enumerate(seeds)}
    weight = [by_no[s].weight for s in seeds]
    while len(owner) < len(wards):
        order = sorted(range(len(seeds)), key=lambda i: (weight[i], i))
        for i in order:
            members = [n for n, o in owner.items() if o == i]
            frontier = sorted({m for n in members for m in by_no[n].neighbours if m not in owner})
            if not frontier:
                continue
            t = sum(by_no[n].weight for n in members)
            cx = sum(by_no[n].centre[0] * by_no[n].weight for n in members) / t
            cy = sum(by_no[n].centre[1] * by_no[n].weight for n in members) / t
            pick = min(frontier, key=lambda m: (math.dist(by_no[m].centre, (cx, cy)), m))
            owner[pick] = i
            weight[i] += by_no[pick].weight
            break
        else:
            raise ValueError("a region cannot grow: the ward graph is not connected")
    return owner


def objective(owner: dict[int, int], by_no: dict[int, Ward], k: int) -> float:
    totals = [0.0] * k
    for n, o in owner.items():
        totals[o] += by_no[n].weight
    mean = sum(totals) / k
    spread = sum((t - mean) ** 2 for t in totals) / (mean * mean)
    cut = sum(1 for n, o in owner.items() for m in by_no[n].neighbours if m > n and owner[m] != o)
    return spread + CUT_PENALTY * cut


def refine(owner: dict[int, int], by_no: dict[int, Ward], k: int) -> dict[int, int]:
    owner = dict(owner)
    current = objective(owner, by_no, k)
    for _ in range(2000):
        best: tuple[float, int, int] | None = None
        for n in sorted(owner):
            src = owner[n]
            targets = sorted({owner[m] for m in by_no[n].neighbours if owner[m] != src})
            if not targets:
                continue
            rest = {m for m, o in owner.items() if o == src and m != n}
            if not rest or not is_connected(rest, by_no):
                continue
            for dst in targets:
                owner[n] = dst
                score = objective(owner, by_no, k)
                owner[n] = src
                if score < current - 1e-12 and (best is None or (score, n, dst) < best):
                    best = (score, n, dst)
        if best is None:
            break
        current, n, dst = best
        owner[n] = dst
    return owner


def name_clusters(groups: list[list[Ward]]) -> list[tuple[str, str]]:
    """('A and B', 'A और B') from the two heaviest wards; later clusters step down the list until the English
    name is new. The Hindi name joins the same wards' Hindi names."""
    used: set[str] = set()
    names: list[tuple[str, str]] = []
    for g in groups:
        ranked = sorted(g, key=lambda w: (-w.weight, w.no))
        hindi: dict[str, str] = {}
        for w in ranked:
            hindi.setdefault(short_name(w.name), short_name(w.name_hi))
        by_first: dict[str, str] = {}
        for n in hindi:  # one name per first word, so 'Channi X and Channi Y' does not happen
            by_first.setdefault(n.split()[0], n)
        short = list(by_first.values())
        pairs = [(0, j) for j in range(1, len(short))] + [
            (i, j) for i in range(1, len(short)) for j in range(i + 1, len(short))
        ]
        name = name_hi = None
        for i, j in pairs:
            cand = f"{short[i]} and {short[j]}" if i != j else short[i]
            if cand not in used:
                name, name_hi = cand, f"{hindi[short[i]]} और {hindi[short[j]]}" if i != j else hindi[short[i]]
                break
        if name is None:
            if short[0] not in used:
                name, name_hi = short[0], hindi[short[0]]
            else:
                name, name_hi = f"{short[0]} area {len(used) + 1}", f"{hindi[short[0]]} क्षेत्र {len(used) + 1}"
        used.add(name)
        names.append((name, name_hi))
    return names


def build(wards: list[Ward], k: int = K) -> dict:
    bridges = find_neighbours(wards)
    by_no = {w.no: w for w in wards}
    owner = refine(grow_regions(wards, pick_seeds(wards, k)), by_no, k)
    members: list[list[Ward]] = [[] for _ in range(k)]
    for n, o in owner.items():
        members[o].append(by_no[n])
    for g in members:
        g.sort(key=lambda w: w.no)

    def weighted_lat(g: list[Ward]) -> float:
        return sum(w.lonlat[1] * w.weight for w in g) / sum(w.weight for w in g)

    def weighted_lon(g: list[Ward]) -> float:
        return sum(w.lonlat[0] * w.weight for w in g) / sum(w.weight for w in g)

    members.sort(key=lambda g: (-round(weighted_lat(g), 3), weighted_lon(g)))
    names = name_clusters(members)
    clusters = []
    for i, (g, (name, name_hi)) in enumerate(zip(members, names, strict=True), start=1):
        clusters.append(
            {
                "id": f"c{i}",
                "name": name,
                "name_hi": name_hi,
                "ward_nos": [w.no for w in g],
                "weight": round(sum(w.weight for w in g)),
                "population_2016_est": sum(w.population for w in g if w.population),
                "wards_without_population": [w.no for w in g if not w.population],
            }
        )
    return {
        "version": "2026.10.1",
        "note": NOTE,
        "source": "data/wards.geojson",
        "method": (
            "Wards are neighbours when their outlines share an edge. Clusters are grown from 8 seed wards "
            "and evened by moving border wards. A ward's weight is population_2016_est, or for wards "
            "without it the ward's area times the density of the five least dense populated wards."
        ),
        "bridges": bridges,
        "clusters": clusters,
    }


def render(doc: dict) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    doc = build(load_wards())
    text = render(doc)
    if "--check" in argv:
        if not OUT_PATH.exists() or OUT_PATH.read_text() != text:
            print(f"{OUT_PATH} is out of date: run scripts/build_clusters.py", file=sys.stderr)
            return 1
        print("data/clusters.json is up to date")
        return 0
    OUT_PATH.write_text(text)
    for c in doc["clusters"]:
        print(f"{c['id']}  {len(c['ward_nos']):2d} wards  weight {c['weight']:6d}  {c['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
