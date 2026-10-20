"""Which clusters to look at first (SPEC s10), guarded against the many-looks problem.

Each cluster is compared with the rest of the city (the city without that cluster), never with a city that
contains it, and the rest's own uncertainty counts. A cluster is flagged only when its notched share over
the last 4 full weeks is below the rest of the city's by more than chance explains: the Newcombe interval for
the difference of two shares, built from Wilson intervals at the Bonferroni-corrected policy lookhere_z,
lies wholly above zero. Both the cluster and the rest need at least min_n answers. At most lookhere_max are
returned, sorted by area id (the choice of which ones is by strength, but the list carries no rank). Reasons
carry no percentage: a share without its interval never goes on screen.

The correction is for looking at 8 clusters at once, not for looking every week: with 8 clusters and no real
difference it flagged someone in 3 to 4% of weekly checks, and in 25 to 32% of 12-week stretches
(tests/stats/test_stats_lookhere_false_flags.py, 2000 trials). A second rule, "below the rest in each of two
consecutive 4-week windows", was dropped: a cluster is below the rest in one window about half the time by
chance alone, so with 8 clusters it flagged someone in about 93% of checks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..policy import Policy
from .intervals import wilson


@dataclass(frozen=True)
class Counts:
    notched: int
    unnotched: int

    @property
    def n(self) -> int:
        return self.notched + self.unnotched

    @property
    def p(self) -> float:
        return self.notched / self.n

    def minus(self, other: Counts) -> Counts:
        return Counts(self.notched - other.notched, self.unnotched - other.unnotched)


@dataclass(frozen=True)
class Flag:
    cluster_id: str
    reason: str  # one English sentence, no percentage
    data: dict[str, str | int]  # the same reason as numbers, so a page can say it in its own language


def difference_low(rest: Counts, here: Counts, z: float) -> float:
    """Lower end of the Newcombe interval for (rest share - this cluster's share), from the two Wilson
    intervals. Above zero means the cluster is below the rest by more than chance explains at this z."""
    l1, _ = wilson(rest.notched, rest.n, z)
    _, u2 = wilson(here.notched, here.n, z)
    return (rest.p - here.p) - math.sqrt((rest.p - l1) ** 2 + (u2 - here.p) ** 2)


def candidates(
    policy: Policy,
    city: Counts,
    clusters: dict[str, Counts],
) -> list[tuple[tuple[float, str], Flag]]:
    """Every cluster whose gap to the rest of the city clears the corrected interval, with its sort key (the
    larger gap first). `city` and each cluster are the counts of the latest 4 full weeks; the city includes
    the cluster, which is taken out here."""
    cfg = policy["stats"]
    min_n = cfg["min_n_for_share"]
    z = cfg["lookhere_z"]
    found: list[tuple[tuple[float, str], Flag]] = []
    for cid, here in clusters.items():
        rest = city.minus(here)
        if here.n < min_n or rest.n < min_n or difference_low(rest, here, z) <= 0:
            continue
        reason = (
            "Fewer of the street dogs that bit had the notch here than in the rest of the city in the "
            f"last 4 full weeks, by more than chance explains ({here.n} answers here)."
        )
        data = {
            "kind": "strong",
            "n": here.n,
            "now_notched": here.notched,
            "now_unnotched": here.unnotched,
            "rest_now_notched": rest.notched,
            "rest_now_unnotched": rest.unnotched,
        }
        found.append(((-(rest.p - here.p), cid), Flag(cid, reason, data)))
    return found


def evaluate(policy: Policy, city: Counts, clusters: dict[str, Counts]) -> list[Flag]:
    """The flags to show: at most lookhere_max, the larger gaps first by the sort key, then listed by area id."""
    ranked = sorted(candidates(policy, city, clusters), key=lambda r: r[0])
    kept = [flag for _, flag in ranked[: policy["stats"]["lookhere_max"]]]
    return sorted(kept, key=lambda f: f.cluster_id)
