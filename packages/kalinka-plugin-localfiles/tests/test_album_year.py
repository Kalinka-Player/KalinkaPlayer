"""The album's release year reaches the plugin's public metadata."""

import sqlite3

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import (
    LocalFilesInputModule,
    album_id,
    track_id,
)


async def _scan(tmp_path, tags):
    """A one-track library; returns its config, track id and album id."""
    music = tmp_path / "music"
    path = music / "Comfortably Numb.flac"
    music.mkdir()
    sf.write(str(path), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    audio = FLAC(str(path))
    audio.update(
        {
            "title": "Comfortably Numb",
            "artist": "Pink Floyd",
            "album": "The Wall",
            **tags,
        }
    )
    audio.save()
    config = LocalFilesConfig(
        music_folders=[str(music)],
        db_path=str(tmp_path / "library.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    await init_db(config.db_path)
    changes = await FileIndexer(config, AsyncIndexerDb(config)).process_file(
        str(path), force=True
    )
    return config, changes["tracks"], changes["albums"]


async def _years_shown(config, tid, aid):
    """The year on the album item, then on each way its track is served."""
    module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))
    album_item = await module.get(album_id(aid))
    track_item = await module.get(track_id(tid))
    info = (await module.get_track_info([tid]))[0]
    listing = await module.browse(album_id(aid))
    tracks = (track_item.track, info.metadata, listing.items[0].track)
    return [album_item.album.model_dump(mode="json")["year"]] + [
        track.model_dump(mode="json")["album"]["year"] for track in tracks
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tags,expected",
    [({"date": "1979-11-30"}, 1979), ({"date": "0000-00-00"}, None), ({}, None)],
)
async def test_album_year_is_on_albums_and_their_tracks(tmp_path, tags, expected):
    config, tid, aid = await _scan(tmp_path, tags)

    with sqlite3.connect(config.db_path) as conn:
        stored = conn.execute("SELECT year FROM albums WHERE id = ?", (aid,)).fetchone()
    assert stored == (expected,)
    assert await _years_shown(config, tid, aid) == [expected] * 4


@pytest.mark.asyncio
async def test_a_placeholder_year_already_stored_is_not_shown(tmp_path):
    config, tid, aid = await _scan(tmp_path, {"date": "1979"})
    with sqlite3.connect(config.db_path) as conn:
        conn.execute("UPDATE albums SET year = 0 WHERE id = ?", (aid,))

    assert await _years_shown(config, tid, aid) == [None] * 4
