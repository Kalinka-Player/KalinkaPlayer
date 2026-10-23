#!/usr/bin/env python3
"""
Tests for Phase-1 1d planner: build_features (from track row + evidence) and
plan_folder (engine + V/A classification). Read-only — no DB writes.
"""

import json

import pytest

from kalinka_plugin_localfiles.clustering.planner import (
    build_features,
    plan_folder,
)


def _ev(album=None, albumartist=None, compilation=None, art=None,
        codec="flac", sr=44100, bd=16, vorbis=True):
    raw = {}
    if vorbis:
        if album:
            raw["album"] = [album]
        if albumartist:
            raw["albumartist"] = [albumartist]
        if compilation:
            raw["compilation"] = [compilation]
    else:
        if album:
            raw["TALB"] = album
        if albumartist:
            raw["TPE2"] = albumartist
        if compilation:
            raw["TCMP"] = compilation
    return {
        "raw_tags": json.dumps(raw),
        "stream_info": json.dumps({"codec": codec, "sample_rate": sr,
                                   "bits_per_sample": bd}),
        "art_phash": art,
        "cue_sheet": None,
        "import_batch": None,
    }


def _track(tid, artist_id="artist_a", track_number=1, disc_number=None,
           artist_name=None):
    return {"id": tid, "artist_id": artist_id, "track_number": track_number,
            "disc_number": disc_number, "artist_name": artist_name}


def test_build_features_reads_albumartist_and_disc_suffix():
    f = build_features(
        _track("t1"),
        _ev(album="Some Album (Disc 2)", albumartist="The Band", compilation="1"),
    )
    assert f.album_key == "some album"           # disc suffix stripped
    assert f.albumartist_key == "the band"
    assert f.compilation is True
    assert f.stream_key == "flac|44100|16"


def test_build_features_id3_vocabulary():
    f = build_features(
        _track("t1"), _ev(album="X", albumartist="AA", vorbis=False)
    )
    assert f.album_key == "x"
    assert f.albumartist_key == "aa"


def test_plan_untagged_folder_gets_folder_name_title():
    rows = [(_track(f"t{i}", track_number=i), _ev()) for i in range(1, 6)]
    plan = plan_folder("/music/Some Album", rows)
    assert not plan.split
    assert len(plan.clusters) == 1
    c = plan.clusters[0]
    assert c.kind == "album"
    assert c.title == "Some Album"        # folder name, not unknown_album
    assert len(c.track_ids) == 5


def test_plan_strips_artist_prefix_from_folder_title():
    # Untagged rip in "The Beatles - Abbey Road": the folder-derived album
    # title keeps only "Abbey Road" once the artist prefix is stripped.
    rows = [
        (_track(f"t{i}", artist_id="beatles", track_number=i,
                artist_name="The Beatles"), _ev())
        for i in range(1, 6)
    ]
    plan = plan_folder("/music/The Beatles - Abbey Road", rows)
    assert plan.clusters[0].title == "Abbey Road"


def test_plan_repairs_folder_derived_title():
    # The folder name is tag text too: an untagged rip in "В.Цой - Черный
    # альбом" must get the same repairs a tagged title does, or the title
    # keeps its unspaced abbreviation and no longer matches the (repaired)
    # artist name the prefix strip compares against.
    rows = [
        (_track(f"t{i}", artist_id="tsoi", track_number=i,
                artist_name="В. Цой"), _ev())
        for i in range(1, 6)
    ]
    plan = plan_folder("/music/В.Цой - Черный альбом", rows)
    assert plan.clusters[0].title == "Черный альбом"


def test_plan_keeps_eponymous_album_title():
    # An album actually named after the artist isn't stripped to empty.
    rows = [
        (_track(f"t{i}", artist_id="metallica", track_number=i,
                artist_name="Metallica"), _ev())
        for i in range(1, 6)
    ]
    plan = plan_folder("/music/Metallica", rows)
    assert plan.clusters[0].title == "Metallica"


