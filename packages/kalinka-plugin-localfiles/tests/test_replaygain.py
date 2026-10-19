import json
from types import SimpleNamespace

import aiosqlite
import pytest
from mutagen.id3 import ID3, TXXX

from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.replaygain_tags import read_replaygain


def test_case_insensitive_album_and_track_tags_ignore_invalid_values():
    assert read_replaygain(
        {
            "REPLAYGAIN_ALBUM_GAIN": [" -4.25 DB "],
            "TXXX:REPLAYGAIN_TRACK_GAIN": "+2.0 dB",
            "replaygain_album_peak": ["0.999"],
            "replaygain_track_peak": ["nan"],
        }
    ) == {
        "replaygain_album_gain": -4.25,
        "replaygain_gain": 2.0,
        "replaygain_album_peak": 0.999,
    }
    assert (
        read_replaygain(
            {
                "replaygain_track_gain": ["inf"],
                "replaygain_album_gain": [],
                "replaygain_album_peak": "invalid",
            }
        )
        == {}
    )


def test_id3_album_tags_survive_metadata_extraction():
    tags = ID3()
    tags.add(TXXX(encoding=3, desc="REPLAYGAIN_ALBUM_GAIN", text=["-4.25 dB"]))
    tags.add(TXXX(encoding=3, desc="REPLAYGAIN_ALBUM_PEAK", text=["0.999"]))
    tags.add(TXXX(encoding=3, desc="replaygain_track_gain", text=["-6.5 dB"]))
    audio = SimpleNamespace(tags=tags, info=SimpleNamespace(length=123))
    metadata = FileIndexer._extract_id3_metadata(None, audio, {}, "mp3")
    assert metadata["replaygain_album_gain"] == -4.25
    assert metadata["replaygain_album_peak"] == 0.999
    assert metadata["replaygain_gain"] == -6.5


@pytest.mark.asyncio
async def test_existing_library_recovers_album_gain_from_saved_tags(tmp_path):
    path = str(tmp_path / "library.db")
    await init_db(path)
    async with aiosqlite.connect(path) as conn:
        await conn.execute("ALTER TABLE tracks DROP COLUMN replaygain_album_gain")
        await conn.execute("ALTER TABLE tracks DROP COLUMN replaygain_album_peak")
        await conn.execute(
            "INSERT INTO tracks (id, title, file_path, format, replaygain_gain) "
            "VALUES ('song', 'Song', '/song.flac', 'audio/flac', -6.5)"
        )
        await conn.execute(
            "INSERT INTO track_evidence (track_id, raw_tags) VALUES ('song', ?)",
            (
                json.dumps(
                    {
                        "REPLAYGAIN_ALBUM_GAIN": ["-4.25 dB"],
                        "replaygain_album_peak": ["0.999"],
                    }
                ),
            ),
        )
        await conn.commit()
    await init_db(path)
    await init_db(path)
    async with aiosqlite.connect(path) as conn:
        row = await (
            await conn.execute(
                "SELECT replaygain_gain, replaygain_album_gain, replaygain_album_peak FROM tracks WHERE id='song'"
            )
        ).fetchone()
    assert row == (-6.5, -4.25, 0.999)
