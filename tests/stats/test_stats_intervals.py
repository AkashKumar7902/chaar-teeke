"""Wilson against DOMAIN section G's table, Manski, the Share shape, suppression, MIN_N and brackets."""

from __future__ import annotations

import pytest

from chaarteeke.stats.intervals import WITHHELD, hide_cells, hide_group, hide_small, manski, share, wilson

SUPPRESSED = "<5"

# (n, p, half-width in points) from DOMAIN G. Its n = 20 row for "p = 0.78" is 16.8, which is what the
# formula gives for 16 of 20 (p = 0.80); 15.6 of 20 gives 17.2. The test uses 16 of 20 for that row.
HALF_WIDTHS = [
    (20, 0.5, 20.1),
    (50, 0.5, 13.4),
    (100, 0.5, 9.6),
    (150, 0.5, 7.9),
    (250, 0.5, 6.2),
    (1000, 0.5, 3.1),
    (20, 0.8, 16.8),
    (50, 0.78, 11.2),
    (100, 0.78, 8.0),
    (150, 0.78, 6.6),
    (250, 0.78, 5.1),
    (1000, 0.78, 2.6),
]


@pytest.mark.parametrize(("n", "p", "expected"), HALF_WIDTHS)
def test_wilson_half_width_matches_domain_table(n, p, expected):
    low, high = wilson(round(p * n), n, 1.96)
    assert round((high - low) / 2 * 100, 1) == expected


def test_wilson_stays_inside_zero_and_one():
    assert wilson(0, 10, 1.96)[0] == 0.0
    assert wilson(10, 10, 1.96)[1] == 1.0


def test_wilson_refuses_no_answers():
    with pytest.raises(ValueError):
        wilson(0, 0, 1.96)
    with pytest.raises(ValueError):
        wilson(5, 4, 1.96)


def test_manski_bounds():
    assert manski(30, 40, 10) == (0.6, 0.8)
    assert manski(30, 40, 0) == (0.75, 0.75)
    with pytest.raises(ValueError):
        manski(0, 0, 0)


def test_no_data(ctx):
    s = share(0, 0, 0, ctx.policy)
    assert s.status == "no_data"
    assert s.share is None and s.needed is None


def test_too_few_has_no_percentage_and_says_how_many_more(ctx):
    s = share(8, 3, 2, ctx.policy)
    assert s.status == "too_few"
    assert (s.n, s.needed) == (11, 14)  # 25 needed
    assert s.share is None and s.ci_low is None and s.bound_low is None


def test_min_n_and_bracket_edges(ctx):
    assert share(12, 12, 0, ctx.policy).status == "too_few"  # 24
    assert share(13, 12, 0, ctx.policy).status == "bracketed"  # 25
    assert share(25, 24, 0, ctx.policy).status == "bracketed"  # 49
    assert share(25, 25, 0, ctx.policy).status == "ok"  # 50


def test_share_carries_interval_and_could_not_see_bounds(ctx):
    s = share(60, 20, 10, ctx.policy)
    assert s.status == "ok" and s.n == 80
    assert s.share == 0.75
    assert s.ci_low < 0.75 < s.ci_high
    assert s.bound_low == round(60 / 90, 4) and s.bound_high == round(70 / 90, 4)
    assert (s.notched, s.unnotched, s.unsure) == (60, 20, 10)


def test_small_cell_reads_less_than_five_and_hides_its_complement_while_no_percentage_is_shown(ctx):
    s = share(3, 20, 10, ctx.policy)  # n = 23: no percentage; showing 20 would give away 3
    assert s.status == "too_few"
    assert s.notched == SUPPRESSED
    assert s.unnotched == WITHHELD
    assert s.unsure == 10
    s = share(20, 3, 10, ctx.policy)
    assert (s.notched, s.unnotched) == (WITHHELD, SUPPRESSED)


def test_two_small_cells_both_read_less_than_five(ctx):
    s = share(3, 2, 1, ctx.policy)
    assert (s.notched, s.unnotched, s.unsure) == (SUPPRESSED, SUPPRESSED, SUPPRESSED)


def test_small_could_not_see_is_hidden_alone_while_no_percentage_is_shown(ctx):
    s = share(12, 10, 2, ctx.policy)
    assert (s.notched, s.unnotched, s.unsure) == (12, 10, SUPPRESSED)


