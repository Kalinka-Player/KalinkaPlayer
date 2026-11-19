#!/usr/bin/env python3
"""AsyncEnricherDb claims persistence (Phase 2d): record_claim / get_claims /
record_resolved_origin round-trip and upsert on the real schema.
"""

import aiosqlite
import pytest
import pytest_asyncio

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.enricher.enricher_db import AsyncEnricherDb
from kalinka_plugin_localfiles.resolution import provenance_db


@pytest_asyncio.fixture
async def db(tmp_path):
    cfg = LocalFilesConfig(
        music_folders=[str(tmp_path)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "art"),
    )
    await init_db(cfg.db_path)
    return AsyncEnricherDb(cfg)


@pytest.mark.asyncio
async def test_claim_roundtrip_and_upsert(db):
    await db.record_claim("artist", "a1", "name", "The Beatles",
                          "musicbrainz:m1", "inferred")
    await db.record_claim("artist", "a1", "name", "THE BEATLES",
                          "tag_consensus", "observed")
    claims = await db.get_claims("artist", "a1", "name")
    by_source = {c["source"]: c for c in claims}
    assert by_source["musicbrainz:m1"]["value"] == "The Beatles"
    assert by_source["tag_consensus"]["tier"] == "observed"

    # Same (entity, field, source) upserts rather than duplicating.
    await db.record_claim("artist", "a1", "name", "The Beatles!",
                          "musicbrainz:m1", "verified")
    claims = await db.get_claims("artist", "a1", "name")
    assert len(claims) == 2
    mb = next(c for c in claims if c["source"] == "musicbrainz:m1")
    assert mb["value"] == "The Beatles!" and mb["tier"] == "verified"


@pytest.mark.asyncio
async def test_resolved_origin_upsert(db):
    await db.record_resolved_origin("artist", "a1", "name",
                                    "tag_consensus", "observed")
    await db.record_resolved_origin("artist", "a1", "name",
                                    "musicbrainz:m1", "inferred", "release:m1")
    async with aiosqlite.connect(db.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM resolved_origin WHERE entity_id='a1' AND field='name'"
        )
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1                       # one row per field
    assert rows[0]["source"] == "musicbrainz:m1"
    assert rows[0]["evidence_ref"] == "release:m1"


async def _origin(db, entity_id):
    async with aiosqlite.connect(db.db_path) as conn:
        cur = await conn.execute(
            "SELECT source, tier, resolved_at FROM resolved_origin "
            "WHERE entity_id = ? AND field = 'name'",
            (entity_id,),
        )
        return tuple(await cur.fetchone())


@pytest.mark.asyncio
async def test_an_origin_already_recorded_is_left_alone(db, write_locked, monkeypatch):
    monkeypatch.setattr(provenance_db.time, "time", lambda: 1000)
    await db.record_resolved_origin("artist", "a1", "name", "tag_consensus", "observed")

    monkeypatch.setattr(provenance_db.time, "time", lambda: 2000)
    with write_locked(db.db_path):
        await db.record_resolved_origin(
            "artist", "a1", "name", "tag_consensus", "observed"
        )
    assert await _origin(db, "a1") == ("tag_consensus", "observed", 1000)

    await db.record_resolved_origin("artist", "a1", "name", "musicbrainz:m1", "inferred")
    assert await _origin(db, "a1") == ("musicbrainz:m1", "inferred", 2000)


@pytest.mark.asyncio
async def test_a_guess_kept_off_a_stronger_origin_writes_nothing(db, write_locked):
    await db.record_resolved_origin("artist", "a1", "name", "tag_consensus", "observed")
    with write_locked(db.db_path):
        await db.record_resolved_origin(
            "artist", "a1", "name", "folder_name", "guessed", keep_stronger=True
        )
    assert (await _origin(db, "a1"))[:2] == ("tag_consensus", "observed")


@pytest.mark.asyncio
async def test_a_guess_still_replaces_a_guess(db):
    await db.record_resolved_origin("artist", "a1", "name", "filename", "guessed")
    await db.record_resolved_origin(
        "artist", "a1", "name", "folder_name", "guessed", keep_stronger=True
    )
    assert (await _origin(db, "a1"))[:2] == ("folder_name", "guessed")
