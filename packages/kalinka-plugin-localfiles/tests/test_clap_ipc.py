"""Tests for CLAP text-encoding IPC between searcher and embedder."""

import asyncio
import multiprocessing
import threading
from unittest.mock import Mock, patch

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.ipc import RequestChannel, reply
from kalinka_plugin_localfiles.searcher.searcher import SearchWorker
from kalinka_plugin_localfiles.searcher.searcher_db import AsyncSearcherDb


async def _async_noop(*args, **kwargs):
    return None


def _make_config(**overrides) -> LocalFilesConfig:
    return LocalFilesConfig(**overrides)


def _make_worker(
    text_encode_req=None, text_encode_resp=None
) -> SearchWorker:
    config = _make_config()
    db = Mock(spec=AsyncSearcherDb)
    db._vec_available = True
    return SearchWorker(config, db, text_encode_req, text_encode_resp)


class TestSearchWorkerEncodeQueryText:
    """SearchWorker._encode_query_text sends IPC and returns the blob."""

    def test_returns_blob_from_embedder(self):
        req_q = multiprocessing.Queue()
        resp_q = multiprocessing.Queue()
        worker = _make_worker(req_q, resp_q)

        fake_blob = b"\x00" * 2048  # 512 floats * 4 bytes
        # Simulate embedder responding
        def fake_embedder():
            msg = req_q.get(timeout=5)
            assert msg["query"] == "relaxing piano"
            reply(resp_q, msg, {"blob": fake_blob})

        t = threading.Thread(target=fake_embedder)
        t.start()

        result = worker._encode_query_text("relaxing piano")
        t.join(timeout=5)

        assert result == fake_blob

    def test_returns_none_when_embedder_returns_none(self):
        req_q = multiprocessing.Queue()
        resp_q = multiprocessing.Queue()
        worker = _make_worker(req_q, resp_q)

        def fake_embedder():
            reply(resp_q, req_q.get(timeout=5), {"blob": None})

        t = threading.Thread(target=fake_embedder)
        t.start()

        result = worker._encode_query_text("some query")
        t.join(timeout=5)

        assert result is None

    def test_returns_none_when_no_queues(self):
        worker = _make_worker(None, None)
        result = worker._encode_query_text("some query")
        assert result is None

    def test_returns_none_on_timeout(self):
        req_q = multiprocessing.Queue()
        resp_q = multiprocessing.Queue()
        worker = _make_worker(req_q, resp_q)
        worker._embedder = RequestChannel(req_q, resp_q, timeout=0.1)

        assert worker._encode_query_text("timeout query") is None


async def _ask_handler(handler, request: dict) -> dict:
    """Run a request handler loop, ask it one thing, and stop it."""
    req_q, resp_q = multiprocessing.Queue(), multiprocessing.Queue()
    shutdown = asyncio.Event()
    task = asyncio.create_task(handler(req_q, resp_q, shutdown))
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, RequestChannel(req_q, resp_q, timeout=10).ask, request
        )
    finally:
        shutdown.set()
        await task


class TestHandlersAnswerTheirOwnRequest:
    """Both answering loops echo the request id; without it every ask would
    time out once the asker matches replies to requests."""

    @pytest.mark.asyncio
    async def test_embedder_text_encode_handler(self):
        from kalinka_plugin_localfiles.embedder.embedder import EmbeddingWorker
        from kalinka_plugin_localfiles.embedder.embedder_db import AsyncEmbedderDb

        worker = EmbeddingWorker(_make_config(), Mock(spec=AsyncEmbedderDb))
        worker._ensure_text_model = lambda: True
        worker._encode_query = lambda query: f"vector:{query}".encode()

        answer = await _ask_handler(
            worker._run_text_encode_handler, {"query": "piano"}
        )

        assert answer["blob"] == b"vector:piano"

    @pytest.mark.asyncio
    async def test_searcher_search_handler(self):
        worker = _make_worker()

        async def fake_search(query, limit):
            return {"tracks": [f"{query}:{limit}"]}

        worker._do_search = fake_search

        answer = await _ask_handler(
            worker._run_search_handler, {"query": "piano", "limit": 3}
        )

        assert answer["tracks"] == ["piano:3"]


class _TracksDb:
    def __init__(self, *tracks):
        self._tracks = {t["id"]: t for t in tracks}

    def get_tracks_by_ids(self, track_ids):
        return [self._tracks[i] for i in track_ids if i in self._tracks]

    def get_album_by_id(self, album_id):
        return None


def _track(tmp_path, track_id: str, title: str) -> dict:
    return {
        "id": track_id,
        "album_id": "album_1",
        "artist_id": "artist_1",
        "album_title": "Album",
        "artist_name": "Artist",
        "title": title,
        "duration": 100,
        "format": "audio/mpeg",
        "file_path": str(tmp_path / "music" / f"{track_id}.mp3"),
    }