def test_plan_coherent_album_uses_tag_title():
    rows = [
        (_track(f"t{i}", track_number=i), _ev(album="Real Title",
                                              albumartist="Band"))
        for i in range(1, 9)
    ]
    plan = plan_folder("/music/folder", rows)
    assert len(plan.clusters) == 1
    assert plan.clusters[0].title == "Real Title"
    assert plan.clusters[0].kind == "album"


def test_plan_va_folder_is_compilation():
    # 6 tracks, 6 distinct artists, clean album-named folder.
    rows = [
        (_track(f"t{i}", artist_id=f"artist_{i}", track_number=i),
         _ev(album=f"track by {i}"))
        for i in range(1, 7)
    ]
    plan = plan_folder("/music/VA - Summer Hits", rows)
    assert len(plan.clusters) == 1
    c = plan.clusters[0]
    assert c.kind == "compilation"
    assert c.title == "Summer Hits"
    assert c.anchor_artist_id == "various_artists"


def test_plan_declared_va_with_trailing_slash():
    # A trailing "/" must not make basename empty and misclassify a declared
    # V/A compilation as a generic dump (Copilot review, PR #100).
    rows = [
        (_track(f"t{i}", artist_id=f"artist_{i}", track_number=i),
         _ev(album=f"track by {i}"))
        for i in range(1, 7)
    ]
    plan = plan_folder("/music/VA - Summer Hits/", rows)
    assert len(plan.clusters) == 1
    assert plan.clusters[0].kind == "compilation"
    assert plan.clusters[0].title == "Summer Hits"


def test_plan_generic_dump_is_singles_pool():
    rows = [
        (_track(f"t{i}", artist_id=f"artist_{i}", track_number=i), _ev())
        for i in range(1, 7)
    ]
    plan = plan_folder("/music/90s Mixes", rows)
    assert len(plan.clusters) == 1
    c = plan.clusters[0]
    assert c.kind == "singles_pool"
    assert c.title == ""
    assert c.anchor_artist_id == "unknown_artist"


def test_plan_flat_va_dump_detaches_to_singles():
    # A flat playlist folder (Jamendo-style): many artists, no shared album
    # tag, each track its own cover art. Must NOT fragment into per-art albums
    # + V/A umbrellas — the whole folder detaches to unknown_album so tracks
    # surface as singles under their real artist.
    rows = [
        (_track(f"t{i}", artist_id=f"artist_{i}", track_number=i),
         _ev(art=f"phash_{i}"))               # distinct per-track art
        for i in range(1, 13)                 # 12 tracks, 12 artists, no album
    ]
    plan = plan_folder(
        "/music/Playlist - Urban - 500604904 --- Jamendo - MP3", rows
    )
    assert len(plan.clusters) == 1
    c = plan.clusters[0]
    assert c.kind == "singles_pool"
    assert c.anchor_artist_id == "unknown_artist"
    assert c.grouping_basis["reason"] == "va_dump_folder"
    assert len(c.track_ids) == 12             # every track detached, none lost


def test_plan_va_dump_keeps_fully_declared_strays_as_albums():
    # A junk pile with two fully-declared releases lost in it (album AND
    # albumartist tags): the majority detaches to unknown_album — including
    # a track with only a junk album tag — but the declared strays keep
    # their albums.
    rows = [
        (_track(f"t{i}", artist_id=f"artist_{i}", track_number=1), _ev())
        for i in range(1, 10)
    ]
    rows.append((_track("junk", artist_id="artist_junk", track_number=1),
                 _ev(album="Album")))  # album tag but no albumartist
    rows.append((_track("bee", artist_id="artist_bee", track_number=1),
                 _ev(album="Bee Moved", albumartist="Blue Monday FM",
                     art="ph_bee")))
    rows.append((_track("gold", artist_id="artist_katz", track_number=1),
                 _ev(album="Musopen Kickstarter Project",
                     albumartist="Shelley Katz")))

    plan = plan_folder("/mnt/usb/Music/Music", rows)
    pools = [c for c in plan.clusters if c.kind == "singles_pool"]
    albums = {c.title: c for c in plan.clusters if c.kind == "album"}
    assert len(pools) == 1 and len(pools[0].track_ids) == 10
    assert "junk" in pools[0].track_ids
    assert pools[0].grouping_basis["reason"] == "va_dump_folder"
    assert set(albums) == {"Bee Moved", "Musopen Kickstarter Project"}
    assert albums["Bee Moved"].track_ids == ["bee"]
    assert albums["Bee Moved"].anchor_artist_id == "artist_bee"