def test_a_zero_cell_reads_less_than_five_but_hides_nothing_else(ctx):
    s = share(20, 0, 8, ctx.policy)
    assert (s.notched, s.unnotched, s.unsure) == (20, SUPPRESSED, 8)


def test_beside_a_percentage_the_counts_are_shown_exactly(ctx):
    """n, the share, the interval and the bounds give x, y and u back, so a '<5' there would be for show."""
    s = share(3, 40, 2, ctx.policy)
    assert s.status == "bracketed" and (s.notched, s.unnotched, s.unsure) == (3, 40, 2)
    assert share(30, 30, 2, ctx.policy).unsure == 2


def test_the_bounds_count_the_unanswered_as_unknown_too(ctx):
    s = share(60, 20, 10, ctx.policy, unanswered=10)
    assert s.bound_low == round(60 / 100, 4) and s.bound_high == round(80 / 100, 4)
    assert (s.unsure, s.share) == (10, 0.75)  # the counts and the share do not change


def test_zero_is_a_small_cell_too(ctx):
    assert hide_small(0, ctx.policy) == SUPPRESSED
    assert hide_small(4, ctx.policy) == SUPPRESSED
    assert hide_small(5, ctx.policy) == 5


def test_clinic_view_keeps_the_raw_counts(ctx):
    s = share(3, 10, 2, ctx.policy, suppress=False)
    assert (s.notched, s.unnotched, s.unsure) == (3, 10, 2)


def test_negative_counts_refused(ctx):
    with pytest.raises(ValueError):
        share(-1, 5, 0, ctx.policy)


# ----------------------------------------------------------------- cells that add up to a total
def test_hide_cells_keeps_a_single_hidden_cell_from_being_worked_out(ctx):
    shown = hide_cells({"a": 40, "b": 12, "c": 3}, ctx.policy)
    assert shown == {"a": 40, "b": "hidden", "c": "<5"}  # 55 - 40 would give c, so the next smallest goes too


def test_hide_cells_lets_zero_and_several_small_cells_be(ctx):
    assert hide_cells({"a": 40, "b": 12, "c": 0}, ctx.policy) == {"a": 40, "b": 12, "c": "<5"}
    assert hide_cells({"a": 40, "b": 2, "c": 3}, ctx.policy) == {"a": 40, "b": "<5", "c": "<5"}
    assert hide_cells({"a": 1, "b": 2, "c": 0}, ctx.policy) == {"a": "<5", "b": "<5", "c": "<5"}


def test_hide_cells_hides_the_next_cell_when_the_hidden_ones_add_up_to_less_than_five(ctx):
    # two hidden cells (1 and 2) leave a residual of 3: that is itself a small count
    assert hide_cells({"a": 40, "b": 1, "c": 2}, ctx.policy) == {"a": "hidden", "b": "<5", "c": "<5"}


def test_hide_cells_hides_the_next_cell_when_every_hidden_cell_is_at_its_largest(ctx):
    # two hidden cells summing to 8 can only be 4 and 4
    assert hide_cells({"a": 40, "b": 4, "c": 4}, ctx.policy) == {"a": "hidden", "b": "<5", "c": "<5"}


def test_hide_cells_with_no_other_cell_to_hide_withholds_the_total(ctx):
    cells, total = hide_group({"a": 4, "b": 4}, ctx.policy)
    assert cells == {"a": "<5", "b": "<5"} and total == WITHHELD
    cells, total = hide_group({"a": 2, "b": 3}, ctx.policy)  # 5 can be 1+4, 2+3, 3+2, 4+1
    assert cells == {"a": "<5", "b": "<5"} and total == 5


def test_hide_group_never_hides_a_cell_that_is_given_away_anyway(ctx):
    cells, total = hide_group({"a": 40, "b": 3, "c": 12}, ctx.policy, open_cells={"c"})
    assert cells == {"a": "hidden", "b": "<5", "c": 12} and total == 55


def test_hide_cells_shows_everything_when_nothing_is_small(ctx):
    assert hide_cells({"a": 40, "b": 12}, ctx.policy) == {"a": 40, "b": 12}
