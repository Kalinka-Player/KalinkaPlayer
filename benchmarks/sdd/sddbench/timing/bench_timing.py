"""Timing wrappers for the shipped pipeline, installed from outside it.

The benchmark may not change what it measures, so nothing here edits the
server: an import hook wraps a named set of functions as their modules load,
each wrapper timing the call and logging one ``BENCH`` line. Behaviour is
untouched — every wrapper returns exactly what it wrapped, and a wrapper that
raises is a wrapper that re-raises.

One wrapper is not a timing wrapper: the KNN legs also log the distances they
returned, because the search endpoint publishes rank order and no score, and a
results file without scores would lose the only relevance signal the ranking
has. It reads what the call already returned and changes nothing.

Active only when KALINKA_BENCH_TIMING=1 is in the environment, which is what
the benchmark's server launch sets and nothing else does.
"""

from __future__ import annotations

import functools
import importlib.abc
import importlib.util
import inspect
import logging
import sys
import threading
import time

logger = logging.getLogger("bench")

#: Per-thread accumulators, so a wrapper inside another wrapper's call can
#: report its share (decode and inference inside one embedding).
_local = threading.local()


def _bucket(name: str) -> float:
    return getattr(_local, name, 0.0)


def _add(name: str, seconds: float) -> None:
    setattr(_local, name, _bucket(name) + seconds)


def _reset(*names: str) -> None:
    for name in names:
        setattr(_local, name, 0.0)


def emit(event: str, **fields) -> None:
    parts = " ".join(
        f"{key}={value:.4f}" if isinstance(value, float) else f"{key}={value}"
        for key, value in fields.items()
    )
    logger.info("BENCH %s %s", event, parts)


def _timed(func, event: str, bucket: str | None = None, fields=None):
    """Wrap ``func`` so its wall time is logged, and optionally accumulated."""

    def report(elapsed: float, result, args, kwargs) -> None:
        if bucket:
            _add(bucket, elapsed)
        payload = {"t": elapsed}
        if fields:
            payload.update(fields(result, args, kwargs))
        emit(event, **payload)

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.perf_counter()
            result = await func(*args, **kwargs)
            report(time.perf_counter() - start, result, args, kwargs)
            return result

        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        report(time.perf_counter() - start, result, args, kwargs)
        return result

    return wrapper


