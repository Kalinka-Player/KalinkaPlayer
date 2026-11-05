"""Tests for ``LocalFilesInputModuleDb.get_artist_orphan_tracks``.

Regression target: V/A coalescing anchors compilation albums to
``various_artists``, but the per-track artist tag still points at the
real artist (Wordsmith, Sam Garbett, …). The artist's "albums" view
correctly returns empty (their album is V/A), so the UI relies on
``get_artist_orphan_tracks`` to surface their actual track. The
predicate used to be ``album_id == 'unknown_album'`` — too narrow.
This test asserts the broader ``album.artist_id != track.artist_id``
predicate catches both cases.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb


def _config(tmp_path) -> LocalFilesConfig:
    return LocalFilesConfig(
        db_path=str(tmp_path / "test.db"),
        artwork_path=str(tmp_path / "artwork"),
    )


def _seed(db_path: str) -> None:
    """Build a small library covering the three cases the browse view
    has to reconcile:

      - Solo artist ``Wordsmith`` with one track on a V/A album
      - Solo artist ``Pink Floyd`` with one album (The Wall) and tracks
        in that album (the "normal" case — these must NOT show up as
        orphans, only in the albums view)
      - Solo artist ``Sam Garbett`` with one track in ``unknown_album``
    """
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO artists (id, name) VALUES (?, ?)",
            [
                ("ar_wordsmith", "Wordsmith"),
                ("ar_pinkfloyd", "Pink Floyd"),
                ("ar_samgarbett", "Sam Garbett"),
            ],
        )
        conn.executemany(
            "INSERT INTO albums (id, title, artist_id) VALUES (?, ?, ?)",
            [
                ("al_va", "Playlist - Urban", "various_artists"),
                ("al_wall", "The Wall", "ar_pinkfloyd"),
            ],
        )
        conn.executemany(
            "INSERT INTO tracks (id, title, album_id, artist_id, file_path, format) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                # Wordsmith → V/A album → ORPHAN under the new predicate
                ("t_wordsmith", "On the Come Up", "al_va", "ar_wordsmith", "/f1", "mp3"),
                # Pink Floyd → their own album → NOT orphan
                ("t_pf_1", "A1 In the Flesh", "al_wall", "ar_pinkfloyd", "/f2", "flac"),
                ("t_pf_2", "A2 The Thin Ice", "al_wall", "ar_pinkfloyd", "/f3", "flac"),
                # Sam Garbett → unknown_album → ORPHAN under both predicates
                ("t_sg", "The Heat", "unknown_album", "ar_samgarbett", "/f4", "mp3"),
                # The umbrella includes tracks with and without a known artist.
                ("t_lost", "Mystery Song", "unknown_album", "unknown_artist", "/f5", "mp3"),
            ],
        )
        conn.commit()


@pytest.fixture
def db(tmp_path):
    cfg = _config(tmp_path)
    asyncio.run(init_db(cfg.db_path))
    _seed(cfg.db_path)
    return LocalFilesInputModuleDb(cfg)


def test_va_anchored_track_surfaces_under_real_artist(db):
    """Wordsmith's only local track lives on a V/A album. The
    albums-by-artist query returns 0 (V/A is anchored to
    ``various_artists``, not Wordsmith); the orphan-tracks query must
    surface the track or the artist page is empty."""
    tracks, total = db.get_artist_orphan_tracks("ar_wordsmith")
    assert total == 1
    assert len(tracks) == 1
    assert tracks[0]["id"] == "t_wordsmith"
    assert tracks[0]["title"] == "On the Come Up"
    # The album title is exposed so the UI can show "from <album>".
    assert tracks[0]["album_title"] == "Playlist - Urban"


def test_unknown_album_track_still_surfaces(db):
    """The original use case — a track in ``unknown_album`` — must
    still appear here, because that album is anchored to
    ``unknown_artist`` which is by definition not the track's real
    artist."""
    tracks, total = db.get_artist_orphan_tracks("ar_samgarbett")
    assert total == 1
    assert tracks[0]["title"] == "The Heat"


def test_album_owner_artist_has_no_orphans(db):
    """Pink Floyd's tracks live on an album anchored to Pink Floyd, so
    they appear in the albums view — they must NOT also appear as
    orphan tracks."""
    tracks, total = db.get_artist_orphan_tracks("ar_pinkfloyd")
    assert total == 0
    assert tracks == []


def test_pagination_respects_total(db):
    """``total`` must reflect the full orphan count, independent of
    the slice returned. (Used by ``_browse_artist`` to size the
    pagination window across albums + orphan tracks.)"""
    _, total = db.get_artist_orphan_tracks("ar_wordsmith", offset=0, limit=0)
    assert total == 1
    rows, _ = db.get_artist_orphan_tracks("ar_wordsmith", offset=10, limit=10)
    assert rows == []


def test_unknown_album_sentinel_lists_every_loose_track(db):
    """The Albums umbrella includes tracks also browsable through their artist."""
    tracks, total = db.get_album_tracks("unknown_album")
    assert total == 2
    assert [t["id"] for t in tracks] == ["t_lost", "t_sg"]


def test_regular_album_listing_unfiltered(db):
    """Ordinary albums still contain all their tracks."""
    tracks, total = db.get_album_tracks("al_wall")
    assert total == 2
    assert {t["id"] for t in tracks} == {"t_pf_1", "t_pf_2"}
