"""The place-name index: ward names, landmarks and roads from data/gazetteer.json, plus names a reviewer taught.

Every written form of every name is folded with `normalise` and filed under the wards it can mean. A form
that can mean several wards is ambiguous and the matcher offers all of them, never one. Three things make a
form ambiguous: the name is shared by several wards ("Gandhi Nagar"), a road crosses several wards, or the
place sits where two ward polygons overlap (checked here from the polygons, with the sliver's coordinates).
A post office located only by its pincode centre is a guess at the ward and is never auto-matched.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db import Database
from .normalise import NormText, is_distinctive, normalise


@dataclass(frozen=True)
class WardRef:
    ward_no: int
    name: str


@dataclass(frozen=True)
class LearnedAlias:
    text_norm: str
    ward_no: int
    added_by: str
    added_at: str


@dataclass(frozen=True)
class Source:
    """One gazetteer entry (or learned alias) behind a written form."""

    ward_nos: tuple[int, ...]
    name: str
    kind: str  # gazetteer type, or "learned"
    sure: bool = True  # False: the ward is a guess, so the form can be offered but not auto-matched


@dataclass
class Form:
    latin: str
    tokens: tuple[str, ...]
    joined: str
    phonetic: str
    sources: list[Source] = field(default_factory=list)

    @property
    def ward_nos(self) -> frozenset[int]:
        return frozenset(w for s in self.sources for w in s.ward_nos)

    @property
    def ambiguous(self) -> bool:
        return len(self.ward_nos) > 1 or any(not s.sure for s in self.sources)

    def evidence_for(self, ward_no: int) -> str:
        names = sorted({s.name for s in self.sources if ward_no in s.ward_nos})
        return names[0] if names else ""


@dataclass
class WrittenForm:
    """One name exactly as written (its own script, not folded), with the entries that carry it. The keyword
    spotter searches these in the sound, so it needs the spelling a speech model would write."""

    text: str
    sources: list[Source] = field(default_factory=list)

    @property
    def ward_nos(self) -> frozenset[int]:
        return frozenset(w for s in self.sources for w in s.ward_nos)

    @property
    def ambiguous(self) -> bool:
        return len(self.ward_nos) > 1 or any(not s.sure for s in self.sources)

    def evidence_for(self, ward_no: int) -> str:
        names = sorted({s.name for s in self.sources if ward_no in s.ward_nos})
        return names[0] if names else ""


@dataclass
class Index:
    forms: list[Form]
    latin: list[str]
    joined: list[str]
    phonetic: list[str]
    by_token: dict[str, list[int]]
    vocab: list[str]
    deva: dict[str, list[int]]  # folded Devanagari spelling -> forms written that way


# ----------------------------------------------------------------------------- geometry
def _ring_contains(ring: list[list[float]], x: float, y: float) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _polygon_contains(poly: list[list[list[float]]], x: float, y: float) -> bool:
    return _ring_contains(poly[0], x, y) and not any(_ring_contains(h, x, y) for h in poly[1:])


@dataclass
class _WardShape:
    ward_no: int
    polygons: list[list[list[list[float]]]]
    bbox: tuple[float, float, float, float]

    def contains(self, lon: float, lat: float) -> bool:
        x0, y0, x1, y1 = self.bbox
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            return False
        return any(_polygon_contains(p, lon, lat) for p in self.polygons)


def _shape(feature: dict[str, Any]) -> _WardShape:
    geom = feature["geometry"]
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    xs = [pt[0] for p in polys for pt in p[0]]
    ys = [pt[1] for p in polys for pt in p[0]]
    return _WardShape(feature["properties"]["ward_no"], polys, (min(xs), min(ys), max(xs), max(ys)))


def _read(source: dict[str, Any] | Path | str) -> dict[str, Any]:
    return source if isinstance(source, dict) else json.loads(Path(source).read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- the gazetteer
class Gazetteer:
    """`Gazetteer(gazetteer_json, wards_geojson, db)`: each source is a dict or a path; db may be None
    (no learned names), as in the offline evaluation."""

    def __init__(
        self,
        gazetteer_json: dict[str, Any] | Path | str,
        wards_geojson: dict[str, Any] | Path | str,
        db: Database | None = None,
    ) -> None:
        raw = _read(gazetteer_json)
        self.meta: dict[str, Any] = raw.get("meta", {})
        self._entries: list[dict[str, Any]] = raw["entries"]
        geo = _read(wards_geojson)
        self._shapes = [_shape(f) for f in geo["features"]]
        self._ward_names = {f["properties"]["ward_no"]: f["properties"]["ward_name"] for f in geo["features"]}
        self._db = db
        self._lock = threading.Lock()
        self._static: tuple[list[Form], dict[str, set[str]]] | None = None
        self._index: Index | None = None
        self._index_key: tuple[int, int] | None = None

    @classmethod
    def load(cls, data_dir: Path, db: Database | None = None) -> Gazetteer:
        return cls(data_dir / "gazetteer.json", data_dir / "wards.geojson", db)

    # ------------------------------------------------------------------ plain lookups
    def ward_name(self, ward_no: int) -> str:
        try:
            return self._ward_names[ward_no]
        except KeyError:
            raise KeyError(f"there is no ward {ward_no}") from None

    def has_ward(self, ward_no: int) -> bool:
        return ward_no in self._ward_names

    def wards(self) -> list[WardRef]:
        return [WardRef(n, self._ward_names[n]) for n in sorted(self._ward_names)]

    def entries(self) -> list[dict[str, Any]]:
        return self._entries

    def aliases(self) -> list[LearnedAlias]:
        if self._db is None:
            return []
        rows = self._db.query("SELECT text_norm, ward_no, added_by, added_at FROM aliases ORDER BY id")
        return [LearnedAlias(r["text_norm"], r["ward_no"], r["added_by"], r["added_at"]) for r in rows]

    def wards_at(self, lon: float, lat: float) -> tuple[int, ...]:
        return tuple(sorted(s.ward_no for s in self._shapes if s.contains(lon, lat)))

    # ---------------------------------------------------------------------- the index
    def index(self) -> Index:
        """The searchable index. Rebuilt when a reviewer adds a name (checked by a cheap count)."""
        key = self._aliases_key()
        with self._lock:
            if self._index is None or key != self._index_key:
                self._index = self._build(key)
                self._index_key = key
            return self._index

    def forms_key(self) -> tuple[int, int]:
        """Changes whenever a reviewer teaches a name: whoever caches something made from the forms keys on it."""
        return self._aliases_key()

    def written_forms(self) -> list[WrittenForm]:
        """Every name as written in the data file (any script), plus learned names; same sources and wards as
        `index()`. Order is stable (by text)."""
        forms: dict[str, WrittenForm] = {}

        def add(text: str, src: Source) -> None:
            text = " ".join(text.split())
            if not text:
                return
            f = forms.setdefault(text, WrittenForm(text))
            if src not in f.sources:
                f.sources.append(src)

        for e in self._entries:
            src = self._source_of(e)
            for text in _names_of(e):
                add(text, src)
        for a in self.aliases():
            if self.has_ward(a.ward_no):
                add(a.text_norm, Source((a.ward_no,), a.text_norm, "learned"))
        return [forms[t] for t in sorted(forms)]

    def _aliases_key(self) -> tuple[int, int]:
        if self._db is None:
            return (0, 0)
        row = self._db.one("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM aliases")
        return (row["n"], row["m"])

    def _static_forms(self) -> tuple[list[Form], dict[str, set[str]]]:
        if self._static is None:
            forms: dict[str, Form] = {}
            deva: dict[str, set[str]] = {}
            for e in self._entries:
                self._add_entry(e, forms, deva)
            self._static = (list(forms.values()), deva)
        return self._static

    def _add_entry(self, e: dict[str, Any], forms: dict[str, Form], deva: dict[str, set[str]]) -> None:
        src = self._source_of(e)
        for text in _names_of(e):
            n = normalise(text)
            if not n.distinctive:
                continue
            _file(forms, n, src)
            if n.devanagari_joined:
                deva.setdefault(n.devanagari_joined, set()).add(n.latin)

    def _source_of(self, e: dict[str, Any]) -> Source:
        weak = str(e.get("ward_confidence", "")).startswith("low")
        return Source(self._wards_of(e), e["name"], e["type"], sure=not weak)

    def _wards_of(self, e: dict[str, Any]) -> tuple[int, ...]:
        """The entry's wards, plus every ward whose polygon also holds its point (an overlap sliver)."""
        wards = set(e["ward_nos"])
        lat, lon = e.get("lat"), e.get("lon")
        if lat is not None and lon is not None:
            here = self.wards_at(lon, lat)
            if len(here) > 1 and wards & set(here):
                wards |= set(here)
        return tuple(sorted(wards))

    def _build(self, key: tuple[int, int]) -> Index:
        static, deva_latin = self._static_forms()
        forms: dict[str, Form] = {
            f.latin: Form(f.latin, f.tokens, f.joined, f.phonetic, list(f.sources)) for f in static
        }
        for a in self.aliases():
            if not self.has_ward(a.ward_no):
                continue
            n = normalise(a.text_norm)
            if n.distinctive:
                _file(forms, n, Source((a.ward_no,), a.text_norm, "learned"))
        flist = sorted(forms.values(), key=lambda f: f.latin)
        pos = {f.latin: i for i, f in enumerate(flist)}
        by_token: dict[str, list[int]] = {}
        for i, f in enumerate(flist):
            for t in set(f.tokens):
                if is_distinctive(t):
                    by_token.setdefault(t, []).append(i)
        return Index(
            forms=flist,
            latin=[f.latin for f in flist],
            joined=[f.joined for f in flist],
            phonetic=[f.phonetic for f in flist],
            by_token=by_token,
            vocab=sorted(by_token),
            deva={k: [pos[latin] for latin in sorted(v)] for k, v in deva_latin.items()},
        )