def test_plan_va_dump_strays_with_same_generic_tag_stay_apart():
    # Two artists' releases sharing a generic album tag must not fuse:
    # strays bucket by (album, albumartist), not album alone.
    rows = [
        (_track(f"t{i}", artist_id=f"artist_{i}", track_number=1), _ev())
        for i in range(1, 11)
    ]
    rows.append((_track("a", artist_id="artist_a2", track_number=1),
                 _ev(album="Greatest Hits", albumartist="Alpha")))
    rows.append((_track("b", artist_id="artist_b2", track_number=1),
                 _ev(album="Greatest Hits", albumartist="Beta")))

    plan = plan_folder("/mnt/usb/Music/Music", rows)
    albums = [c for c in plan.clusters if c.kind == "album"]
    assert len(albums) == 2
    assert {tuple(c.track_ids) for c in albums} == {("a",), ("b",)}


def test_plan_va_dump_of_declared_singles_still_flattens():
    # A curated pool where every track fully declares its own single release:
    # the declared subset is itself a many-artist pool, so the whole folder
    # still detaches — no per-track junk albums.
    rows = [
        (_track(f"t{i}", artist_id=f"artist_{i}", track_number=1),
         _ev(album=f"Single {i}", albumartist=f"Artist {i}", art=f"ph_{i}"))
        for i in range(1, 13)
    ]
    plan = plan_folder(
        "/music/Playlist - Urban - 500604904 --- Jamendo - MP3", rows
    )
    assert len(plan.clusters) == 1
    c = plan.clusters[0]
    assert c.kind == "singles_pool"
    assert len(c.track_ids) == 12


def test_plan_shared_album_tag_survives_many_artists():
    # A real V/A compilation with a shared album tag is NOT a dump even though
    # every track is a different artist — the shared tag protects it.
    rows = [
        (_track(f"t{i}", artist_id=f"artist_{i}", track_number=i),
         _ev(album="Now That's What I Call Music 50"))
        for i in range(1, 13)
    ]
    plan = plan_folder("/music/Now 50", rows)
    assert len(plan.clusters) == 1
    assert plan.clusters[0].kind == "compilation"


def test_multidisc_in_one_folder_strips_disc_suffix_from_title():
    # Both discs tagged "… (Disc N)" in one folder collapse to one album whose
    # title drops the disc marker.
    rows = []
    for i in range(1, 6):
        rows.append((_track(f"a{i}", track_number=i, disc_number=1),
                     _ev(album="Best Of MJ (Disk 1)", albumartist="MJ")))
    for i in range(1, 6):
        rows.append((_track(f"b{i}", track_number=i, disc_number=2),
                     _ev(album="Best Of MJ (Disk 2)", albumartist="MJ")))
    plan = plan_folder("/music/Best Of MJ (2CD)", rows)
    assert len(plan.clusters) == 1
    c = plan.clusters[0]
    assert c.title == "Best Of MJ"      # (Disk N) stripped
    assert c.kind == "multi_disc"
    assert len(c.track_ids) == 10


def test_vangelis_roman_disc_tags_form_one_album():
    # Two complete 1..8 runs with no albumartist tag used to score as
    # separate albums, despite sharing an artist, cover and parent folder.
    rows = [
        (
            _track(f"d{disc}t{number}", artist_id="vangelis",
                   artist_name="Vangelis", track_number=number, disc_number=disc),
            _ev(album=f"The Best of Vangelis CD {suffix}", art="21d0e4e4e4e0e1e0"),
        )
        for disc, suffix in ((1, "I"), (2, "II"))
        for number in range(1, 9)
    ]

    plan = plan_folder("/music/Vangelis - The Best Of - 2 CD", rows)

    assert not plan.split
    assert len(plan.clusters) == 1
    cluster = plan.clusters[0]
    assert cluster.title == "The Best of Vangelis"
    assert cluster.kind == "multi_disc"
    assert set(cluster.track_ids) == {track["id"] for track, _ in rows}
    assert cluster.disc_numbers == frozenset({1, 2})


