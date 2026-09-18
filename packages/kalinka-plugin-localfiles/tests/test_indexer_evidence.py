#!/usr/bin/env python3
"""
Tests for Phase-0 evidence capture (0b): the indexer records verbatim raw
tags (including albumartist and the compilation flag, which the display
tables don't carry) and stream info into track_evidence when it processes a
file.

Uses a real FLAC (soundfile) so the mutagen extraction path is exercised
end-to-end, plus a stubbed-extraction case for the process_file write path.
"""

import json

import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from mutagen.flac import FLAC

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb


@pytest_asyncio.fixture
async def indexer(tmp_path):
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    config = LocalFilesConfig(
        music_folders=[str(music_dir)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
        quiescence_seconds=0,
    )
    await init_db(config.db_path)
    return FileIndexer(config, AsyncIndexerDb(config)), music_dir, config


def _write_flac(path, tags):
    # A short, valid FLAC so the real mutagen path runs.
    sf.write(str(path), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    audio.save()


async def _evidence(config, track_id):
    import aiosqlite

    async with aiosqlite.connect(config.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM track_evidence WHERE track_id = ?", (track_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


@pytest.mark.asyncio
async def test_flac_evidence_captures_albumartist_and_compilation(indexer):
    fi, music_dir, config = indexer
    path = music_dir / "01 - track.flac"
    _write_flac(
        path,
        {
            "title": "Track One",
            "artist": "The Performer",
            "albumartist": "Various Artists",
            "album": "Some Comp",
            "compilation": "1",
            "tracknumber": "1",
        },
    )

    changes = await fi.process_file(str(path))
    track_id = changes["tracks"]

    ev = await _evidence(config, track_id)
    assert ev is not None
    raw = json.loads(ev["raw_tags"])
    # albumartist + compilation are captured verbatim even though the tracks
    # table has no column for them.
    assert raw["albumartist"] == ["Various Artists"]
    assert raw["compilation"] == ["1"]
    assert raw["artist"] == ["The Performer"]

    stream = json.loads(ev["stream_info"])
    assert stream["codec"] == "flac"
    assert stream["sample_rate"] == 44100
    assert stream["channels"] == 1
    assert stream["bits_per_sample"] == 16


@pytest.mark.asyncio
async def test_evidence_refreshed_on_reprocess(indexer):
    fi, music_dir, config = indexer
    path = music_dir / "02 - track.flac"
    _write_flac(path, {"title": "V1", "artist": "A", "album": "X"})
    changes = await fi.process_file(str(path))
    track_id = changes["tracks"]

    # Retag and bump mtime so process_file re-reads the file.
    audio = FLAC(str(path))
    audio["albumartist"] = "New AA"
    audio.save()
    import os

    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 10))

    await fi.process_file(str(path))
    ev = await _evidence(config, track_id)
    raw = json.loads(ev["raw_tags"])
    assert raw["albumartist"] == ["New AA"]  # snapshot refreshed in place


@pytest.mark.asyncio
async def test_evidence_written_via_process_file_write_path(indexer):
    """Stubbed extraction: confirms process_file persists whatever evidence
    the extractor returns, independent of the mutagen path."""
    fi, music_dir, config = indexer
    path = music_dir / "03 - track.mp3"
    path.write_bytes(b"x")
    fi._extract_metadata = lambda _s, _p: {
        "format": "audio/mpeg",
        "duration": 100,
        "title": "Stub",
        "artist": "Stub Artist",
        "raw_tags": {"TPE2": "Album Artist", "TCMP": "1"},
        "stream_info": {"codec": "mp3", "sample_rate": 44100},
    }
    changes = await fi.process_file(str(path))
    ev = await _evidence(config, changes["tracks"])
    assert json.loads(ev["raw_tags"])["TPE2"] == "Album Artist"
    assert json.loads(ev["stream_info"])["codec"] == "mp3"
