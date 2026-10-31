"""The enricher works on a snapshot of a row while the indexer goes on
changing it. Writing the whole snapshot back undid the indexer's work — a
track regrouped into another album went back, a cover the indexer had just
found was cleared — so only what enrichment changed is written."""

from __future__ import annotations

import os

import aiosqlite
import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.enricher.enricher import MetadataEnricher
from kalinka_plugin_localfiles.enricher.enricher_db import AsyncEnricherDb
from kalinka_plugin_localfiles.enricher.procedural_artwork_plugin import (
    ProceduralArtworkPlugin,
)


class _FillsMbid:
    """A source that answers every row with an MBID and nothing else."""

    runs_after_resolution = False

    def can_enrich_album(self):
        return True

    def can_enrich_track(self):
        return True

    async def enrich_album(self, album):
        return {"updates": {"mbid": "release-1"}}

    async def enrich_track(self, track):
        return {"updates": {"mbid": "recording-1"}}


async def _library(tmp_path):
    cfg = LocalFilesConfig(
        db_path=str(tmp_path / "test.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    await init_db(cfg.db_path)
    async with aiosqlite.connect(cfg.db_path) as conn:
        await conn.execute("INSERT INTO artists (id, name) VALUES ('ar1', 'Air')")
        await conn.executemany(
            "INSERT INTO albums (id, title, artist_id, enriched) "
            "VALUES (?, ?, 'ar1', 0)",
            [("al1", "Moon Safari"), ("al2", "Talkie Walkie")],
        )
        await conn.execute(
            "INSERT INTO tracks (id, title, album_id, artist_id, file_path, "
            "format, duration, track_number, enriched) "
            "VALUES ('t1', 'La Femme d''Argent', 'al1', 'ar1', '/1.flac', "
            "'flac', 427, 1, 0)"
        )
        await conn.commit()
    db = AsyncEnricherDb(cfg)
    enricher = MetadataEnricher(cfg, db)
    enricher.plugins = [_FillsMbid()]
    return cfg, db, enricher


async def _row(cfg, table, row_id):
    async with aiosqlite.connect(cfg.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        return dict(await cursor.fetchone())


async def _execute(cfg, sql):
    async with aiosqlite.connect(cfg.db_path) as conn:
        await conn.execute(sql)
        await conn.commit()


@pytest.mark.asyncio
async def test_a_track_regrouped_meanwhile_keeps_its_new_album(tmp_path):
    cfg, db, enricher = await _library(tmp_path)
    snapshot = await _row(cfg, "tracks", "t1")
    await _execute(cfg, "UPDATE tracks SET album_id = 'al2' WHERE id = 't1'")

    await enricher._enrich_track(snapshot)

    track = await _row(cfg, "tracks", "t1")
    assert track["album_id"] == "al2"
    assert track["mbid"] == "recording-1"


@pytest.mark.asyncio
async def test_a_cover_found_meanwhile_is_not_cleared(tmp_path):
    cfg, db, enricher = await _library(tmp_path)
    snapshot = await _row(cfg, "albums", "al1")
    await _execute(
        cfg, "UPDATE albums SET image_url = 'al1.jpg', image_generated = 0 "
        "WHERE id = 'al1'"
    )

    await enricher._enrich_album(snapshot)

    album = await _row(cfg, "albums", "al1")
    assert album["image_url"] == "al1.jpg"
    assert album["mbid"] == "release-1"


@pytest.mark.asyncio
async def test_an_art_source_leaves_a_cover_found_while_it_worked(tmp_path):
    """Its decision was made on the snapshot; the files it would write
    would replace the cover the indexer just stored."""
    cfg, db, _ = await _library(tmp_path)
    snapshot = await _row(cfg, "albums", "al1")
    await _execute(
        cfg, "UPDATE albums SET image_url = 'al1.jpg', image_generated = 0 "
        "WHERE id = 'al1'"
    )

    assert await ProceduralArtworkPlugin(cfg, db).enrich_album(snapshot) is None
    assert not os.path.exists(os.path.join(cfg.artwork_path, "album", "al1_large.jpg"))


class _GeneratesWhileTheIndexerStoresARealCover:
    """A generated-art source whose album gains a real cover from the
    indexer before the enricher writes the row back."""

    runs_after_resolution = False

    def __init__(self, db_path):
        self.db_path = db_path

    def can_enrich_album(self):
        return True

    async def enrich_album(self, album):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE albums SET image_url = 'al1.jpg', image_generated = 0 "
                "WHERE id = 'al1'"
            )
            await conn.commit()
        return {"updates": {"image_url": "al1", "image_generated": 1}}


@pytest.mark.asyncio
async def test_a_real_cover_stored_during_enrichment_is_not_flagged_generated(
    tmp_path,
):
    cfg, db, enricher = await _library(tmp_path)
    enricher.plugins = [_GeneratesWhileTheIndexerStoresARealCover(cfg.db_path)]

    await enricher._enrich_album(await _row(cfg, "albums", "al1"))

    album = await _row(cfg, "albums", "al1")
    assert (album["image_url"], album["image_generated"]) == ("al1.jpg", 0)
