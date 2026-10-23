"""Track/disc numbers reach the database and the plugin's public metadata."""

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
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule, track_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative,tags,expected",
    [
        (
            "Stagger Lee.flac",
            {"tracknumber": "7/12", "discnumber": "2/3"},
            (7, 2),
        ),
        ("Stagger Lee.flac", {"tracknumber": "7"}, (7, None)),
        ("Stagger Lee.flac", {"discnumber": "2"}, (None, 2)),
        ("Stagger Lee.flac", {}, (None, None)),
        (
            "Pink Floyd/1979 - The Wall/CD2/05 - Comfortably Numb.flac",
            {},
            (5, 2),
        ),
    ],
)
async def test_positions_are_persisted_and_returned_after_rescanning(
    tmp_path, relative, tags, expected
):
    music = tmp_path / "music"
    path = music / relative
    path.parent.mkdir(parents=True)
    sf.write(str(path), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    config = LocalFilesConfig(
        music_folders=[str(music)],
        db_path=str(tmp_path / "library.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    await init_db(config.db_path)
    indexer = FileIndexer(config, AsyncIndexerDb(config))
    tid = None

    # Initial scan, changed tags, then removal: no stale positions survive a
    # rescan. When tags are absent, a numbered path supplies the positions.
    for position_tags, positions in (
        (tags, expected),
        ({"tracknumber": "9/14", "discnumber": "3/4"}, (9, 3)),
        (tags, expected),
    ):
        audio = FLAC(str(path))
        audio.clear()
        audio.update({"title": "Stagger Lee", "artist": "Nick Cave", **position_tags})
        audio.save()
        changes = await indexer.process_file(str(path), force=True)
        if tid is None:
            tid = changes["tracks"]
        assert changes["tracks"] == tid

        # Read through a new connection, so this checks persisted columns.
        with sqlite3.connect(config.db_path) as conn:
            row = conn.execute(
                "SELECT track_number, disc_number FROM tracks WHERE id = ?", (tid,)
            ).fetchone()
            assert row == positions

        module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))
        item = await module.get(track_id(tid))
        info = (await module.get_track_info([tid]))[0]
        for metadata in (item.track, info.metadata):
            payload = metadata.model_dump(mode="json")
            assert (payload["track_number"], payload["disc_number"]) == positions
