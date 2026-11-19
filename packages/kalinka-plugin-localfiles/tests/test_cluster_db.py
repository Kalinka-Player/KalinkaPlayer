#!/usr/bin/env python3
"""
Tests for Phase-1 1a: the clustering schema and its data layer —
album_cluster / membership_constraint / entity_id_alias — with emphasis on
alias flatten-on-write (chains must stay single-hop) and cluster generation
bumping (hysteresis).
"""

import aiosqlite
import pytest

from kalinka_plugin_localfiles.clustering.cluster_db import AsyncClusterDb
from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db


async def _cols(db_path, table):
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(f"PRAGMA table_info({table})")
        return {r[1] for r in await cur.fetchall()}


@pytest.fixture
def config(tmp_path):
    return LocalFilesConfig(
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )


@pytest.mark.asyncio
async def test_schema_tables_exist(config):
    await init_db(config.db_path)
    assert "primary_folder" in await _cols(config.db_path, "album_cluster")
    assert {"kind", "track_id", "album_id"} <= await _cols(
        config.db_path, "membership_constraint"
    )
    assert {"old_id", "current_id", "entity_type"} == await _cols(
        config.db_path, "entity_id_alias"
    )


@pytest.mark.asyncio
async def test_resolve_id_passthrough_and_alias(config):
    await init_db(config.db_path)
    db = AsyncClusterDb(config)
    assert await db.resolve_id("album_x") == "album_x"  # no alias -> itself
    await db.add_alias("album_old", "album_new", "album")
    assert await db.resolve_id("album_old") == "album_new"


@pytest.mark.asyncio
async def test_alias_chain_is_flattened(config):
    await init_db(config.db_path)
    db = AsyncClusterDb(config)
    # A -> B, then B merged into C: both A and B must resolve directly to C.
    await db.add_alias("A", "B", "album")
    await db.add_alias("B", "C", "album")
    assert await db.resolve_id("A") == "C"  # single hop, not A->B->C
    assert await db.resolve_id("B") == "C"
    # And the stored row for A points straight at C.
    async with aiosqlite.connect(config.db_path) as conn:
        cur = await conn.execute(
            "SELECT current_id FROM entity_id_alias WHERE old_id='A'"
        )
        assert (await cur.fetchone())[0] == "C"


@pytest.mark.asyncio
async def test_add_alias_noop_on_self(config):
    await init_db(config.db_path)
    db = AsyncClusterDb(config)
    await db.add_alias("X", "X", "album")
    assert await db.resolve_id("X") == "X"
    async with aiosqlite.connect(config.db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM entity_id_alias")
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_cluster_upsert_and_generation(config):
    await init_db(config.db_path)
    db = AsyncClusterDb(config)
    await db.upsert_cluster("album_1", "/m/A", grouping_conf=0.8, kind="album")
    c = await db.get_cluster("album_1")
    assert c["primary_folder"] == "/m/A"
    assert c["kind"] == "album"
    assert c["generation"] == 0

    # Plain update: generation unchanged.
    await db.upsert_cluster("album_1", "/m/A", grouping_conf=0.9)
    assert (await db.get_cluster("album_1"))["generation"] == 0

    # Re-cluster: generation bumped.
    await db.upsert_cluster("album_1", "/m/A", bump_generation=True)
    assert (await db.get_cluster("album_1"))["generation"] == 1


@pytest.mark.asyncio
async def test_a_cluster_stored_as_given_is_left_alone(config, write_locked):
    await init_db(config.db_path)
    db = AsyncClusterDb(config)
    await db.upsert_cluster("album_1", "/m/A", kind="album", bump_generation=True)

    with write_locked(config.db_path):
        await db.upsert_cluster("album_1", "/m/A", kind="album", bump_generation=True)
    assert (await db.get_cluster("album_1"))["generation"] == 0

    await db.upsert_cluster("album_1", "/m/B", kind="album", bump_generation=True)
    cluster = await db.get_cluster("album_1")
    assert cluster["primary_folder"] == "/m/B"
    assert cluster["generation"] == 1


@pytest.mark.asyncio
async def test_constraints_lookup(config):
    await init_db(config.db_path)
    db = AsyncClusterDb(config)
    async with aiosqlite.connect(config.db_path) as conn:
        await conn.execute(
            "INSERT INTO membership_constraint "
            "(id, kind, track_id, album_id, created_at) VALUES "
            "('c1', 'include', 't1', 'album_1', 1), "
            "('c2', 'exclude', 't2', 'album_1', 1)"
        )
        await conn.commit()
    rows = await db.get_constraints_for_tracks(["t1", "t2", "t3"])
    assert {r["id"] for r in rows} == {"c1", "c2"}
    assert await db.get_constraints_for_tracks([]) == []
