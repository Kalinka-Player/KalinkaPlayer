"""The album's release year reaches the plugin's public metadata."""

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tags,expected",
    [({"date": "1979-11-30"}, 1979), ({}, None)],
)
async def test_album_year_is_on_albums_and_their_tracks(tmp_path, tags, expected):
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
    tid, aid = changes["tracks"], changes["albums"]

    module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))
    album_item = await module.get(album_id(aid))
    track_item = await module.get(track_id(tid))
    info = (await module.get_track_info([tid]))[0]
    listing = await module.browse(album_id(aid))

    assert album_item.album.model_dump(mode="json")["year"] == expected
    for track in (track_item.track, info.metadata, listing.items[0].track):
        assert track.model_dump(mode="json")["album"]["year"] == expected
