"""How often look-here flags a cluster when no cluster differs from the rest (Monte Carlo).

Eight clusters share one true notched share. Each week every cluster gets n street-dog answers (the demo
seed gives about 8 per cluster per week). The check is made every week for 12 weeks, as the page does, on the
same latest 4 full weeks coverage uses. Any flag at all is a false flag, because nothing differs.

Run `uv run python tests/stats/test_stats_lookhere_false_flags.py` for the long run (2000 trials) that
docs/EVALS.md quotes. The tests below use a short run with the same seed rule.
"""

from __future__ import annotations

import random

from chaarteeke.policy import load_policy
from chaarteeke.stats.lookhere import Counts, candidates

CLUSTERS = 8
WARM_UP_WEEKS = 4  # one 4-week window is needed before the first check
CHECKS = 12


def _window(series: list[int], per_week: int, a: int, b: int) -> Counts:
    x = sum(series[a:b])
    return Counts(x, per_week * (b - a) - x)


def simulate(p: float, per_week: int, trials: int, seed: int = 20261019) -> dict[str, float]:
    """False-flag rates. `check`: a given weekly check flags at least one cluster. `12w`: at least one of the
    12 weekly checks does."""
    policy = load_policy()
    rng = random.Random(seed)
    weeks = WARM_UP_WEEKS + CHECKS
    checks_hit = trials_hit = 0
    for _ in range(trials):
        data = [[rng.binomialvariate(per_week, p) for _ in range(weeks)] for _ in range(CLUSTERS)]
        ever = False
        for t in range(WARM_UP_WEEKS, weeks):
            clusters = {f"c{k}": _window(data[k], per_week, t - 4, t) for k in range(CLUSTERS)}
            city = Counts(
                sum(c.notched for c in clusters.values()), sum(c.unnotched for c in clusters.values())
            )
            hit = bool(candidates(policy, city, clusters))
            checks_hit += hit
            ever |= hit
        trials_hit += ever
    return {"check": checks_hit / (trials * CHECKS), "12w": trials_hit / trials}


def test_the_corrected_interval_rarely_flags_when_nothing_differs():
    rates = simulate(p=0.65, per_week=8, trials=300)
    assert rates["check"] < 0.08, rates  # a few percent of weekly checks at most
    assert rates["12w"] < 0.5, rates  # but looking every week for 12 weeks adds up


def test_the_flag_rate_is_low_at_other_shares_too():
    for p in (0.5, 0.8):
        rates = simulate(p=p, per_week=8, trials=200)
        assert rates["check"] < 0.08, (p, rates)


def test_fewer_answers_than_the_minimum_never_flag():
    assert simulate(p=0.65, per_week=5, trials=50)["12w"] == 0.0  # 20 per window: under min_n


if __name__ == "__main__":
    print("p, answers per cluster per week: false flags (check = one weekly check, 12w = any of 12 checks)")
    for share in (0.65, 0.8):
        for per_week in (8, 12):
            r = simulate(share, per_week, trials=2000)
            print(f"{share:.2f}, {per_week:>2}: " + ", ".join(f"{k} {v * 100:.1f}%" for k, v in r.items()))
