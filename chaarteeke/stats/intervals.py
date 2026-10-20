"""Wilson and Manski intervals and the Share shape with small-cell suppression (DOMAIN section G).

x is "notch seen", y is "no notch seen", u is "could not see". The share is x / (x + y); u never enters
the denominator but always travels beside it, and Manski bounds say what the share could be if every
"could not see", and every report whose animal question was not answered, had gone one way or the other.

What the small-cell rule protects. A count under 5 is hidden wherever no percentage sits beside it. Once a
share is shown (25 or more answers) the counts it rests on are shown exactly too: a share with n, a
Wilson interval and Manski bounds gives x, y and u back to anyone who divides, so hiding them would only
look like protection. Where counts add up to a total shown beside them (hide_cells), a hidden cell is also
kept from being worked out by subtraction.
"""

from __future__ import annotations

import math

from ..models import Share
from ..policy import Policy

WITHHELD = "hidden"


def wilson(x: int, n: int, z: float) -> tuple[float, float]:
    """Wilson score interval for x successes in n trials."""
    if n <= 0:
        raise ValueError("no interval without answers")
    if not 0 <= x <= n:
        raise ValueError("x must be between 0 and n")
    p = x / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def manski(x: int, n: int, u: int) -> tuple[float, float]:
    """Bounds on the share if the u unknown answers are all no-notch (low) or all notch (high). The caller
    counts as unknown every report that is not a notch or no-notch answer: could not see, and a street-dog
    bite whose notch question was skipped or not answered."""
    if n + u <= 0:
        raise ValueError("no bounds without answers")
    if not (0 <= x <= n and u >= 0):
        raise ValueError("need 0 <= x <= n and u >= 0")
    return x / (n + u), (x + u) / (n + u)


def _cfg(policy: Policy) -> dict:
    return policy["stats"]


def suppressed_label(policy: Policy) -> str:
    """ "<5" with the policy's number."""
    return f"<{_cfg(policy)['suppress_below']}"


def hide_small(count: int, policy: Policy) -> int | str:
    """A count for outward views: "<5" under the policy threshold (zero included)."""
    return suppressed_label(policy) if count < _cfg(policy)["suppress_below"] else count


def hide_group(
    cells: dict[str, int], policy: Policy, *, open_cells: frozenset[str] | set[str] = frozenset()
) -> tuple[dict[str, int | str], int | str]:
    """Cells that add up to a total which is shown beside them, for outward views. Returns the cells as
    shown and the total as shown.

    A cell under the threshold reads "<5" (zero too). The hidden cells together are the total minus the
    shown cells (the residual), so the residual must not give any of them away. It does when it is one
    small cell, a sum under the threshold, or every hidden cell at its largest (k cells summing to
    k * (threshold - 1)). Then the smallest cell still shown is hidden as well ("hidden"); that cell can
    take up a difference of one either way, so no small cell is left unique. A hidden zero gives away
    nothing the total does not already say. A cell in `open_cells` is shown anyway (a percentage beside
    it gives it back), so it is never hidden here. If there is no cell left to hide and the residual is
    still unsafe, the total reads "hidden" instead.
    """
    low = _cfg(policy)["suppress_below"]
    small = suppressed_label(policy)
    total = sum(cells.values())
    shown: dict[str, int | str] = {
        k: (small if v < low and k not in open_cells else v) for k, v in cells.items()
    }
    if total < low:
        return shown, small
    hidden = [k for k, v in cells.items() if v < low and k not in open_cells]
    residual = sum(cells[k] for k in hidden)
    # Safe: nothing hidden but zeros, or several small cells whose sum is neither under the threshold
    # (it would itself be a small count) nor every cell at its largest. One more hidden cell of 5 or more
    # makes any small cell ambiguous, because that cell can take up a difference of one either way.
    if residual == 0 or low <= residual < len(hidden) * (low - 1):
        return shown, total
    spare = sorted((k for k in cells if k not in hidden and k not in open_cells), key=lambda k: (cells[k], k))
    if not spare:
        return shown, WITHHELD
    shown[spare[0]] = WITHHELD
    return shown, total