def _file(forms: dict[str, Form], n: NormText, src: Source) -> None:
    f = forms.get(n.latin)
    if f is None:
        f = forms[n.latin] = Form(n.latin, n.tokens, n.latin_joined, n.phonetic)
    if src not in f.sources:
        f.sources.append(src)


_PAREN = re.compile(r"[()/]")
# Words that tell two wards of one locality apart; people say the locality alone.
_QUALIFIERS = frozenset("north south east west central centre upper lower i ii".split())


_WARD_NUMBER = re.compile(r"^(?:ward|ward no|wd|vard|vard no|vd|वर्द|वर्द नंबर|व्द)\s+(\d+)$", re.IGNORECASE)


def _names_of(e: dict[str, Any]) -> list[str]:
    """Every written form of an entry. A ward-number alias ("Ward 12") that names a different number than
    the ward's own is a fault in the data file (ward 55 carries "Ward 5"; 36 aliases do) and is left out,
    so that "ward 5" does not also mean ward 55. The data file should be fixed at its source."""
    own = e["ward_nos"][0] if e["type"] == "ward" and len(e["ward_nos"]) == 1 else None
    aliases = [*e.get("aliases_latin", []), *e.get("aliases_devanagari", [])]
    if own is not None:
        aliases = [
            a for a in aliases if not ((m := _WARD_NUMBER.match(a.strip())) and int(m.group(1)) != own)
        ]
    names = [e["name"], *aliases]
    if e.get("name_hi"):
        names.append(e["name_hi"])
    if e.get("name_ur"):
        names.append(e["name_ur"])
    if e["type"] == "ward":
        parts = [p.strip() for p in _PAREN.split(e["name"]) if p.strip()]
        names += parts
        for part in parts:
            base = " ".join(w for w in part.split() if w.lower() not in _QUALIFIERS)
            if base and base != part:
                names.append(base)
    return names
