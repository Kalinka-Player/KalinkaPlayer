#!/usr/bin/env python3
"""
Tests for Phase-0 0d: persist an embedded-art perceptual hash at index time
and a computed chromaprint when AcoustID runs. Both land in track_evidence
without disturbing the other evidence columns.
"""

import io

import aiosqlite
import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
from mutagen.flac import FLAC, Picture
from PIL import Image

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.enricher.enricher_db import AsyncEnricherDb
from kalinka_plugin_localfiles.indexer.indexer import FileIndexer
from kalinka_plugin_localfiles.indexer.indexer_db import AsyncIndexerDb
from kalinka_plugin_localfiles.utils.artwork_store import dhash_bytes


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


def _cover_png(color):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def _write_flac(path, tags, cover=None):
    sf.write(str(path), np.zeros(4410, dtype="float32"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    if cover is not None:
        pic = Picture()
        pic.type = 3
        pic.mime = "image/png"
        pic.data = cover
        audio.add_picture(pic)
    audio.save()


async def _evidence(config, track_id):
    async with aiosqlite.connect(config.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM track_evidence WHERE track_id = ?", (track_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


def test_art_phash_is_16_hex_and_stable():
    a1 = dhash_bytes(_cover_png((120, 30, 200)))
    a2 = dhash_bytes(_cover_png((120, 30, 200)))
    assert a1 is not None and len(a1) == 16
    int(a1, 16)  # valid hex
    assert a1 == a2  # same image -> same hash
    assert dhash_bytes(b"not an image") is None


@pytest.mark.asyncio
async def test_indexing_stores_art_phash(indexer):
    fi, music_dir, config = indexer
    path = music_dir / "01 - track.flac"
    _write_flac(path, {"title": "T", "artist": "A", "album": "X"},
                cover=_cover_png((10, 200, 90)))
    changes = await fi.process_file(str(path))
    ev = await _evidence(config, changes["tracks"])
    assert ev["art_phash"] is not None and len(ev["art_phash"]) == 16
    assert ev["art_type"] == 3


@pytest.mark.asyncio
async def test_retag_without_art_clears_phash(indexer):
    """A picture missing from a file that was read is a removed picture: a
    read that fails raises before the evidence is written. A kept hash would
    have the album cover chosen from art no file has any more."""
    fi, music_dir, config = indexer
    path = music_dir / "02 - track.flac"
    _write_flac(path, {"title": "T", "artist": "A", "album": "X"},
                cover=_cover_png((200, 10, 10)))
    changes = await fi.process_file(str(path))
    track_id = changes["tracks"]
    phash = (await _evidence(config, track_id))["art_phash"]
    assert phash is not None

    # Strip the picture, retag, bump mtime, reprocess.
    audio = FLAC(str(path))
    audio.clear_pictures()
    audio["album"] = "Y"
    audio.save()
    import os

    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 10))
    await fi.process_file(str(path))

    ev = await _evidence(config, track_id)
    assert ev["art_phash"] is None
    assert ev["art_type"] is None


@pytest.mark.asyncio
async def test_save_fingerprint_upserts_without_disturbing_evidence(indexer):
    fi, music_dir, config = indexer
    path = music_dir / "03 - track.flac"
    _write_flac(path, {"title": "T", "artist": "A", "album": "X"})
    changes = await fi.process_file(str(path))
    track_id = changes["tracks"]
    # Indexer wrote raw_tags/stream_info; the enricher now adds a fingerprint.
    before = await _evidence(config, track_id)
    assert before["raw_tags"] is not None
    assert before["fingerprint"] is None

    enr_db = AsyncEnricherDb(config)
    await enr_db.save_fingerprint(track_id, "AQADtMkYhYkYnGiOQ")

    after = await _evidence(config, track_id)
    assert after["fingerprint"] == "AQADtMkYhYkYnGiOQ"
    assert after["fp_computed_at"] is not None
    assert after["raw_tags"] == before["raw_tags"]  # untouched
    assert after["art_phash"] == before["art_phash"]


@pytest.mark.asyncio
async def test_save_fingerprint_creates_row_if_absent(tmp_path):
    config = LocalFilesConfig(
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    await init_db(config.db_path)
    enr_db = AsyncEnricherDb(config)
    await enr_db.save_fingerprint("track_orphan", "FPX")
    async with aiosqlite.connect(config.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM track_evidence WHERE track_id='track_orphan'"
        )
        row = dict(await cur.fetchone())
    assert row["fingerprint"] == "FPX"
    assert row["raw_tags"] is None
