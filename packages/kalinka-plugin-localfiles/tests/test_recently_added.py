"""What arrived last: an album once, dated by its first file, and each track
that belongs to no album on its own."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from kalinka_plugin_sdk.datamodel import EntityId, EntityType

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule

TRACKS = [
    ("t_a1", "al_a", 100),
    ("t_a2", "al_a", 300),
    ("t_b1", "al_b", 200),
    ("t_single", "unknown_album", 250),
    ("t_old_single", "unknown_album", 50),
]


def _seed(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO artists (id, name) VALUES ('ar', 'Artist')")
        conn.executemany(
            "INSERT INTO albums (id, title, artist_id, last_updated)"
            " VALUES (?, ?, 'ar', ?)",
            # Touched by the enricher long after they arrived.
            [("al_a", "A", 9000), ("al_b", "B", 10)],
        )
        conn.executemany(
            "INSERT INTO tracks (id, title, album_id, artist_id, file_path, format,"
            " duration) VALUES (?, ?, ?, 'ar', ?, 'flac', 60)",
            [(id, id, album, f"/music/{id}.flac") for id, album, _ in TRACKS],
        )
        conn.executemany(
            "INSERT INTO library_file (file_id, current_path, first_indexed)"
            " VALUES (?, ?, ?)",
            [(id, f"/music/{id}.flac", added) for id, _, added in TRACKS],
        )


@pytest.fixture
def module(tmp_path):
    config = LocalFilesConfig(
        db_path=str(tmp_path / "test.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    asyncio.run(init_db(config.db_path))
    _seed(config.db_path)
    db = LocalFilesInputModuleDb(config)
    return LocalFilesInputModule(config, db, storage_source=_NoRoots)


class _NoRoots:
    def canonical_roots(self, locations):
        return []


def _recent(module, offset=0, limit=50):
    recent = EntityId(id="recent", type=EntityType.CATALOG, source="localfiles")
    return asyncio.run(module.browse(recent, offset=offset, limit=limit))


def test_newest_arrival_first_each_album_once(module):
    listing = _recent(module)

    assert [item.id.id for item in listing.items] == [
        "t_single",
        "al_b",
        "al_a",
        "t_old_single",
    ]
    assert listing.items[1].album is not None
    assert listing.items[0].track is not None
    assert listing.total == 4


def test_an_album_is_dated_by_its_first_file_not_by_its_last_edit(module):
    names = [item.id.id for item in _recent(module).items]

    assert names.index("al_b") < names.index("al_a")


def test_pages_hold_their_place(module):
    assert [item.id.id for item in _recent(module, offset=1, limit=2).items] == [
        "al_b",
        "al_a",
    ]