def hide_cells(cells: dict[str, int], policy: Policy) -> dict[str, int | str]:
    """The cells of hide_group when the total is shown by the caller as hide_small(sum). The caller shows
    that total with `hide_group(...)[1]` when it can be withheld too."""
    return hide_group(cells, policy)[0]


def street_total(x: int, y: int, u: int, policy: Policy) -> int | str:
    """The street-dog total for outward views. Beside a share (25 or more answers) the three counts are
    shown, so the total is too. Below that, if the could-not-see cell is hidden (1 to 4), the total minus
    the shown notch and no-notch counts would give it back, so the total reads "hidden" too."""
    total = x + y + u
    cfg = _cfg(policy)
    if x + y >= cfg["min_n_for_share"]:
        return total
    if 0 < u < cfg["suppress_below"] and total >= cfg["suppress_below"]:
        return WITHHELD
    return hide_small(total, policy)


def share(x: int, y: int, u: int, policy: Policy, *, suppress: bool = True, unanswered: int = 0) -> Share:
    """Everything that must sit beside a notch share.

    status: no_data (nothing yet), too_few (fewer than min_n answers: no percentage, `needed` says how
    many more), bracketed (min_n to bracket_n_below - 1) or ok.

    With suppress=True the three counts are for outward views. While there is no percentage (fewer than
    min_n answers) a cell under the threshold reads "<5" (zero too), and if one of notched/unnotched is
    hidden and not zero, the other reads "hidden", because n = notched + unnotched would give it away. Once
    a percentage is shown the counts are shown exactly: n, the share, the Wilson interval and the bounds
    already give x, y and u to anyone who divides, so a "<5" there would only look like protection.

    `unanswered` is the reports in the same place whose animal question got no usable answer. The bounds
    count them, with u, as unknown: any of them may be a street dog with or without the notch.
    """
    if min(x, y, u, unanswered) < 0:
        raise ValueError("counts cannot be negative")
    cfg = _cfg(policy)
    n = x + y
    shown: list[int | str] = [x, y, u]
    small = suppressed_label(policy)
    if suppress and n < cfg["min_n_for_share"]:
        low = cfg["suppress_below"]
        hide_x, hide_y, hide_u = x < low, y < low, u < low
        # A hidden 1-4 would be n minus the other cell, so the other one is hidden too. A hidden zero
        # gives nothing away that n does not already say.
        also_x, also_y = hide_y and y > 0, hide_x and x > 0
        shown = [
            small if hide_x else (WITHHELD if also_x else x),
            small if hide_y else (WITHHELD if also_y else y),
            small if hide_u else u,
        ]
    out = Share(n=n, notched=shown[0], unnotched=shown[1], unsure=shown[2], status="no_data")
    if x + y + u == 0:
        return out
    if n < cfg["min_n_for_share"]:
        out.status = "too_few"
        out.needed = cfg["min_n_for_share"] - n
        return out
    low_ci, high_ci = wilson(x, n, cfg["z"])
    low_b, high_b = manski(x, n, u + unanswered)
    out.status = "ok" if n >= cfg["bracket_n_below"] else "bracketed"
    out.share = round(x / n, 4)
    out.ci_low, out.ci_high = round(low_ci, 4), round(high_ci, 4)
    out.bound_low, out.bound_high = round(low_b, 4), round(high_b, 4)
    return out


def hide_across_areas(
    areas: dict[str, tuple[int, int, int]], shares: dict[str, Share], policy: Policy
) -> None:
    """Keep a hidden notch / no-notch / could-not-see count of one area from being read back as the city
    count minus the other areas. The three columns each add up to the city's; an area with fewer than
    min_n answers hides a cell under the threshold, so (hide_group) the smallest other area cell that is not
    given away by a percentage is hidden too. Changes the Share objects in place. An area whose cell is
    given away anyway (its percentage is shown) is never hidden here."""
    low = _cfg(policy)["min_n_for_share"]
    open_areas = {a for a, (x, y, _) in areas.items() if x + y >= low}
    for column, name in enumerate(("notched", "unnotched", "unsure")):
        shown, _ = hide_group({a: v[column] for a, v in areas.items()}, policy, open_cells=open_areas)
        for a, value in shown.items():
            if a not in open_areas and value == WITHHELD:
                setattr(shares[a], name, WITHHELD)
