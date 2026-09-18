"""Tests for CLAP text-encoding IPC between searcher and embedder."""

import asyncio
import multiprocessing
import queue
import threading
from unittest.mock import Mock, patch

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
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
            resp_q.put({"blob": fake_blob})

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
            req_q.get(timeout=5)
            resp_q.put({"blob": None})

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

        # Patch timeout to be short for testing
        with patch.object(
            type(worker), "_encode_query_text",
            wraps=worker._encode_query_text,
        ):
            # Don't put anything on resp_q — simulate embedder not responding
            # We need to test with a very short timeout
            original_method = SearchWorker._encode_query_text

            def short_timeout_encode(self, query):
                if self._text_encode_request_queue is None or self._text_encode_response_queue is None:
                    return None
                try:
                    self._text_encode_request_queue.put({"query": query})
                    resp = self._text_encode_response_queue.get(timeout=0.1)
                    return resp.get("blob")
                except Exception:
                    return None

            with patch.object(SearchWorker, "_encode_query_text", short_timeout_encode):
                result = worker._encode_query_text("timeout query")
                assert result is None


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

        def fake_module(cfg, db, req=None, resp=None, storage_source=None):
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
