"""Recommendations in CLAP space: what carries a queue on, and albums to try."""

import os
import tempfile

import aiosqlite
import numpy as np
import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.embedder.embedder_db import AsyncEmbedderDb
from kalinka_plugin_localfiles.embedding_utils import encode_embedding, normalise
from kalinka_plugin_localfiles.searcher.recommender import Recommender, pick, taste
from kalinka_plugin_localfiles.searcher.searcher_db import AsyncSearcherDb

DIM = 512


def _direction(seed):
    return normalise(np.random.default_rng(seed).standard_normal(DIM).astype(np.float32))


def _near(base, seed, spread=0.2):
    return normalise(base + spread * _direction(seed))


def test_candidates_like_a_turned_down_track_rank_lower():
    taste_vec = _direction(1)
    disliked = _direction(2)
    candidates = {
        # cos to the taste ~0.71, and as like the turned-down track
        "like_both": normalise(taste_vec + disliked),
        # cos to the taste ~0.55, unlike the turned-down track
        "like_taste": _near(taste_vec, 3, 1.5),
    }

    assert pick(taste_vec, candidates, [], 1) == ["like_both"]
    assert pick(taste_vec, candidates, [disliked], 1) == ["like_taste"]


def test_a_copy_of_a_turned_down_track_is_never_offered():
    taste_vec = _direction(1)
    disliked = _near(taste_vec, 2, 0.3)
    candidates = {"copy": normalise(disliked + 0.001 * _direction(3)), "other": _near(taste_vec, 4, 0.5)}

    assert pick(taste_vec, candidates, [disliked], 2) == ["other"]


def test_picks_spread_out_and_never_repeat_a_recording():
    taste_vec = _direction(1)
    twin = _near(taste_vec, 2, 0.05)
    candidates = {
        "a": twin,
        "a_again": normalise(twin + 0.001 * _direction(3)),
        "b": _near(taste_vec, 4, 0.6),
    }

    first, second = pick(taste_vec, candidates, [], 2)

    assert first in {"a", "a_again"}
    assert second == "b"


def test_nothing_to_pick_from_picks_nothing():
    assert pick(_direction(1), {}, [], 3) == []
    assert pick(_direction(1), {"a": _direction(2)}, [], 0) == []


def test_later_seeds_weigh_more():
    old, new = _direction(1), _direction(2)

    t = taste([old, new], decay=0.5)

    assert float(t @ new) > float(t @ old)


async def _library(path, tracks, albums, plays=(), arrivals=()):
    """tracks: {track_id: (album_id, vector)}; albums: {album_id: vector}."""
    await init_db(path)
    async with aiosqlite.connect(path) as conn:
        for album_id in albums:
            await conn.execute(
                "INSERT INTO albums (id, title, artist_id, last_updated) VALUES (?, ?, 'unknown_artist', 1)",
                (album_id, album_id),
            )
        for track_id, (album_id, _) in tracks.items():
            await conn.execute(
                "INSERT INTO tracks (id, title, album_id, artist_id, file_path, format, enriched)"
                " VALUES (?, ?, ?, 'unknown_artist', ?, 'flac', 1)",
                (track_id, track_id, album_id, f"/m/{track_id}"),
            )
        for track_id, completed, skipped in plays:
            await conn.execute(
                "INSERT INTO play_history (track_id, played_at, listened_s, completed, skipped)"
                " VALUES (?, 1, 100, ?, ?)",
                (track_id, int(completed), int(skipped)),
            )
        for track_id, when in arrivals:
            await conn.execute(
                "INSERT INTO library_file (file_id, current_path, first_indexed) VALUES (?, ?, ?)",
                (track_id, f"/m/{track_id}", when),
            )
        await conn.commit()
    cfg = LocalFilesConfig(db_path=path)
    edb = AsyncEmbedderDb(cfg)
    await edb._check_vec_available()
    for track_id, (_, vector) in tracks.items():
        await edb.complete_clap_job(0, track_id, encode_embedding(vector), version=4)
    for album_id, vector in albums.items():
        await edb.update_album_embedding(album_id, encode_embedding(vector))
    sdb = AsyncSearcherDb(cfg)
    await sdb._check_vec_available()
    return Recommender(sdb)


@pytest.fixture
def db_path():
    pytest.importorskip("sqlite_vec")
    with tempfile.TemporaryDirectory() as d:
        yield os.path.join(d, "r.db")


@pytest.mark.asyncio
async def test_next_tracks_follow_the_seeds_and_skip_queued_and_turned_down(db_path):
    rock, synth = _direction(10), _direction(20)
    tracks = {
        "seed": ("al", rock),
        "queued": ("al", _near(rock, 1, 0.05)),
        "rock_close": ("al", _near(rock, 2, 0.3)),
        "rock_far": ("al", _near(rock, 3, 0.8)),
        "synth": ("al", synth),
        "disliked": ("al", _near(rock, 4, 0.3)),
    }
    recommender = await _library(db_path, tracks, {"al": rock})

    picked = await recommender.next_tracks(["seed"], ["disliked"], ["seed", "queued"], 2)

    assert picked == ["rock_close", "rock_far"]


async def _keep_vectors_only_in_the_index(path):
    async with aiosqlite.connect(path) as conn:
        await conn.execute("UPDATE tracks SET embedding_clap_audio = NULL")
        await conn.execute("UPDATE albums SET embedding_clap = NULL")
        await conn.commit()


