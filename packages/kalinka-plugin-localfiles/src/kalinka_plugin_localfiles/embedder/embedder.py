"""
Embedding worker — CLAP-only pipeline (ONNX Runtime backend).

Runs as a separate long-lived subprocess. Uses a job queue table
(embedding_jobs) for atomic claim/complete/fail semantics.

Stages (handled by this process):
  - 'clap_audio' — CLAP (laion/clap-htsat-unfused) embeds raw audio into
                   a 512-dim space shared with text queries.
  - 'clap_text'  — CLAP encodes track metadata text.

After clap_audio jobs complete, album and artist embeddings are updated
by mean-pooling their tracks' CLAP vectors.

ML dependencies are NOT listed in pyproject.toml — installed on demand.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import queue
import signal
import threading
import time
from typing import Optional

from ..config_model import LocalFilesConfig
from ..embedding_utils import (
    CLAP_MODEL_VERSION,
    decode_embedding,
    encode_embedding,
    normalise,
)
from ..pip_utils import ensure_package
from ..storage import build_resolver, library_roots, storage_failure
from ..worker_utils import configure_worker_logging, set_proc_title, sleep_interruptible
from .embedder_db import AsyncEmbedderDb

logger = logging.getLogger(__name__.split(".")[-1])


# ---------------------------------------------------------------------------
# On-demand pip install (process-local config)
# ---------------------------------------------------------------------------

_PIP_SPECS: dict[str, str] = {
    "onnxruntime": "onnxruntime",
    "soundfile": "soundfile",
    "soxr": "soxr",
    "tokenizers": "tokenizers",
    "numpy": "numpy",
    "sqlite_vec": "sqlite-vec",
}


def _ensure_package(import_name: str) -> bool:
    return ensure_package(import_name, _PIP_SPECS)


# ---------------------------------------------------------------------------
# Numpy — lazy module-level reference
# ---------------------------------------------------------------------------

np = None


def _ensure_numpy() -> bool:
    global np
    if np is not None:
        return True
    if not _ensure_package("numpy"):
        logger.error("numpy unavailable; embedder cannot run")
        return False
    import numpy

    np = numpy
    return True


# ---------------------------------------------------------------------------
# EmbeddingWorker
# ---------------------------------------------------------------------------


class EmbeddingWorker:
    def __init__(self, config: LocalFilesConfig, db: AsyncEmbedderDb):
        self.config = config
        self.db = db
        self.storage = build_resolver(config)
        self._clap = None
        # The two CLAP towers load and unload independently. Text serves
        # search queries and stays resident; audio is indexing-only and
        # idles out (see _maybe_unload_audio).
        self._text_available = False
        self._audio_available = False
        # -inf: monotonic time starts at boot, so 0.0 reads as "just tried".
        self._text_load_attempted_at: float = float("-inf")
        self._audio_load_attempted_at: float = float("-inf")
        self._audio_last_used_at: float = 0.0
        # Roots found not answering this pass, whose tracks wait for the next.
        self._unreachable: dict[str, str] = {}
        # Guards session creation: the text-encode handler (executor thread)
        # and the main work loop can both trigger a load concurrently.
        self._model_lock = threading.Lock()

    # ------------------------------------------------------------------
    # CLAP model loading (text and audio towers load independently)
    # ------------------------------------------------------------------

    def _new_clap(self):
        """Instantiate the wrapper (no sessions yet). Returns it or None."""
        if self._clap is not None:
            return self._clap
        from .clap_onnx import ClapOnnxModel

        self._clap = ClapOnnxModel(
            model_dir=self.config.ai_search.model_dir,
            custom_model_dir=self.config.ai_search.custom_model_dir,
        )
        return self._clap

    def _ensure_text_model(self) -> bool:
        """Load the text encoder (+ tokenizer + VA head). Resident for queries."""
        if self._text_available:
            return True
        if not _ensure_numpy():
            return False
        for pkg in ("onnxruntime", "tokenizers"):
            if not _ensure_package(pkg):
                logger.warning("%s unavailable; CLAP text encoding disabled.", pkg)
                return False

        with self._model_lock:
            if self._text_available:
                return True
            self._text_load_attempted_at = time.monotonic()
            try:
                self._new_clap().load_text()
                self._text_available = self._clap.is_text_loaded
                if self._text_available:
                    # Log the resolved (tilde-expanded) path the loader
                    # actually used, not the raw config string — otherwise a
                    # misconfigured ``~/`` value silently looks like it loaded
                    # from the home directory when it really loaded from a
                    # literal-tilde directory under the server's CWD.
                    logger.info(
                        "CLAP text model loaded from: %s", self._clap._model_dir
                    )
            except Exception as e:
                logger.warning(
                    "CLAP text model loading failed: %s; query encoding disabled", e
                )
        return self._text_available

    def _ensure_audio_model(self) -> bool:
        """Load the audio encoder on demand (indexing only)."""
        if self._audio_available:
            self._audio_last_used_at = time.monotonic()
            return True
        if not _ensure_numpy():
            return False
        for pkg in ("onnxruntime", "soundfile", "soxr", "tokenizers"):
            if not _ensure_package(pkg):
                logger.warning("%s unavailable; CLAP audio embedding disabled.", pkg)
                return False

        with self._model_lock:
            if self._audio_available:
                self._audio_last_used_at = time.monotonic()
                return True
            self._audio_load_attempted_at = time.monotonic()
            try:
                self._new_clap().load_audio()
                self._audio_available = self._clap.is_audio_loaded
                if self._audio_available:
                    self._audio_last_used_at = time.monotonic()
                    logger.info("CLAP audio model loaded (on-demand for indexing)")
            except Exception as e:
                logger.warning(
                    "CLAP audio model loading failed: %s; audio embedding disabled", e
                )
        return self._audio_available

    def _maybe_unload_audio(self, idle_timeout: float) -> None:
        """Release the audio tower after ``idle_timeout`` s with no audio work."""
        if not self._audio_available or idle_timeout <= 0:
            return
        if time.monotonic() - self._audio_last_used_at < idle_timeout:
            return
        with self._model_lock:
            if self._clap is not None:
                self._clap.unload_audio()
            self._audio_available = False
            # Reset the retry gate so the next batch reloads immediately
            # instead of waiting out a poll interval.
            self._audio_load_attempted_at = float("-inf")
        logger.info("CLAP audio model unloaded after %.0fs idle", idle_timeout)

    # ------------------------------------------------------------------
    # Inference helpers
    # ------------------------------------------------------------------

    def _compute_clap_audio(self, file_path: str) -> Optional[bytes]:
        """Compute 512-dim CLAP audio embedding for file_path.

        The file is opened through its storage and the stream handed to the
        encoder, which seeks to the fragments it samples — so a track on a
        share costs the few fragments it reads rather than a copy of the
        whole file.

        @return None when the audio cannot be embedded.
        @raise OSError If the bytes could not be read, which is the storage's
            failure and not the track's.
        """
        if not self._audio_available:
            return None
        try:
            t0 = time.monotonic()
            storage = self.storage.for_path(file_path)
            with storage.open(file_path) as audio:
                vec = self._clap.get_audio_embedding(audio)
            # Keep the idle timer measuring from the last real embed, not
            # just from load, so an active backlog never idles out mid-run.
            self._audio_last_used_at = time.monotonic()
            if vec is None:
                return None
            vec = normalise(vec)
            logger.info("CLAP audio embedding: %.3fs", time.monotonic() - t0)
            return encode_embedding(vec)
        except Exception as e:
            failure = storage_failure(e)
            if failure is not None:
                raise failure
            logger.warning("CLAP embedding failed for %s: %s", file_path, e)
            return None

    def _compute_va(self, blob: bytes) -> Optional[tuple[float, float]]:
        """Map a STORED int8 CLAP embedding -> (valence, arousal) in 1-9.

        From the stored vector (decode -> head), not by re-running the encoder,
        so backfill is cheap; int8 dequant is ~lossless for the head.
        """
        if not self._text_available or self._clap is None or not self._clap.has_va_head:
            return None
        try:
            vec = decode_embedding(blob)
            return self._clap.get_valence_arousal(vec)
        except Exception as e:
            logger.warning("VA compute failed: %s", e)
            return None

    def _encode_query(self, query: str) -> Optional[bytes]:
        """Encode a text query with CLAP. Returns int8 embedding bytes or None."""
        if not self._text_available:
            return None
        try:
            vec = self._clap.get_text_embedding(query)
            if vec is None:
                return None
            vec = normalise(vec)
            return encode_embedding(vec)
        except Exception as e:
            logger.warning("CLAP text encoding failed: %s", e)
            return None

    def _compute_clap_text(
        self, title: str, artist_name: str, album_title: str
    ) -> Optional[bytes]:
        """Encode track metadata text with CLAP text encoder."""
        if not self._text_available:
            return None
        parts = []
        if artist_name:
            parts.append(artist_name)
        if title:
            parts.append(title)
        text = " - ".join(parts)
        if album_title:
            text += f" ({album_title})"
        if not text.strip():
            return None
        return self._encode_query(text)

    # ------------------------------------------------------------------
    # Batch processors
    # ------------------------------------------------------------------

    async def _process_clap_batch(self) -> bool:
        """Embed one claimed batch.

        @return Whether any job in it was settled, done or failed. A batch
            that was only set aside, because its storage is not answering,
            is not progress: the pass ends rather than asking again at once.
            Nothing is claimed while the audio model is not loaded, so a job
            only ever spends an attempt on its own audio.
        """
        if not self._audio_available:
            return False
        cfg = self.config.ai_search
        batch = await self.db.claim_batch("clap_audio", cfg.audio_batch_size)
        if not batch:
            return False

        loop = asyncio.get_running_loop()
        completed_track_ids = []
        deferred = 0
        for job in batch:
            track_id = job["entity_id"]
            file_path = await self.db.get_file_path_for_track(track_id)
            if file_path is None:
                await self.db.fail_job(
                    job["id"], "track not found", cfg.max_job_attempts
                )
                continue
            if self._unreachable and self.storage.root_of(
                file_path, self._unreachable
            ):
                await self.db.defer_jobs([job["id"]])
                deferred += 1
                continue

            try:
                blob = await loop.run_in_executor(
                    None, self._compute_clap_audio, file_path
                )
            except OSError as e:
                root = await self._unreachable_root(file_path)
                if root is not None:
                    await self.db.defer_jobs([job["id"]])
                    deferred += 1
                    continue
                await self.db.fail_job(
                    job["id"], f"unreadable: {e}", cfg.max_job_attempts
                )
                continue
            if blob is None:
                await self.db.fail_job(
                    job["id"], "audio could not be embedded", cfg.max_job_attempts
                )
                continue

            try:
                await self.db.complete_clap_job(
                    job["id"], track_id, blob, job["model_version"]
                )
                completed_track_ids.append(track_id)
            except Exception as e:
                await self.db.fail_job(job["id"], str(e), cfg.max_job_attempts)

        if completed_track_ids:
            logger.info(
                "CLAP embeddings written for %d tracks", len(completed_track_ids)
            )
            # Pre-verdict tracks pool later, off their enrichment-gated text job.
            settled = await self.db.filter_enriched_tracks(completed_track_ids)
            if settled:
                await self._update_aggregate_embeddings(settled)
        return deferred < len(batch)

    async def _unreachable_root(self, file_path: str) -> Optional[str]:
        """The configured folder holding ``file_path`` if it is not answering
        right now, remembered for the rest of this pass; None if it answers.

        What tells a track that cannot be read from a NAS that is asleep or
        switched off. The one fails its job; the other must not, or a Pi
        embedding a large library for days gives up on every track the
        outage touched.
        """
        roots = await asyncio.to_thread(library_roots, self.config, self.storage)
        root = self.storage.root_of(file_path, roots)
        if root is None:
            return None
        status = await self.storage.for_path(root).probe_root(root)
        if status.available:
            return None
        logger.warning(
            "%s is not answering (%s); its tracks wait for the next pass",
            root,
            status.reason,
        )
        self._unreachable[root] = status.reason
        return root

    async def _process_va_backfill(self) -> bool:
        """Fill mood (V,A) for embedded tracks that lack it, from the stored int8
        blob (no audio re-embedding; covers new and pre-existing tracks).

        Returns True only on progress (>=1 row written) so the caller's drain
        loop terminates even if a whole batch fails, instead of re-selecting the
        same NULL rows forever.
        """
        if not self._text_available or self._clap is None or not self._clap.has_va_head:
            return False
        cfg = self.config.ai_search.mood
        batch = await self.db.get_tracks_needing_va(cfg.backfill_batch)
        if not batch:
            return False
        updates: list[tuple[str, float, float]] = []
        for track_id, blob in batch:
            va = self._compute_va(blob)
            if va is not None:
                updates.append((track_id, va[0], va[1]))
        if not updates:
            logger.warning("VA backfill: %d tracks but none computed", len(batch))
            return False
        await self.db.store_mood_va(updates)
        logger.info("Mood (V,A) written for %d tracks", len(updates))
        return True

    async def _update_aggregate_embeddings(self, track_ids: list[str]) -> None:
        """Mean-pool CLAP embeddings for affected albums and artists."""
        if not _ensure_numpy():
            return

        album_ids: set[str] = set()
        artist_ids: set[str] = set()
        for tid in track_ids:
            aid = await self.db.get_album_id_for_track(tid)
            if aid:
                album_ids.add(aid)
            arid = await self.db.get_artist_id_for_track(tid)
            if arid:
                artist_ids.add(arid)

        # The sentinels are not real entities — mean-pooling unrelated tracks
        # into a single "Unknown Album"/"Unknown Artist" vector would pollute
        # album/artist search. Track-level embeddings still cover these tracks.
        album_ids.discard("unknown_album")
        artist_ids.discard("unknown_artist")

        for album_id in album_ids:
            blobs = await self.db.get_track_embeddings_for_album(album_id)
            if not blobs:
                continue
            try:
                vecs = [decode_embedding(b) for b in blobs]
                mean_vec = normalise(np.mean(vecs, axis=0))
                await self.db.update_album_embedding(
                    album_id, encode_embedding(mean_vec)
                )
            except Exception as e:
                logger.warning(
                    "Album embedding aggregation failed for %s: %s", album_id, e
                )

        for artist_id in artist_ids:
            blobs = await self.db.get_track_embeddings_for_artist(artist_id)
            if not blobs:
                continue
            try:
                vecs = [decode_embedding(b) for b in blobs]
                mean_vec = normalise(np.mean(vecs, axis=0))
                await self.db.update_artist_embedding(
                    artist_id, encode_embedding(mean_vec)
                )
            except Exception as e:
                logger.warning(
                    "Artist embedding aggregation failed for %s: %s", artist_id, e
                )

    async def _process_clap_text_batch(self) -> bool:
        """Process clap_text jobs: embed track metadata text with CLAP.

        Nothing is claimed while the text model is not loaded.
        """
        if not self._text_available:
            return False
        cfg = self.config.ai_search
        batch = await self.db.claim_batch("clap_text", cfg.audio_batch_size)
        if not batch:
            return False

        loop = asyncio.get_running_loop()
        completed_track_ids = []
        for job in batch:
            track_id = job["entity_id"]
            meta = await self.db.get_track_metadata_for_embedding(track_id)
            if meta is None:
                await self.db.fail_job(
                    job["id"], "track metadata not found", cfg.max_job_attempts
                )
                continue

            blob = await loop.run_in_executor(
                None,
                self._compute_clap_text,
                meta["title"],
                meta["artist_name"],
                meta["album_title"],
            )
            if blob is None:
                await self.db.fail_job(
                    job["id"], "clap text returned None", cfg.max_job_attempts
                )
                continue

            try:
                await self.db.complete_clap_text_job(
                    job["id"], track_id, blob, job["model_version"]
                )
                completed_track_ids.append(track_id)
            except Exception as e:
                await self.db.fail_job(job["id"], str(e), cfg.max_job_attempts)

        if completed_track_ids:
            logger.info(
                "CLAP text embeddings written for %d tracks", len(completed_track_ids)
            )
            await self._update_aggregate_text_embeddings(completed_track_ids)
            # The audio-aggregate refresh for tracks embedded pre-verdict
            # (and for snapshot-restored blobs) rides this completion.
            await self._update_aggregate_embeddings(completed_track_ids)
        return True

    async def _update_aggregate_text_embeddings(self, track_ids: list[str]) -> None:
        """Embed album/artist metadata text directly (not mean-pooled)."""
        if not self._text_available:
            return

        album_ids: set[str] = set()
        artist_ids: set[str] = set()
        for tid in track_ids:
            aid = await self.db.get_album_id_for_track(tid)
            if aid:
                album_ids.add(aid)
            arid = await self.db.get_artist_id_for_track(tid)
            if arid:
                artist_ids.add(arid)

        # Skip the sentinels — "Unknown Album"/"Unknown Artist" are placeholder
        # rows, not searchable entities.
        album_ids.discard("unknown_album")
        artist_ids.discard("unknown_artist")

        for album_id in album_ids:
            meta = await self.db.get_album_metadata_for_text_embedding(album_id)
            if meta is None:
                continue
            text = meta["title"]
            if meta["artist_name"]:
                text += f" by {meta['artist_name']}"
            if not text.strip():
                continue
            blob = self._encode_query(text)
            if blob:
                try:
                    await self.db.update_album_text_embedding(album_id, blob)
                except Exception as e:
                    logger.warning(
                        "Album text embedding failed for %s: %s", album_id, e
                    )

        for artist_id in artist_ids:
            name = await self.db.get_artist_name(artist_id)
            if not name:
                continue
            blob = self._encode_query(name)
            if blob:
                try:
                    await self.db.update_artist_text_embedding(artist_id, blob)
                except Exception as e:
                    logger.warning(
                        "Artist text embedding failed for %s: %s", artist_id, e
                    )

    # ------------------------------------------------------------------
    # Text-encode service (IPC for searcher KNN queries)
    # ------------------------------------------------------------------

    async def _run_text_encode_handler(
        self,
        request_queue: multiprocessing.Queue,
        response_queue: multiprocessing.Queue,
        shutdown_event: asyncio.Event,
    ) -> None:
        """Poll for text-encode requests from the searcher and return CLAP vectors."""
        loop = asyncio.get_running_loop()
        while not shutdown_event.is_set():
            try:
                req = await loop.run_in_executor(
                    None, lambda: request_queue.get(timeout=1.0)
                )
            except queue.Empty:
                continue
            except Exception:
                await asyncio.sleep(0.1)
                continue

            query = req.get("query", "")
            try:
                # Text tower is normally pre-loaded at startup; this is a
                # cheap no-op then, and a safety net if eager load failed.
                await loop.run_in_executor(None, self._ensure_text_model)
                blob = await loop.run_in_executor(None, self._encode_query, query)
                response_queue.put({"blob": blob})
            except Exception as e:
                logger.warning("Text-encode handler error: %s", e)
                response_queue.put({"blob": None})

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(
        self,
        shutdown_event: asyncio.Event,
        nudge_queue: Optional[multiprocessing.Queue] = None,
        text_encode_request_queue: Optional[multiprocessing.Queue] = None,
        text_encode_response_queue: Optional[multiprocessing.Queue] = None,
    ):
        cfg = self.config.ai_search
        poll = cfg.poll_interval_seconds
        audio_idle_timeout = cfg.audio_model_idle_timeout_seconds
        loop = asyncio.get_running_loop()

        logger.info("EmbeddingWorker started (CLAP-only pipeline)")

        await self.db._check_vec_available()
        await self.db.recover_stale_jobs()
        await self.db.requeue_ambiguous_audio_failures(CLAP_MODEL_VERSION)
        # Heal the KNN index from the blob column: rows can be missing after
        # a snapshot restore or an earlier failed upsert.
        await self.db.backfill_missing_vec_rows()

        # Start text-encode handler for searcher KNN queries (always runs)
        encode_task = None
        if text_encode_request_queue and text_encode_response_queue:
            encode_task = asyncio.create_task(
                self._run_text_encode_handler(
                    text_encode_request_queue,
                    text_encode_response_queue,
                    shutdown_event,
                )
            )
            logger.info("Text-encode handler started (serving searcher KNN queries)")

        # Pre-load the text tower now (in a thread, so we don't block the event
        # loop) so the first user search doesn't pay a ~480 MB model load — the
        # old "first query after idle times out" failure. The audio tower stays
        # unloaded until there's something to index.
        logger.info("Pre-loading CLAP text model")
        await loop.run_in_executor(None, self._ensure_text_model)

        # Wait for the first nudge or poll cycle before loading models.
        # On a fresh restart with an already-indexed library there's
        # nothing to nudge with, so this sleep is the full poll_interval
        # — log the duration explicitly so "did the embedder die?" can be
        # answered from the log alone instead of by ps + strace.
        logger.info(
            "Embedder ready; sleeping %ds before first work cycle "
            "(wakes early on indexer nudge)",
            poll,
        )
        await sleep_interruptible(poll, shutdown_event, nudge_queue, "Embedder")
        logger.info("Embedder waking — starting first work cycle")

        while not shutdown_event.is_set():
            # Re-attach snapshotted audio embeddings from a library rebuild
            # before scheduling, so those tracks never enter the audio queue.
            if await self.db.restore_snapshot(CLAP_MODEL_VERSION):
                await self.db.backfill_missing_vec_rows()

            await self.db.schedule_new_jobs(CLAP_MODEL_VERSION)
            await self.db.resume_deferred_jobs()
            self._unreachable.clear()

            did_work = False
            retry_gap = poll

            # Process CLAP audio — loads the audio tower on demand.
            while await self.db.has_pending_jobs("clap_audio"):
                if time.monotonic() - self._audio_load_attempted_at >= retry_gap:
                    await loop.run_in_executor(None, self._ensure_audio_model)

                try:
                    batch_processed = await self._process_clap_batch()
                    if batch_processed:
                        did_work = True
                    else:
                        break
                except Exception:
                    logger.exception("Unexpected error in CLAP batch; will retry")
                    break

            # Process CLAP text (metadata) embeddings — text tower only.
            while await self.db.has_pending_jobs("clap_text"):
                if time.monotonic() - self._text_load_attempted_at >= retry_gap:
                    await loop.run_in_executor(None, self._ensure_text_model)

                try:
                    batch_processed = await self._process_clap_text_batch()
                    if batch_processed:
                        did_work = True
                    else:
                        break
                except Exception:
                    logger.exception(
                        "Unexpected error in CLAP text batch; will retry"
                    )
                    break

            # Backfill mood (V,A) for embedded tracks that lack it — cheap (one
            # tiny matmul per track from the stored vector). Uses the VA head,
            # which lives in the text tower, so it needs no audio session.
            if self.config.ai_search.mood.enabled:
                if time.monotonic() - self._text_load_attempted_at >= retry_gap:
                    await loop.run_in_executor(None, self._ensure_text_model)
                try:
                    while await self._process_va_backfill():
                        did_work = True
                except Exception:
                    logger.exception("Unexpected error in VA backfill; will retry")

            if did_work:
                continue

            # The text tower stays resident for the lifetime of the process —
            # it serves the searcher's query-encode IPC, and unloading it made
            # the first post-idle search time out on a ~480 MB reload. The
            # audio tower (~272 MB), needed only for the indexing above, idles
            # out here once there's nothing left to embed.
            #
            # DEBUG line below fires every poll cycle on an idle library
            # (default poll=300s, so ~288 lines/day per process). Real
            # work is already announced by "CLAP embeddings written" /
            # "CLAP text embedded".
            self._maybe_unload_audio(audio_idle_timeout)
            logger.debug("No pending embedding work; sleeping %ds", poll)
            await sleep_interruptible(poll, shutdown_event, nudge_queue, "Embedder")

        # Clean shutdown
        if encode_task:
            encode_task.cancel()
            try:
                await encode_task
            except asyncio.CancelledError:
                pass

        logger.info("EmbeddingWorker shutting down")


# ---------------------------------------------------------------------------
# Process entry points
# ---------------------------------------------------------------------------

_shutdown_event: Optional[asyncio.Event] = None


async def async_main(
    config: LocalFilesConfig,
    nudge_queue: Optional[multiprocessing.Queue] = None,
    text_encode_request_queue: Optional[multiprocessing.Queue] = None,
    text_encode_response_queue: Optional[multiprocessing.Queue] = None,
):
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    db = AsyncEmbedderDb(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown_event.set)

    worker = EmbeddingWorker(config, db)
    try:
        await worker.run(
            _shutdown_event,
            nudge_queue,
            text_encode_request_queue,
            text_encode_response_queue,
        )
    except asyncio.CancelledError:
        logger.info("Embedder task cancelled")
    except Exception:
        logger.exception("Embedder crashed")


def main(
    config: LocalFilesConfig,
    logger_queue: multiprocessing.Queue,
    nudge_queue: Optional[multiprocessing.Queue] = None,
    text_encode_request_queue: Optional[multiprocessing.Queue] = None,
    text_encode_response_queue: Optional[multiprocessing.Queue] = None,
):
    """Entry point for the embedder subprocess."""
    set_proc_title("kal-embedder")

    configure_worker_logging(logger_queue)

    asyncio.run(
        async_main(
            config, nudge_queue, text_encode_request_queue, text_encode_response_queue
        )
    )
