"""Stored covers outlive neither their use nor their links' usefulness.

Cover files nothing points at any more are deleted after a full scan, and a
link kept under a regrouped album's old id — in the queue, in a collection —
is answered with the cover of the album it became.
"""

import os
import sqlite3
import time
from contextlib import closing

import pytest
import pytest_asyncio
from PIL import Image

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule

OLD, NEW = "album_000000000000000a", "album_000000000000000b"


@pytest_asyncio.fixture
async def config(tmp_path):
    (tmp_path / "music").mkdir()
    config = LocalFilesConfig(
        music_folders=[str(tmp_path / "music")],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    await init_db(config.db_path)
    with closing(sqlite3.connect(config.db_path)) as conn:
        conn.execute("INSERT INTO artists (id, name) VALUES ('ar', 'Artist')")
        conn.execute(
            "INSERT INTO albums (id, title, artist_id, image_url) "
            "VALUES (?, 'Murder Ballads', 'ar', ?)",
            (NEW, f"{NEW}.jpg"),
        )
        conn.commit()
    return config


def _cover_files(config, kind, key, age_s=0):
    folder = os.path.join(config.artwork_path, kind)
    os.makedirs(folder, exist_ok=True)
    paths = []
    for size in ("thumbnail", "small", "large"):
        path = os.path.join(folder, f"{key}_{size}.jpg")
        Image.new("RGB", (8, 8), (9, 9, 9)).save(path, "JPEG")
        then = time.time() - age_s
        os.utime(path, (then, then))
        paths.append(path)
    return paths


@pytest.mark.asyncio
async def test_covers_nothing_uses_are_deleted_and_the_rest_kept(config):
    used = _cover_files(config, "album", NEW, age_s=7200)
    retired = _cover_files(config, "album", OLD, age_s=7200)
    fresh = _cover_files(config, "track", "track_00000000000000f1")
    leftover = os.path.join(config.artwork_path, "album", f"{NEW}_small.jpg.1.2.tmp")
    open(leftover, "wb").close()
    os.utime(leftover, (time.time() - 7200,) * 2)

    await FileIndexer(config, AsyncIndexerDb(config))._sweep_artwork()

    assert all(os.path.exists(path) for path in used + fresh)
    assert not any(os.path.exists(path) for path in retired + [leftover])


@pytest.mark.asyncio
async def test_a_link_under_a_regrouped_albums_old_id_shows_its_new_cover(config):
    _cover_files(config, "album", NEW)
    with closing(sqlite3.connect(config.db_path)) as conn:
        conn.execute(
            "INSERT INTO entity_id_alias (old_id, current_id, entity_type) "
            "VALUES (?, ?, 'album')",
            (OLD, NEW),
        )
        conn.commit()
    module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))

    path = await module.get_resource_path(f"album/{OLD}_small.jpg")

    assert path == os.path.join(config.artwork_path, "album", f"{NEW}_small.jpg")
    assert await module.get_resource_path("album/album_000000000000000c_small.jpg") is None


@pytest.mark.asyncio
async def test_a_link_to_a_track_cover_since_cleared_shows_what_the_track_shows(
    config,
):
    _cover_files(config, "album", NEW)
    with closing(sqlite3.connect(config.db_path)) as conn:
        conn.execute(
            "INSERT INTO tracks (id, title, album_id, artist_id, file_path, format) "
            "VALUES ('track_00000000000000f1', 'Stagger Lee', ?, 'ar', '/1.flac', "
            "'flac')",
            (NEW,),
        )
        conn.commit()
    module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))

    path = await module.get_resource_path("track/track_00000000000000f1_large.jpg")

    assert path == os.path.join(config.artwork_path, "album", f"{NEW}_large.jpg")