@pytest.mark.asyncio
async def test_vectors_kept_only_in_the_knn_index_still_recommend(db_path):
    rock, jazz = _direction(10), _direction(30)
    tracks = {
        "seed": ("rock_album", rock),
        "rock_close": ("rock_album", _near(rock, 2, 0.3)),
        **{f"j{i}": ("heard_jazz", _near(jazz, i, 0.2)) for i in range(5)},
    }
    albums = {"rock_album": rock, "heard_jazz": jazz, "more_jazz": _near(jazz, 50, 0.3)}
    plays = [(f"j{i}", True, False) for i in range(5)]
    recommender = await _library(db_path, tracks, albums, plays=plays)
    await _keep_vectors_only_in_the_index(db_path)

    assert (await recommender.next_tracks(["seed"], [], ["seed"], 1)) == ["rock_close"]
    assert (await recommender.albums_for_you(1)) == ["more_jazz"]


async def _remove_as_the_indexer_does(path, table, entry_id):
    async with aiosqlite.connect(path) as conn:
        await conn.execute(f"DELETE FROM {table} WHERE id = ?", (entry_id,))
        await conn.commit()


@pytest.mark.asyncio
async def test_a_removed_track_left_in_the_index_is_never_proposed(db_path):
    rock = _direction(10)
    tracks = {
        "seed": ("al", rock),
        "gone": ("al", _near(rock, 1, 0.1)),
        "still_here": ("al", _near(rock, 2, 0.4)),
    }
    recommender = await _library(db_path, tracks, {"al": rock})
    await _remove_as_the_indexer_does(db_path, "tracks", "gone")

    assert await recommender.next_tracks(["seed"], [], ["seed"], 1) == ["still_here"]


@pytest.mark.asyncio
async def test_a_removed_album_left_in_the_index_is_never_recommended(db_path):
    jazz = _direction(30)
    tracks = {f"j{i}": ("heard_jazz", _near(jazz, i, 0.2)) for i in range(5)}
    albums = {"heard_jazz": jazz, "gone": _near(jazz, 50, 0.1), "more_jazz": _near(jazz, 51, 0.4)}
    plays = [(f"j{i}", True, False) for i in range(5)]
    recommender = await _library(db_path, tracks, albums, plays=plays)
    await _remove_as_the_indexer_does(db_path, "albums", "gone")

    assert await recommender.albums_for_you(1) == ["more_jazz"]


@pytest.mark.asyncio
async def test_the_embedder_prunes_index_rows_of_removed_entries(db_path):
    rock = _direction(10)
    tracks = {"kept": ("al", rock), "gone": ("al", _near(rock, 1, 0.3))}
    await _library(db_path, tracks, {"al": rock, "gone_album": _direction(20)})
    await _remove_as_the_indexer_does(db_path, "tracks", "gone")
    await _remove_as_the_indexer_does(db_path, "albums", "gone_album")
    embedder_db = AsyncEmbedderDb(LocalFilesConfig(db_path=db_path))
    await embedder_db._check_vec_available()

    assert await embedder_db.prune_orphan_vec_rows() == 2
    assert await embedder_db.prune_orphan_vec_rows() == 0
    vectors = AsyncSearcherDb(LocalFilesConfig(db_path=db_path))
    await vectors._check_vec_available()
    assert set(await vectors.get_track_vectors(["kept", "gone"])) == {"kept"}
    assert set(await vectors.get_album_vectors(["al", "gone_album"])) == {"al"}


@pytest.mark.asyncio
async def test_next_tracks_without_a_known_seed_propose_nothing(db_path):
    recommender = await _library(db_path, {"t": ("al", _direction(1))}, {"al": _direction(1)})

    assert await recommender.next_tracks(["missing"], [], [], 3) == []


@pytest.mark.asyncio
async def test_albums_come_from_what_was_heard_out_and_skip_what_was_heard(db_path):
    jazz, metal, folk = _direction(30), _direction(40), _direction(45)
    tracks = {f"j{i}": ("heard_jazz", _near(jazz, i, 0.2)) for i in range(5)}
    tracks["m"] = ("metal_single", metal)
    albums = {
        "heard_jazz": jazz,
        "more_jazz": _near(jazz, 50, 0.3),
        # Its one track was skipped, so the album is that recording.
        "metal_single": metal,
        "folk": folk,
    }
    plays = [(f"j{i}", True, False) for i in range(5)] + [("m", False, True)]
    recommender = await _library(db_path, tracks, albums, plays=plays)

    assert await recommender.albums_for_you(3) == ["more_jazz", "folk"]
    assert await recommender.albums_for_you(1) == ["more_jazz"]


@pytest.mark.asyncio
async def test_without_enough_history_the_newest_albums_stand_in_for_a_taste(db_path):
    ambient, punk = _direction(60), _direction(70)
    albums = {
        "new1": ambient,
        "new2": _near(ambient, 1, 0.4),
        "new3": _near(ambient, 2, 0.4),
        "old_ambient": _near(ambient, 3, 0.4),
        "punk": punk,
    }
    tracks = {f"t-{a}": (a, v) for a, v in albums.items()}
    arrivals = [("t-new1", 500), ("t-new2", 400), ("t-new3", 300), ("t-old_ambient", 100), ("t-punk", 50)]
    recommender = await _library(db_path, tracks, albums, arrivals=arrivals)

    assert await recommender.albums_for_you(2) == ["old_ambient", "punk"]
