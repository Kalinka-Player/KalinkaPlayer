"""The shim: it must measure the pipeline without changing it, and it must
still find the functions it claims to wrap."""

import asyncio
import importlib
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sddbench" / "timing"))

import bench_timing  # noqa: E402


def test_a_wrapper_returns_what_it_wrapped(caplog):
    def add(a, b):
        return a + b

    wrapped = bench_timing._timed(add, "add")
    with caplog.at_level(logging.INFO, logger="bench"):
        assert wrapped(2, 3) == 5
    assert "BENCH add t=" in caplog.text


def test_a_wrapper_re_raises(caplog):
    def boom():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        bench_timing._timed(boom, "boom")()


def test_an_async_wrapper_stays_awaitable(caplog):
    async def fetch():
        await asyncio.sleep(0)
        return "value"

    wrapped = bench_timing._timed(fetch, "fetch")
    with caplog.at_level(logging.INFO, logger="bench"):
        assert asyncio.run(wrapped()) == "value"
    assert "BENCH fetch t=" in caplog.text


def test_accumulators_add_up_and_reset():
    def work():
        return 1

    wrapped = bench_timing._accumulate(work, "bucket_s")
    bench_timing._reset("bucket_s")
    wrapped()
    wrapped()
    assert bench_timing._bucket("bucket_s") > 0
    bench_timing._reset("bucket_s")
    assert bench_timing._bucket("bucket_s") == 0.0


def test_fields_are_logged_alongside_the_duration(caplog):
    def hits():
        return [1, 2, 3]

    wrapped = bench_timing._timed(
        hits, "hits", fields=lambda result, args, kwargs: {"n": len(result)}
    )
    with caplog.at_level(logging.INFO, logger="bench"):
        wrapped()
    assert "n=3" in caplog.text


def test_every_symbol_the_report_lists_is_actually_patched():
    """The shim names what it wraps, and the report prints that list. A rename
    in the plugin must fail here rather than quietly lose a timing."""
    pytest.importorskip("kalinka_plugin_localfiles")
    bench_timing.install()
    try:
        for module_name in bench_timing._PATCHES:
            importlib.import_module(module_name)
        from kalinka_plugin_localfiles.embedder import clap_onnx, embedder, embedder_db
        from kalinka_plugin_localfiles.indexer import indexer
        from kalinka_plugin_localfiles.searcher import searcher, searcher_db

        wrapped = {
            "clap_onnx._read_fragment": clap_onnx._read_fragment,
            "clap_onnx.ClapOnnxModel.get_audio_embedding": clap_onnx.ClapOnnxModel.get_audio_embedding,
            "clap_onnx.ClapOnnxModel.load_audio": clap_onnx.ClapOnnxModel.load_audio,
            "clap_onnx.ClapOnnxModel.load_text": clap_onnx.ClapOnnxModel.load_text,
            "clap_onnx.ClapOnnxModel.get_text_embedding": clap_onnx.ClapOnnxModel.get_text_embedding,
            "clap_onnx.ClapOnnxModel.get_valence_arousal": clap_onnx.ClapOnnxModel.get_valence_arousal,
            "embedder_db.AsyncEmbedderDb.complete_clap_job": embedder_db.AsyncEmbedderDb.complete_clap_job,
            "embedder_db.AsyncEmbedderDb.store_mood_va": embedder_db.AsyncEmbedderDb.store_mood_va,
            "embedder_db.AsyncEmbedderDb.claim_batch": embedder_db.AsyncEmbedderDb.claim_batch,
            "embedder_db.AsyncEmbedderDb.schedule_new_jobs": embedder_db.AsyncEmbedderDb.schedule_new_jobs,
            "embedder.EmbeddingWorker._process_clap_batch": embedder.EmbeddingWorker._process_clap_batch,
            "embedder.EmbeddingWorker._process_va_backfill": embedder.EmbeddingWorker._process_va_backfill,
            "indexer.FileIndexer._audio_files_by_folder": indexer.FileIndexer._audio_files_by_folder,
            "indexer.FileIndexer._index_files": indexer.FileIndexer._index_files,
            "indexer.FileIndexer.cleanup_stale_tracks": indexer.FileIndexer.cleanup_stale_tracks,
            "searcher.SearchWorker._encode_query_blob": searcher.SearchWorker._encode_query_blob,
            "searcher.SearchWorker._knn_leg": searcher.SearchWorker._knn_leg,
            "searcher.SearchWorker._query_to_va": searcher.SearchWorker._query_to_va,
            "searcher.SearchWorker._do_search": searcher.SearchWorker._do_search,
            "searcher_db.AsyncSearcherDb.knn_search_audio": searcher_db.AsyncSearcherDb.knn_search_audio,
            "searcher_db.AsyncSearcherDb.knn_search_mood": searcher_db.AsyncSearcherDb.knn_search_mood,
        }
        listed = {symbol for symbol, _, _ in bench_timing.TOUCHES}
        assert listed - set(wrapped) == {"onnxruntime session.run (audio, text)"}
        unwrapped = [name for name, func in wrapped.items() if not hasattr(func, "__wrapped__")]
        assert not unwrapped
    finally:
        bench_timing.uninstall()


def test_uninstalling_puts_the_pipeline_back():
    pytest.importorskip("kalinka_plugin_localfiles")
    from kalinka_plugin_localfiles.searcher import searcher

    original = searcher.SearchWorker._do_search
    bench_timing.install()
    try:
        assert searcher.SearchWorker._do_search is not original
    finally:
        bench_timing.uninstall()
    assert searcher.SearchWorker._do_search is original


def test_installing_twice_does_not_wrap_anything_twice():
    """A second install over a module already patched would log every call
    twice and double every bucket the report adds up."""
    pytest.importorskip("kalinka_plugin_localfiles")
    from kalinka_plugin_localfiles.searcher import searcher

    bench_timing.install()
    try:
        wrapped = searcher.SearchWorker._do_search
        bench_timing.install()
        assert searcher.SearchWorker._do_search is wrapped
    finally:
        bench_timing.uninstall()
