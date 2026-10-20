"""The place index: wards, ambiguity, overlap slivers, guessed wards and learned names."""

from __future__ import annotations

import pytest
from places_support import SLIVER_GEOJSON, entry

from chaarteeke.config import DATA_DIR
from chaarteeke.places.gazetteer import Gazetteer


def mini(entries, db=None, geo=SLIVER_GEOJSON):
    return Gazetteer({"meta": {}, "entries": entries}, geo, db)


def forms(g, text):
    from chaarteeke.places.normalise import normalise

    key = normalise(text).latin
    return [f for f in g.index().forms if f.latin == key]


def test_the_real_gazetteer_knows_all_75_wards(gaz):
    assert [w.ward_no for w in gaz.wards()] == list(range(1, 76))
    assert gaz.ward_name(27) == "Bakshi Nagar"
    assert gaz.has_ward(75) and not gaz.has_ward(76)


def test_an_unknown_ward_raises_not_a_blank_name(gaz):
    with pytest.raises(KeyError, match="no ward 99"):
        gaz.ward_name(99)


def test_a_name_shared_by_two_wards_is_one_ambiguous_form(gaz):
    (f,) = forms(gaz, "Gandhi Nagar")
    assert f.ward_nos >= {20, 21}
    assert f.ambiguous


def test_a_one_ward_name_is_not_ambiguous(gaz):
    (f,) = forms(gaz, "Bakshi Nagar")
    assert f.ward_nos == {27}
    assert not f.ambiguous


def test_qualifiers_are_dropped_so_people_can_say_just_the_locality(gaz):
    (f,) = forms(gaz, "Muthi")
    assert f.ward_nos == {66, 67}  # Upper Muthi, Lower Muthi


def test_a_road_that_crosses_wards_is_ambiguous():
    g = mini([entry("Long Road", [1, 3], "road", ambiguous=True)])
    (f,) = forms(g, "Long Road")
    assert f.ward_nos == {1, 3} and f.ambiguous


def test_a_place_in_an_overlap_sliver_belongs_to_both_wards():
    g = mini([entry("Sliver Temple", [1], lat=0.5, lon=1.5), entry("Inner Temple", [1], lat=0.5, lon=0.5)])
    assert forms(g, "Sliver Temple")[0].ward_nos == {1, 2}
    assert forms(g, "Sliver Temple")[0].ambiguous
    assert forms(g, "Inner Temple")[0].ward_nos == {1}


def test_wards_at_reports_every_polygon_that_holds_the_point():
    g = mini([])
    assert g.wards_at(1.5, 0.5) == (1, 2)
    assert g.wards_at(0.5, 0.5) == (1,)
    assert g.wards_at(4.0, 0.5) == ()


def test_a_ward_guessed_from_a_pincode_centre_is_never_sure():
    g = mini([entry("Pincode Office", [3], "post_office", ward_confidence="low (point only)")])
    (f,) = forms(g, "Pincode Office")
    assert f.ambiguous  # offered, but never placed by itself


def test_names_with_no_distinctive_word_are_not_indexed():
    g = mini([entry("Bus Stand", [1]), entry("Real Place", [1])])
    assert forms(g, "Bus Stand") == []
    assert forms(g, "Real Place")


def test_devanagari_names_are_indexed_by_their_spelling():
    g = mini([entry("Bakshi Nagar", [1], name_hi="बख्शी नगर")])
    from chaarteeke.places.normalise import normalise

    (i,) = g.index().deva[normalise("बख्शी नगर").devanagari_joined]
    assert g.index().forms[i].ward_nos == {1}


def test_learned_names_join_the_index_without_a_restart(ctx):
    g = mini([entry("Real Place", [1])], ctx.db)
    assert forms(g, "Sabzi Mandi Chowk") == []
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO aliases(text_norm, ward_no, added_by, added_at) VALUES ('sabzi mandi chowk', 3, 'staff:rev', 'x')"
        )
    (f,) = forms(g, "Sabzi Mandi Chowk")
    assert f.ward_nos == {3}
    assert g.aliases()[0].ward_no == 3


def test_a_learned_name_for_a_ward_that_does_not_exist_is_ignored(ctx):
    g = mini([entry("Real Place", [1])], ctx.db)
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO aliases(text_norm, ward_no, added_by, added_at) VALUES ('ghost town', 99, 'x', 'x')"
        )
    assert forms(g, "Ghost Town") == []


def test_load_reads_the_repository_data():
    g = Gazetteer.load(DATA_DIR)
    assert g.meta["total"] == len(g.entries()) == 784


def test_ward_number_aliases_that_name_another_ward_are_left_out_of_the_index(gaz):
    """The data file gives ward 55 the aliases "Ward 5", "Ward No 5" ...; "ward 5" must mean ward 5 only."""
    (f,) = forms(gaz, "Ward 5")
    assert f.ward_nos == {5}
    (g,) = forms(gaz, "Ward 55")
    assert g.ward_nos == {55}
    (h,) = forms(gaz, "ward 1")
    assert h.ward_nos == {1}