class TestAiSearchAfterATimeout:
    @pytest.mark.asyncio
    async def test_the_next_search_gets_its_own_tracks(self, tmp_path):
        """A search that timed out leaves the searcher's late answer queued;
        the next search must not show it."""
        from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule

        config = _make_config(
            music_folders=[str(tmp_path / "music")],
            db_path=str(tmp_path / "localfiles.db"),
            artwork_path=str(tmp_path / "artwork"),
        )
        req_q, resp_q = multiprocessing.Queue(), multiprocessing.Queue()
        module = LocalFilesInputModule(
            config,
            _TracksDb(
                _track(tmp_path, "track_first", "First"),
                _track(tmp_path, "track_second", "Second"),
            ),
            req_q,
            resp_q,
        )

        module._searcher = RequestChannel(req_q, resp_q, timeout=0.2)
        assert (await module.ai_search("first")).items == []
        reply(resp_q, req_q.get(timeout=5), {"tracks": ["track_first"]})

        def searcher():
            reply(resp_q, req_q.get(timeout=5), {"tracks": ["track_second"]})

        thread = threading.Thread(target=searcher)
        thread.start()
        module._searcher = RequestChannel(req_q, resp_q, timeout=5)
        result = await module.ai_search("second")
        thread.join()

        [card] = result.items
        assert [item.name for item in card.sections] == ["Second"]

    @pytest.mark.asyncio
    async def test_a_closed_searcher_channel_is_no_result(self, tmp_path):
        from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule

        config = _make_config(
            music_folders=[str(tmp_path / "music")],
            db_path=str(tmp_path / "localfiles.db"),
            artwork_path=str(tmp_path / "artwork"),
        )
        module = LocalFilesInputModule(
            config, _TracksDb(), multiprocessing.Queue(), multiprocessing.Queue()
        )
        module._searcher = Mock(ask=Mock(side_effect=ValueError("Queue is closed")))

        assert (await module.ai_search("piano")).items == []


class TestSearchWorkerKnnLegAvailability:
    """_knn_leg returns [] when IPC queues are not available."""

    @pytest.mark.asyncio
    async def test_knn_leg_returns_empty_without_queues(self):
        worker = _make_worker(None, None)
        result = await worker._knn_leg("test query", 50)
        assert result == []

    @pytest.mark.asyncio
    async def test_knn_leg_returns_empty_without_vec(self):
        req_q = multiprocessing.Queue()
        resp_q = multiprocessing.Queue()
        worker = _make_worker(req_q, resp_q)
        worker.db._vec_available = False
        result = await worker._knn_leg("test query", 50)
        assert result == []


class TestAiSearchIsOneSwitch:
    """The index and the query path cannot be enabled apart."""

    def test_disabled_by_default(self):
        assert _make_config().ai_search.enabled is False

    def test_single_flag_gates_both_halves(self):
        """Both subprocesses read one flag, so neither can run alone —
        vectors nothing reads and a query path with nothing to read were
        both reachable when this was two settings."""
        config = _make_config(ai_search={"enabled": True})
        assert config.ai_search.enabled is True
        assert not hasattr(config, "searcher")
        assert not hasattr(config, "embedder")


class TestSearchQueueWiring:
    """The input module gets search queues only when a searcher reads them."""

    @staticmethod
    def _queues_passed_with(ai_search_enabled: bool, tmp_path):
        """Run setup() with the subprocess/DB machinery stubbed out and
        report the (request, response) queues handed to the input module."""
        from kalinka_plugin_localfiles import module_setup

        config = _make_config(
            ai_search={"enabled": ai_search_enabled},
            db_path=str(tmp_path / "lf.db"),
            artwork_path=str(tmp_path / "art"),
            music_folders=[str(tmp_path)],
        )
        captured = {}

        def fake_module(
            cfg,
            db,
            req=None,
            resp=None,
            storage_source=None,
            scan_control=None,
            librarian_alive=None,
        ):
            assert scan_control is not None
            assert callable(librarian_alive)
            captured["queues"] = (req, resp)
            return Mock()

        with patch.object(module_setup, "LocalFilesInputModule", fake_module), \
                patch.object(module_setup, "init_db", _async_noop), \
                patch.object(module_setup.multiprocessing, "Process", Mock()), \
                patch.object(
                    module_setup.KalinkaPluginLocalFiles,
                    "_evaluate_subfeatures",
                    _async_noop,
                ):
            plugin = module_setup.KalinkaPluginLocalFiles()
            asyncio.run(plugin.setup(Mock(config=config)))
            plugin._log_listener.stop()
        return captured["queues"]

    def test_no_queues_when_ai_search_disabled(self, tmp_path):
        """A request nobody reads would block ai_search() for the full 30 s
        IPC timeout, stalling the whole assembled search response."""
        assert self._queues_passed_with(False, tmp_path) == (None, None)

    def test_queues_wired_when_ai_search_enabled(self, tmp_path):
        assert all(q is not None for q in self._queues_passed_with(True, tmp_path))