@pytest.mark.parametrize("volume", ["2", "II"])
def test_standalone_volume_keeps_its_name(volume):
    # A single "Vol. 2" release (one title variant) must NOT be stripped.
    rows = [
        (_track(f"t{i}", track_number=i), _ev(album=f"Greatest Hits Vol. {volume}",
                                              albumartist="Band"))
        for i in range(1, 7)
    ]
    plan = plan_folder("/music/Greatest Hits Vol 2", rows)
    assert len(plan.clusters) == 1
    assert plan.clusters[0].title == f"Greatest Hits Vol. {volume}"
    assert plan.clusters[0].kind == "album"


def test_plan_empty_folder():
    plan = plan_folder("/music/empty", [])
    assert plan.clusters == [] and not plan.split


def test_plan_two_albums_split():
    a = [(_track(f"a{i}", artist_id="one", track_number=i),
          _ev(album="Alpha", albumartist="one", art="0000000000000000"))
         for i in range(1, 7)]
    b = [(_track(f"b{i}", artist_id="two", track_number=i),
          _ev(album="Beta", albumartist="two", art="ffffffffffffffff"))
         for i in range(1, 7)]
    plan = plan_folder("/music/mixed", a + b)
    assert plan.split
    assert len(plan.clusters) == 2
    titles = {c.title for c in plan.clusters}
    assert titles == {"Alpha", "Beta"}


class TestThePathTitleRung:
    """The filename model sits between the tags and the bare folder name.

    A folder name is not an album name: it carries year prefixes and edition
    suffixes the model reads out and the basename keeps. It stays below the
    tags, which are evidence rather than a reading of a directory.
    """

    def test_it_beats_the_bare_folder_name(self):
        rows = [(_track("t1"), _ev()), (_track("t2", track_number=2), _ev())]
        plan = plan_folder(
            "/music/Pink Floyd/1979 - The Wall",
            rows,
            path_album_title="The Wall",
        )
        assert [c.title for c in plan.clusters] == ["The Wall"]

    def test_the_folder_name_still_answers_when_it_reads_nothing(self):
        rows = [(_track("t1"), _ev()), (_track("t2", track_number=2), _ev())]
        plan = plan_folder("/music/Pink Floyd/The Wall", rows)
        assert [c.title for c in plan.clusters] == ["The Wall"]

    def test_the_tags_still_outrank_it(self):
        rows = [
            (_track("t1"), _ev(album="Wish You Were Here")),
            (_track("t2", track_number=2), _ev(album="Wish You Were Here")),
        ]
        plan = plan_folder(
            "/music/Pink Floyd/1979 - The Wall",
            rows,
            path_album_title="The Wall",
        )
        assert [c.title for c in plan.clusters] == ["Wish You Were Here"]

    def test_a_path_derived_title_is_marked_as_a_guess(self):
        """Which is what lets MusicBrainz correct it later, and what keeps
        the next scan from rebuilding the correction away."""
        rows = [(_track("t1"), _ev()), (_track("t2", track_number=2), _ev())]
        guessed = plan_folder("/music/Pink Floyd/The Wall", rows).clusters[0]
        tagged = plan_folder(
            "/music/Pink Floyd/The Wall",
            [(_track("t1"), _ev(album="The Wall")),
             (_track("t2", track_number=2), _ev(album="The Wall"))],
        ).clusters[0]
        assert (guessed.title_source, tagged.title_source) == (
            "folder_name", "tag_consensus",
        )

    def test_a_year_read_off_the_folder_travels_with_the_title(self):
        rows = [(_track("t1"), _ev()), (_track("t2", track_number=2), _ev())]
        plan = plan_folder(
            "/music/Pink Floyd/1979 - The Wall",
            rows,
            path_album_title="The Wall",
            path_album_year=1979,
        )
        assert [c.year for c in plan.clusters] == [1979]
