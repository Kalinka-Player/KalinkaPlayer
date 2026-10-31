"""The album cover decision, from what the index knows about its tracks."""

from kalinka_plugin_localfiles.indexer.artwork_choice import (
    Member,
    album_folders,
    embedded_choices,
)

A = "0f0f0f0f0f0f0f0f"
A_REENCODED = "0f0f0f0f0f0f0f0e"
B = "f0f0f0f0f0f0f0f0"


def _member(n, phash, art_type=3, disc=None):
    return Member(f"t{n}", f"/m/Album/{n:02}.flac", disc, n, phash, art_type)


def test_one_cover_re_encoded_per_track_still_counts_once():
    choices = embedded_choices(
        [_member(1, B), _member(2, B), _member(3, A), _member(4, A_REENCODED),
         _member(5, A)]
    )
    assert [c.phash for c in choices] == [A, B]
    assert len(choices[0].members) == 3


def test_a_tie_goes_to_the_earliest_track():
    choices = embedded_choices([_member(2, A), _member(1, B, disc=1)])
    assert choices[0].phash == B


def test_a_front_cover_outranks_more_untyped_pictures():
    choices = embedded_choices(
        [_member(1, A, art_type=0), _member(2, A, art_type=0), _member(3, B)]
    )
    assert choices[0].phash == B


def test_a_folder_shared_by_two_albums_is_neither_ones():
    owned = album_folders(
        [("a", "/m/Mixed/A.flac"), ("b", "/m/Mixed/B.flac"),
         ("c", "/m/Album/CD1/01.flac"), ("c", "/m/Album/CD2/01.flac")],
        ["/m"],
    )
    assert owned["a"].roots == [] and owned["b"].roots == []
    assert owned["c"].folders == ["/m/Album"]
    assert owned["c"].discs == ["/m/Album/CD1", "/m/Album/CD2"]
