"""Look here: the Bonferroni-corrected interval against the rest of the city, and nothing else; at most three."""

from __future__ import annotations

from chaarteeke.stats.coverage import coverage
from chaarteeke.stats.intervals import wilson
from chaarteeke.stats.lookhere import Counts, difference_low, evaluate


def C(notched, unnotched):
    return Counts(notched, unnotched)


REST = C(160, 40)  # the rest of the city: 80%


def city_with(clusters, rest=REST):
    """The city counts the way coverage builds them: the rest of the city plus every cluster in it."""
    city = rest
    for here in clusters.values():
        city = C(city.notched + here.notched, city.unnotched + here.unnotched)
    return city


def flags_for(ctx, clusters, rest=REST):
    return evaluate(ctx.policy, city_with(clusters, rest), clusters)


def test_a_cluster_far_below_the_rest_of_the_city_is_flagged_by_the_corrected_interval(ctx):
    flags = flags_for(ctx, {"c1": C(60, 140)})
    assert [f.cluster_id for f in flags] == ["c1"]
    assert "more than chance explains" in flags[0].reason and "rest of the city" in flags[0].reason
    # the same reason as numbers, with the answers the flag used: this area and the rest of the city
    assert flags[0].data == {
        "kind": "strong",
        "n": 200,
        "now_notched": 60,
        "now_unnotched": 140,
        "rest_now_notched": 160,
        "rest_now_unnotched": 40,
    }


def test_the_cluster_is_compared_with_the_city_without_itself(ctx):
    here, rest = C(30, 20), C(160, 40)  # 60% of 50 against 80% of 200
    city = C(here.notched + rest.notched, here.unnotched + rest.unnotched)  # 76%, with the cluster in it
    # the old comparison, the cluster's corrected upper end against the city that holds it, does not flag
    assert wilson(here.notched, here.n, ctx.policy["stats"]["lookhere_z"])[1] > city.p
    assert [f.cluster_id for f in evaluate(ctx.policy, city, {"c1": here})] == ["c1"]


def test_the_rests_own_uncertainty_counts(ctx):
    here = C(30, 20)  # 60% of 50
    sure_rest = C(160, 40)  # 80% of 200
    unsure_rest = C(16, 4)  # 80% of 20, below min_n: no comparison at all
    assert [f.cluster_id for f in flags_for(ctx, {"c1": here}, sure_rest)] == ["c1"]
    assert flags_for(ctx, {"c1": here}, unsure_rest) == []
    # 30 of 50 against 24 of 30: the difference interval is wide because the rest is small
    small_rest = C(24, 6)
    assert difference_low(small_rest, here, ctx.policy["stats"]["lookhere_z"]) < 0
    assert difference_low(sure_rest, here, ctx.policy["stats"]["lookhere_z"]) > 0


def test_an_uncorrected_95_percent_gap_is_not_enough(ctx):
    # 33 of 50 is 66%: against the rest at 80% its uncorrected 95% difference interval clears zero,
    # but z = 2.73 widens it past zero
    z = ctx.policy["stats"]["lookhere_z"]
    assert difference_low(C(160, 40), C(33, 17), 1.96) > 0 > difference_low(C(160, 40), C(33, 17), z)
    assert flags_for(ctx, {"c1": C(33, 17)}) == []


def test_a_low_share_that_chance_explains_is_not_flagged_however_long_it_lasts(ctx):
    """The rule 'below the rest in two 4-week windows' is gone: being below once or twice is not a finding."""
    assert (
        flags_for(ctx, {"c2": C(33, 17)}) == []
    )  # below the rest (66% against 80%), within chance at z 2.73
    assert not any(f.data["kind"] != "strong" for f in flags_for(ctx, {"c2": C(33, 17), "c1": C(60, 140)}))


def test_too_few_answers_never_flag(ctx):
    assert flags_for(ctx, {"c1": C(2, 22)}) == []  # 24 answers, under the minimum of 25


def test_at_most_three_and_the_strongest_win(ctx):
    clusters = {f"c{i}": C(50 - 4 * i, 50 + 4 * i) for i in range(1, 8)}  # all seven are below a rest of 80%
    flags = flags_for(ctx, clusters, C(800, 200))
    assert len(flags) == 3
    assert [f.cluster_id for f in flags] == sorted(f.cluster_id for f in flags)  # listed by id, no rank
    assert {f.cluster_id for f in flags} == {"c5", "c6", "c7"}  # the lowest shares


def test_reasons_never_print_a_share(ctx):
    flags = flags_for(ctx, {"c1": C(60, 140)})
    assert flags and all("%" not in f.reason and "rank" not in f.reason.lower() for f in flags)


def test_coverage_flags_from_reports_in_the_database(sctx, add_reports):
    wards = {c.id: c.ward_nos[0] for c in sctx.clusters.all()}
    for cid, ward in wards.items():
        low = cid == "c3"
        for weeks_ago in range(1, 9):  # eight full weeks before the current one
            add_reports(ward, "notched", n=3 if low else 8, weeks_ago=weeks_ago)
            add_reports(ward, "unnotched", n=7 if low else 2, weeks_ago=weeks_ago)
    cov = coverage(sctx)
    assert cov.look_here == ["c3"]
    flagged = next(c for c in cov.clusters if c.id == "c3")
    assert flagged.look_here and flagged.look_here_reason
    assert not any(c.look_here for c in cov.clusters if c.id != "c3")


def test_only_the_latest_four_full_weeks_decide(sctx, add_reports):
    """A cluster that was low earlier and has recovered is not flagged: the weeks before the window do not count."""
    wards = {c.id: c.ward_nos[0] for c in sctx.clusters.all()}
    for cid, ward in wards.items():
        for weeks_ago in range(1, 9):
            low = cid == "c3" and weeks_ago > 4  # low only in the four weeks before the latest four
            add_reports(ward, "notched", n=3 if low else 8, weeks_ago=weeks_ago)
            add_reports(ward, "unnotched", n=7 if low else 2, weeks_ago=weeks_ago)
    assert coverage(sctx).look_here == []


def test_the_reason_data_matches_the_english_sentence(sctx, add_reports):
    wards = {c.id: c.ward_nos[0] for c in sctx.clusters.all()}
    for cid, ward in wards.items():
        low = cid == "c3"
        for weeks_ago in range(1, 9):
            add_reports(ward, "notched", n=3 if low else 8, weeks_ago=weeks_ago)
            add_reports(ward, "unnotched", n=7 if low else 2, weeks_ago=weeks_ago)
    cov = coverage(sctx)
    flagged = next(c for c in cov.clusters if c.id == "c3")
    data = flagged.look_here_reason_data
    assert data is not None and data["kind"] == "strong"
    assert f"({data['n']} answers here)" in flagged.look_here_reason
    # the card can show the answers the flag used, not only the 12-week share
    assert data["now_notched"] + data["now_unnotched"] == data["n"]
    for c in cov.clusters:
        if not c.look_here:
            assert c.look_here_reason is None and c.look_here_reason_data is None
