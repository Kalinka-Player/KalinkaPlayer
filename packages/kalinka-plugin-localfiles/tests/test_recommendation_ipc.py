"""The library's recommendations as the server and the app reach them: queue
continuation, albums you may like, and searches that name a decade."""

import asyncio
import os
import sqlite3
import tempfile
from unittest.mock import Mock

import numpy as np
import pytest

from kalinka_plugin_sdk.datamodel import EntityId, EntityType
from kalinka_plugin_sdk.inputmodule import ContinuationRequest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.db_schema import init_db
from kalinka_plugin_localfiles.embedding_utils import encode_embedding, normalise
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.searcher import searcher as searcher_mod
from kalinka_plugin_localfiles.searcher.searcher import SearchWorker
from kalinka_plugin_localfiles.searcher.searcher_db import AsyncSearcherDb


def _direction(seed):
    return normalise(np.random.default_rng(seed).standard_normal(512).astype(np.float32))


def _seed(path):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO artists (id, name, last_updated) VALUES ('ar', 'Artist', 1)")
        for album, year, direction in (("al-70s", 1974, 1), ("al-90s", 1996, 2)):
            conn.execute(
                "INSERT INTO albums (id, title, artist_id, year, last_updated) VALUES (?, ?, 'ar', ?, 1)",
                (album, album, year),
            )
            for n in range(2):
                vector = normalise(_direction(direction) + 0.2 * _direction(10 * direction + n))
                conn.execute(
                    "INSERT INTO tracks (id, title, album_id, artist_id, file_path, format,"
                    " last_updated, embedding_clap_audio) VALUES (?, ?, ?, 'ar', ?, 'flac', 1, ?)",
                    (f"{album}-{n}", f"{album} {n}", album, f"/m/{album}-{n}", encode_embedding(vector)),
                )


@pytest.fixture
def config():
    d = tempfile.mkdtemp()
    cfg = LocalFilesConfig(db_path=os.path.join(d, "l.db"), artwork_path=os.path.join(d, "art"))
    asyncio.run(init_db(cfg.db_path))
    _seed(cfg.db_path)
    return cfg


def _module(config, answer):
    from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule

    module = LocalFilesInputModule.__new__(LocalFilesInputModule)
    module.db_manager = LocalFilesInputModuleDb(config)
    module._music_folders = []
    module._search_lock = asyncio.Lock()
    module._searcher = None if answer is None else Mock(ask=Mock(return_value=answer), timeout=5)
    return module


@pytest.mark.asyncio
async def test_continuation_asks_the_searcher_with_the_whole_request(config):
    module = _module(config, {"tracks": ["al-70s-1", "al-70s-0"]})
    request = ContinuationRequest(seeds=["a", "b"], avoid=["c"], exclude=["a", "b", "c"], count=2)

    assert await module.continue_queue(request) == ["al-70s-1", "al-70s-0"]
    module._searcher.ask.assert_called_once_with(
        {"op": "continue", "seeds": ["a", "b"], "avoid": ["c"], "exclude": ["a", "b", "c"], "count": 2}
    )


@pytest.mark.asyncio
async def test_continuation_without_a_searcher_or_answer_proposes_nothing(config):
    assert await _module(config, None).continue_queue(ContinuationRequest(seeds=["a"])) == []
    failing = _module(config, {})
    failing._searcher.ask.side_effect = ValueError("Queue is closed")
    assert await failing.continue_queue(ContinuationRequest(seeds=["a"])) == []


@pytest.mark.asyncio
async def test_albums_you_may_like_list_in_the_searchers_order(config):
    module = _module(config, {"albums": ["al-90s", "al-70s"]})

    result = await module.browse(EntityId(id="for-you", type=EntityType.CATALOG, source="localfiles"))

    assert [item.id.id for item in result.items] == ["al-90s", "al-70s"]
    assert module._searcher.ask.call_args.args[0]["op"] == "albums"


def test_the_root_offers_album_recommendations_only_with_a_searcher(config):
    with_searcher = [item.id.id for item in _module(config, {})._browse_root(0, 20).items]
    without = [item.id.id for item in _module(config, None)._browse_root(0, 20).items]

    assert "for-you" in with_searcher
    assert "for-you" not in without


def _worker(config):
    searcher_mod._ensure_numpy()
    worker = SearchWorker(config, AsyncSearcherDb(config))
    worker._mood_index = [["calm"], np.array([[5.0, 2.0]], np.float32), np.eye(1, 512, dtype=np.float32)]
    return worker


@pytest.mark.asyncio
async def test_the_searcher_serves_each_kind_of_request(config):
    worker = _worker(config)
    worker._recommender = Mock()

    async def next_tracks(*args):
        return ["t"]

    async def albums_for_you(count):
        return ["a"] * count

    worker._recommender.next_tracks = Mock(side_effect=next_tracks)
    worker._recommender.albums_for_you = Mock(side_effect=albums_for_you)

    assert await worker._handle({"op": "continue", "seeds": ["s"], "count": 1}) == {"tracks": ["t"]}
    assert await worker._handle({"op": "albums", "count": 2}) == {"albums": ["a", "a"]}
    worker._recommender.next_tracks.assert_called_once_with(["s"], [], [], 1)


@pytest.mark.asyncio
async def test_a_search_naming_a_decade_keeps_to_albums_of_that_decade(config, monkeypatch):
    worker = _worker(config)

    async def nineties_like(_query):
        return encode_embedding(_direction(2))

    monkeypatch.setattr(worker, "_encode_query_blob", nineties_like)

    seventies = await worker._do_search("70s rock", 10)
    fifties = await worker._do_search("50s rock", 10)

    assert set(seventies["tracks"]) == {"al-70s-0", "al-70s-1"}
    assert fifties == {"tracks": []}