def _accumulate(func, bucket: str):
    """Wrap ``func`` so its wall time lands in a bucket, logging nothing: for
    calls too frequent to log one line each (fragment reads, ONNX runs)."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            _add(bucket, time.perf_counter() - start)

    return wrapper


# ---------------------------------------------------------------------------
# Per-module patches
# ---------------------------------------------------------------------------


def _patch_clap_onnx(module) -> None:
    module._read_fragment = _accumulate(module._read_fragment, "decode_s")

    model = module.ClapOnnxModel
    original_embed = model.get_audio_embedding

    @functools.wraps(original_embed)
    def get_audio_embedding(self, audio):
        _reset("decode_s", "infer_s")
        start = time.perf_counter()
        result = original_embed(self, audio)
        total = time.perf_counter() - start
        decode, infer = _bucket("decode_s"), _bucket("infer_s")
        emit(
            "embed_track",
            t=total,
            decode=decode,
            infer=infer,
            other=max(total - decode - infer, 0.0),
            ok=int(result is not None),
        )
        return result

    model.get_audio_embedding = get_audio_embedding
    model.get_text_embedding = _timed(model.get_text_embedding, "encode_text")
    model.get_valence_arousal = _timed(model.get_valence_arousal, "va_head")

    # An ONNX session exists only once its loader has run, so the loader is
    # what wraps it. Separate buckets per tower: the text tower serves query
    # encoding on the same thread pool the audio tower embeds on, and one
    # shared bucket would charge a query's encode to a track's embedding.
    for loader_name, attribute, bucket in (
        ("load_audio", "_audio_session", "infer_s"),
        ("load_text", "_text_session", "text_infer_s"),
    ):
        timed_loader = _timed(getattr(model, loader_name), f"model_{loader_name}")

        def make(loader, attribute=attribute, bucket=bucket):
            @functools.wraps(loader)
            def load(self):
                loader(self)
                session = getattr(self, attribute, None)
                if session is not None and not getattr(session, "_bench", False):
                    session.run = _accumulate(session.run, bucket)
                    session._bench = True

            return load

        setattr(model, loader_name, make(timed_loader))


def _patch_embedder_db(module) -> None:
    db = module.AsyncEmbedderDb
    db.complete_clap_job = _timed(db.complete_clap_job, "db_write_embedding")
    db.store_mood_va = _timed(
        db.store_mood_va, "db_write_mood",
        fields=lambda result, args, kwargs: {"n": len(args[1])},
    )
    db.claim_batch = _timed(db.claim_batch, "db_claim_batch")
    db.schedule_new_jobs = _timed(db.schedule_new_jobs, "db_schedule_jobs")


def _patch_embedder(module) -> None:
    worker = module.EmbeddingWorker
    worker._process_clap_batch = _timed(worker._process_clap_batch, "embed_batch")
    worker._process_va_backfill = _timed(worker._process_va_backfill, "va_backfill")


def _patch_indexer(module) -> None:
    indexer = module.FileIndexer
    indexer._audio_files_by_folder = _timed(
        indexer._audio_files_by_folder, "scan_listing"
    )
    indexer._index_files = _timed(indexer._index_files, "scan_index_files")
    indexer.cleanup_stale_tracks = _timed(
        indexer.cleanup_stale_tracks, "scan_cleanup"
    )


def _patch_searcher(module) -> None:
    worker = module.SearchWorker
    worker._encode_query_blob = _timed(worker._encode_query_blob, "query_encode")
    worker._knn_leg = _timed(
        worker._knn_leg, "query_knn",
        fields=lambda result, args, kwargs: {"hits": len(result)},
    )
    worker._query_to_va = _timed(worker._query_to_va, "query_mood_map")
    worker._do_search = _timed(
        worker._do_search, "query_total",
        fields=lambda result, args, kwargs: {
            "q": _tag(args[1]), "n": len(result.get("tracks", []))
        },
    )


def _patch_searcher_db(module) -> None:
    db = module.AsyncSearcherDb

    def distances(result, args, kwargs):
        pairs = ";".join(f"{hit['track_id']}:{hit['distance']:.6f}" for hit in result)
        return {"hits": len(result), "dist": pairs or "-"}

    db.knn_search_audio = _timed(db.knn_search_audio, "knn_audio", fields=distances)
    db.knn_search_mood = _timed(db.knn_search_mood, "knn_mood", fields=distances)


def _tag(query: str) -> str:
    """A stable short id for a query string, so a log line names which query it
    timed without carrying the caption into the log."""
    import hashlib

    return hashlib.sha1(query.encode("utf-8", "replace")).hexdigest()[:12]


#: Every shipped symbol this module wraps, and why — the report prints this
#: list verbatim, so an added wrapper cannot go unmentioned.
TOUCHES = [
    ("clap_onnx._read_fragment", "fragment decode/resample time", "timing"),
    ("clap_onnx.ClapOnnxModel.get_audio_embedding", "per-track embedding split", "timing"),
    ("clap_onnx.ClapOnnxModel.load_audio", "audio tower load time", "timing"),
    ("clap_onnx.ClapOnnxModel.load_text", "text tower load time", "timing"),
    ("clap_onnx.ClapOnnxModel.get_text_embedding", "text encode time", "timing"),
    ("clap_onnx.ClapOnnxModel.get_valence_arousal", "mood head time", "timing"),
    ("onnxruntime session.run (audio, text)", "inference time inside an embedding", "timing"),
    ("embedder_db.AsyncEmbedderDb.complete_clap_job", "vector write time", "timing"),
    ("embedder_db.AsyncEmbedderDb.store_mood_va", "mood write time", "timing"),
    ("embedder_db.AsyncEmbedderDb.claim_batch", "job claim time", "timing"),
    ("embedder_db.AsyncEmbedderDb.schedule_new_jobs", "job scheduling time", "timing"),
    ("embedder.EmbeddingWorker._process_clap_batch", "batch time", "timing"),
    ("embedder.EmbeddingWorker._process_va_backfill", "mood backfill time", "timing"),
    ("indexer.FileIndexer._audio_files_by_folder", "scan listing time", "timing"),
    ("indexer.FileIndexer._index_files", "scan per-file time", "timing"),
    ("indexer.FileIndexer.cleanup_stale_tracks", "scan cleanup time", "timing"),
    ("searcher.SearchWorker._encode_query_blob", "query encode time", "timing"),
    ("searcher.SearchWorker._knn_leg", "KNN leg time", "timing"),
    ("searcher.SearchWorker._query_to_va", "mood mapping time", "timing"),
    ("searcher.SearchWorker._do_search", "whole-query time", "timing"),
    ("searcher_db.AsyncSearcherDb.knn_search_audio",
     "KNN time AND the distances it returned", "timing + observation"),
    ("searcher_db.AsyncSearcherDb.knn_search_mood",
     "mood KNN time AND the distances it returned", "timing + observation"),
]


_PATCHES = {
    "kalinka_plugin_localfiles.embedder.clap_onnx": _patch_clap_onnx,
    "kalinka_plugin_localfiles.embedder.embedder_db": _patch_embedder_db,
    "kalinka_plugin_localfiles.embedder.embedder": _patch_embedder,
    "kalinka_plugin_localfiles.indexer.indexer": _patch_indexer,
    "kalinka_plugin_localfiles.searcher.searcher": _patch_searcher,
    "kalinka_plugin_localfiles.searcher.searcher_db": _patch_searcher_db,
}


# ---------------------------------------------------------------------------
# Import hook
# ---------------------------------------------------------------------------


class _PatchingLoader(importlib.abc.Loader):
    """Delegates to the real loader, then patches the module it produced."""

    def __init__(self, loader, patch):
        self._loader = loader
        self._patch = patch

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        try:
            self._patch(module)
            logger.info("BENCH patched %s", module.__name__)
        except Exception:
            logger.exception("BENCH could not patch %s", module.__name__)


class _PatchingFinder(importlib.abc.MetaPathFinder):
    def __init__(self, patches):
        self._patches = patches
        self._resolving: set[str] = set()

    def find_spec(self, fullname, path=None, target=None):
        patch = self._patches.get(fullname)
        if patch is None or fullname in self._resolving:
            return None
        self._resolving.add(fullname)
        try:
            spec = importlib.util.find_spec(fullname)
        except Exception:
            return None
        finally:
            self._resolving.discard(fullname)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PatchingLoader(spec.loader, patch)
        return spec


def install() -> None:
    """Arm the hook. Modules already imported are patched in place."""
    if any(isinstance(finder, _PatchingFinder) for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _PatchingFinder(_PATCHES))
    for name, patch in _PATCHES.items():
        module = sys.modules.get(name)
        if module is not None:
            patch(module)
