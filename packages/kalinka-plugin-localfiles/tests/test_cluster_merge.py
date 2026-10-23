#!/usr/bin/env python3
"""Tests for Phase-1 1e: cross-folder disc-sibling merge."""

import pytest

from kalinka_plugin_localfiles.clustering.merge import merge_disc_siblings
from kalinka_plugin_localfiles.clustering.planner import ClusterPlan


def _album(folder, title, ids, disc, aa="the band"):
    return ClusterPlan(
        track_ids=list(ids), kind="album", title=title,
        anchor_artist_id="artist_a", folder=folder,
        disc_numbers=frozenset({disc}) if disc else frozenset(),
        albumartist_key=aa,
    )


@pytest.mark.parametrize("suffixes", [("CD1", "CD2", "CD3"), ("CD I", "CD II", "CD III")])
def test_disc_siblings_merge(suffixes):
    first, second, third = suffixes
    clusters = [
        _album(f"/m/Friends {first}", f"Friends {first}", ["a1", "a2"], 1),
        _album(f"/m/Friends {second}", f"Friends {second}", ["b1", "b2"], 2),
        _album(f"/m/Friends {third}", f"Friends {third}", ["c1", "c2"], 3),
    ]
    out = merge_disc_siblings(clusters)
    assert len(out) == 1
    m = out[0]
    assert m.kind == "multi_disc"
    assert m.title == "Friends"
    assert sorted(m.track_ids) == ["a1", "a2", "b1", "b2", "c1", "c2"]
    assert m.disc_numbers == frozenset({1, 2, 3})


def test_different_artists_do_not_merge():
    clusters = [
        _album("/m/Hits CD1", "Hits CD1", ["a1"], 1, aa="artist one"),
        _album("/m/Hits CD2", "Hits CD2", ["b1"], 2, aa="artist two"),
    ]
    out = merge_disc_siblings(clusters)
    assert len(out) == 2  # incompatible album-artists -> kept apart


def test_colliding_discs_do_not_merge():
    clusters = [
        _album("/m/X CD1", "X CD1", ["a1"], 1),
        _album("/m/X Disc 1", "X Disc 1", ["b1"], 1),  # both claim disc 1
    ]
    out = merge_disc_siblings(clusters)
    assert len(out) == 2


def test_unrelated_albums_pass_through():
    clusters = [
        _album("/m/Alpha", "Alpha", ["a1"], None),
        _album("/m/Beta", "Beta", ["b1"], None),
    ]
    out = merge_disc_siblings(clusters)
    assert len(out) == 2
    assert {c.title for c in out} == {"Alpha", "Beta"}


def test_compilations_not_merged():
    comp1 = ClusterPlan(["a1"], "compilation", "Comp CD1", "various_artists",
                        folder="/m/Comp CD1")
    comp2 = ClusterPlan(["b1"], "compilation", "Comp CD2", "various_artists",
                        folder="/m/Comp CD2")
    out = merge_disc_siblings([comp1, comp2])
    assert len(out) == 2  # only plain albums merge
