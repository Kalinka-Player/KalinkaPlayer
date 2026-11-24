"""
Search worker — CLAP semantic search + mood (valence/arousal) ranking.

Runs as a separate long-lived subprocess (same pattern as embedder.py).

Online work:
  - Receives search queries via IPC queues.
  - Encodes the query with CLAP (text→audio) via the embedder process.
  - Retrieves candidates by CLAP KNN, augmented by a mood (V/A) leg.
  - Ranks by CLAP similarity blended with mood proximity.

numpy is NOT listed in pyproject.toml — installed on demand for the mood
leg's vector math.
"""

from __future__ import annotations

import asyncio
import logging
import math
import multiprocessing
import os
import queue
import re
import signal
import time
from typing import Optional

from ..config_model import LocalFilesConfig
from ..embedding_utils import decode_embedding
from ..ipc import RequestChannel, reply
from ..pip_utils import ensure_package
from ..worker_utils import configure_worker_logging, set_proc_title
from .query_terms import decades_in, year_spans
from .recommender import Recommender
from .searcher_db import AsyncSearcherDb

logger = logging.getLogger(__name__.split(".")[-1])


# ---------------------------------------------------------------------------
# On-demand pip install (process-local config)
# ---------------------------------------------------------------------------

_PIP_SPECS: dict[str, str] = {
    "numpy": "numpy",
}


def _ensure_package(import_name: str) -> bool:
    return ensure_package(import_name, _PIP_SPECS, {})


# ---------------------------------------------------------------------------
# Numpy — lazy module-level reference
# ---------------------------------------------------------------------------

np = None


def _ensure_numpy() -> bool:
    global np
    if np is not None:
        return True
    if not _ensure_package("numpy"):
        logger.error("numpy unavailable; searcher mood ranking cannot run")
        return False
    import numpy

    np = numpy
    return True


# Filler words ignored when scoring a query's mood "purity" (_query_to_va).
# Genre/instrument nouns are deliberately excluded — that's content for CLAP.
_FILLER_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "from", "into", "my", "me", "i", "you", "your", "we", "us",
    "it", "its", "this", "that", "these", "those", "some", "something",
    "anything", "like", "want", "need", "give", "play", "playing", "song",
    "songs", "music", "track", "tracks", "tune", "tunes", "sound", "sounds",
    "playlist", "vibe", "vibes", "mood", "feeling", "feel", "get", "got", "im",
    "am", "are", "is", "be", "now", "tonight", "today", "day", "night", "time",
    "really", "very", "more", "bit", "little", "kinda", "sorta", "stuff",
})

# ---------------------------------------------------------------------------
# SearchWorker
# ---------------------------------------------------------------------------


class SearchWorker:
    def __init__(
        self,
        config: LocalFilesConfig,
        db: AsyncSearcherDb,
        text_encode_request_queue: Optional[multiprocessing.Queue] = None,
        text_encode_response_queue: Optional[multiprocessing.Queue] = None,
    ):
        self.config = config
        self.db = db
        self._recommender = Recommender(db)
        self._embedder = (
            RequestChannel(
                text_encode_request_queue, text_encode_response_queue, timeout=30.0
            )
            if text_encode_request_queue is not None
            and text_encode_response_queue is not None
            else None
        )
        # Mood index (words, va[M,2], text_emb[M,512]); lazy-loaded, retried.
        self._mood_index: Optional[tuple] = None
        # -inf: monotonic time starts at boot, so 0.0 reads as "just tried".
        self._mood_load_attempted_at: float = float("-inf")

    # ------------------------------------------------------------------
    # CLAP text encoding via IPC (served by embedder process)
    # ------------------------------------------------------------------

    def _encode_query_text(self, query: str) -> Optional[bytes]:
        """Encode a text query with CLAP via IPC to the embedder process."""
        if self._embedder is None:
            return None
        try:
            resp = self._embedder.ask({"query": query})
        except Exception as e:
            logger.warning("CLAP text encoding IPC failed: %s", e)
            return None
        if resp is None:
            logger.warning("CLAP text encoding timed out")
            return None
        return resp.get("blob")

    # ------------------------------------------------------------------
    # Mood (valence/arousal) query mapping
    # ------------------------------------------------------------------

    def _load_mood_index(self) -> Optional[tuple]:
        """Load (words, va, text_emb) from mood_index.npz, downloading if needed.

        Best-effort, retried every ~5 min; None until available (mood ranking
        then degrades to pure CLAP).
        """
        if self._mood_index is not None:
            return self._mood_index
        if time.monotonic() - self._mood_load_attempted_at < 300:
            return None
        self._mood_load_attempted_at = time.monotonic()
        if not _ensure_numpy():
            return None
        try:
            # Reuse the CLAP-artifact downloader (not the tag-model one above).
            from ..embedder.clap_onnx import _ensure_model_file as _ensure_clap
            model_dir = os.path.expanduser(self.config.ai_search.model_dir)
            path = _ensure_clap("mood_index", model_dir)
            if not path:
                return None
            d = np.load(path, allow_pickle=True)
            self._mood_index = (
                [str(w) for w in d["words"]],
                d["va"].astype(np.float32),
                d["text_emb"].astype(np.float32),
            )
            logger.info("Mood index loaded (%d words)", len(self._mood_index[0]))
        except Exception as e:
            logger.warning("Mood index load failed (mood ranking off): %s", e)
            self._mood_index = None
        return self._mood_index

    def _query_to_va(
        self, query: str, query_blob: Optional[bytes]
    ) -> tuple[Optional[tuple[float, float]], float]:
        """Map a query to a target (valence, arousal) + confidence in [0,1].

        Keyword spotting first (a literal mood word -> confidence 1.0), then a
        CLAP-text nearest-neighbour fallback over the mood vocabulary. Returns
        (None, 0.0) for non-mood queries so ranking stays pure CLAP. No LLM.
        """
        idx = self._load_mood_index()
        if idx is None:
            return None, 0.0
        words, va, emb = idx
        if not words:  # empty/corrupt index — no mood mapping possible
            return None, 0.0
        mcfg = self.config.ai_search.mood

        # 1) Keyword spotting — literal mood word(s) present in the query.
        tokens = set(re.findall(r"[a-z]+", query.lower()))
        hits = [i for i, w in enumerate(words) if w in tokens]
        if hits:
            tv = float(np.mean([va[i][0] for i in hits]))
            ta = float(np.mean([va[i][1] for i in hits]))
            # Confidence = mood purity (share of non-filler words that are mood
            # words): pure mood -> 1.0; "melancholic piano" -> 0.5 (CLAP keeps piano).
            matched = {words[i] for i in hits}
            content = [t for t in tokens if len(t) >= 3 and t not in _FILLER_WORDS]
            mood_share = sum(t in matched for t in content) / len(content) \
                if content else 1.0
            return (tv, ta), float(mood_share)

        # 2) CLAP-text nearest-neighbour fallback.
        if not mcfg.nn_fallback or query_blob is None:
            return None, 0.0
        q = decode_embedding(query_blob).astype(np.float32)
        qn = float(np.linalg.norm(q))
        if qn == 0.0:
            return None, 0.0
        sims = emb @ (q / qn)
        order = np.argsort(-sims)[: mcfg.nn_top_k]
        top_cos = float(sims[order[0]])
        if top_cos < mcfg.nn_threshold:
            return None, 0.0  # query isn't mood-like -> pure CLAP
        w = np.clip(sims[order], 0.0, None)
        if w.sum() <= 0:
            return None, 0.0
        tv = float((va[order, 0] * w).sum() / w.sum())
        ta = float((va[order, 1] * w).sum() / w.sum())
        # Confidence rises from 0 at the threshold to 1 at perfect similarity
        # (denominator guarded for nn_threshold == 1.0).
        denom = 1.0 - mcfg.nn_threshold
        conf = (top_cos - mcfg.nn_threshold) / denom if denom > 0 else 1.0
        return (tv, ta), float(min(1.0, max(0.0, conf)))

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    @staticmethod
    def _score_track(knn_norm: float, has_knn_hits: bool = True) -> float:
        """CLAP KNN relevance for one track, normalised to 0-1.

        Mood (valence/arousal) blending is applied by the caller.
        """
        return knn_norm if has_knn_hits else 0.0

    # ------------------------------------------------------------------
    # Search dispatch
    # ------------------------------------------------------------------

    async def _do_search(self, query: str, limit: int) -> dict:
        """Handle one semantic search request, returning ranked track ids.

        This is the CLAP semantic leg only (KNN + mood). BEST MATCH
        (literal/navigational name lookup) and the navigational suppression
        that hides these suggestions for a name query now live in the server,
        which assembles them across all sources from ``search()``.

        A decade the query names ("70s rock") keeps both legs to tracks whose
        album first came out then; a query naming a decade the library has
        nothing from finds nothing.
        """
        cfg = self.config.ai_search

        # Encode the query once (CLAP text via IPC); the blob is reused by the
        # KNN leg and the mood NN fallback so we don't double the IPC round-trip.
        query_blob = await self._encode_query_blob(query)

        decades = decades_in(query)
        within = set(await self.db.tracks_from_years(year_spans(decades))) if decades else None
        if within is not None and not within:
            logger.info("_do_search: no tracks from %s", decades)
            return {"tracks": []}

        knn_hits = await self._knn_leg(query, cfg.knn_candidate_limit, query_blob, within)
        logger.info("_do_search: query=%r KNN=%d hits", query, len(knn_hits))

        has_knn = bool(knn_hits)
        if not knn_hits:
            logger.info("_do_search: no semantic hits")
            return {"tracks": []}

        # KNN score map (normalised 0-1, higher is better).
        dists = [h["distance"] for h in knn_hits]
        min_dist = min(dists)
        max_dist = max(dists)
        dist_range = max_dist - min_dist if max_dist != min_dist else 1.0
        knn_map: dict[str, float] = {
            h["track_id"]: 1.0 - (h["distance"] - min_dist) / dist_range
            for h in knn_hits
        }

        all_track_ids = list(dict.fromkeys(h["track_id"] for h in knn_hits))

        # --- Mood (valence/arousal) leg ---
        # Union tracks closest to the query's target (V,A) with the CLAP
        # candidates, so a pure-mood query isn't limited to CLAP's (near-random)
        # neighbours. Blend weight scales with the query's mood confidence.
        mood_map: dict[str, float] = {}
        mood_weight = 0.0
        if cfg.mood.enabled:
            target_va, mood_conf = self._query_to_va(query, query_blob)
            if target_va is not None and mood_conf > 0.0:
                mood_weight = cfg.mood.weight * mood_conf
                mood_hits = await self._mood_leg(target_va, cfg.mood.candidates, within)
                all_track_ids.extend(h["track_id"] for h in mood_hits)
                all_track_ids = list(dict.fromkeys(all_track_ids))
                va_bulk = await self.db.get_tracks_va_bulk(all_track_ids)
                if va_bulk:
                    d = {tid: math.dist(target_va, va)
                         for tid, va in va_bulk.items()}
                    dmin, dmax = min(d.values()), max(d.values())
                    drange = (dmax - dmin) or 1.0
                    mood_map = {tid: 1.0 - (dist - dmin) / drange
                                for tid, dist in d.items()}
                logger.info(
                    "_do_search: mood target V=%.2f A=%.2f conf=%.2f (+%d cand)",
                    target_va[0], target_va[1], mood_conf, len(mood_hits),
                )

        scored: list[tuple[float, str]] = []
        for tid in all_track_ids:
            score = self._score_track(knn_map.get(tid, 0.0), has_knn_hits=has_knn)
            if mood_weight > 0.0:
                # Adaptive blend: final = (1 - w)*clap + w*mood, w = weight*conf.
                score = (1.0 - mood_weight) * score + mood_weight * mood_map.get(tid, 0.0)
            scored.append((score, tid))

        scored.sort(key=lambda st: -st[0])
        return {"tracks": [tid for _, tid in scored[:limit]]}

    async def _knn_leg(
        self,
        query: str,
        candidate_limit: int,
        blob: Optional[bytes] = None,
        within: Optional[set[str]] = None,
    ) -> list[dict]:
        """CLAP KNN search leg — returns [{track_id, distance}].

        KNN-searches the audio index with the precomputed query ``blob`` (CLAP's
        text→audio is the canonical retrieval direction). ``blob`` is encoded
        once by the caller and shared with the mood fallback; None → empty leg.
        With ``within``, only those tracks are ranked, exactly rather than
        through the index, so a narrow selection still fills the leg.
        """
        if blob is None:
            return []
        if within is not None:
            return await self._ranked_within(blob, candidate_limit, within)
        if not self.db._vec_available:
            return []
        t0 = time.monotonic()
        results = await self.db.knn_search_audio(blob, candidate_limit)
        logger.info(
            "KNN audio search: %d results in %.3fs", len(results), time.monotonic() - t0
        )
        return results

    async def _ranked_within(self, blob: bytes, limit: int, within: set[str]) -> list[dict]:
        if not _ensure_numpy():
            return []
        query = decode_embedding(blob)
        found = await self.db.get_track_vectors(list(within))
        ranked = sorted(
            (float(np.linalg.norm(decode_embedding(v) - query)), t) for t, v in found.items()
        )
        return [{"track_id": t, "distance": d} for d, t in ranked[:limit]]

    async def _mood_leg(
        self, target_va: tuple[float, float], limit: int, within: Optional[set[str]]
    ) -> list[dict]:
        """The tracks nearest ``target_va``, from ``within`` when given."""
        if within is None:
            return await self.db.knn_search_mood(target_va[0], target_va[1], limit)
        va = await self.db.get_tracks_va_bulk(list(within))
        nearest = sorted(va, key=lambda t: math.dist(target_va, va[t]))[:limit]
        return [{"track_id": t, "distance": math.dist(target_va, va[t])} for t in nearest]

    async def _handle(self, req: dict) -> dict:
        """Serve one IPC request: a search unless its ``op`` says otherwise.

        ``continue`` takes ``seeds``, ``avoid``, ``exclude`` and ``count`` and
        answers ``tracks``; ``albums`` takes ``count`` and answers ``albums``.
        """
        op = req.get("op", "search")
        if op == "continue":
            if not _ensure_numpy():
                return {"tracks": []}
            tracks = await self._recommender.next_tracks(
                req.get("seeds", []), req.get("avoid", []), req.get("exclude", []), int(req.get("count", 1))
            )
            return {"tracks": tracks}
        if op == "albums":
            if not _ensure_numpy():
                return {"albums": []}
            return {"albums": await self._recommender.albums_for_you(int(req.get("count", 20)))}
        return await self._do_search(req["query"], req.get("limit", 20))

    async def _encode_query_blob(self, query: str) -> Optional[bytes]:
        """Encode the query once (CLAP text via IPC), with the ASCII/queue
        guards the KNN and mood legs share. Non-ASCII -> None (CLAP's English
        tokenizer noises on Cyrillic/CJK)."""
        if not query.isascii():
            logger.info("CLAP encode skipped: non-ASCII query %r", query)
            return None
        if self._embedder is None:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._encode_query_text, query)

    # ------------------------------------------------------------------
    # Search handler (IPC)
    # ------------------------------------------------------------------

    async def _run_search_handler(
        self,
        search_request_queue: multiprocessing.Queue,
        search_response_queue: multiprocessing.Queue,
        shutdown_event: asyncio.Event,
    ) -> None:
        """Poll the request queue and dispatch searches."""
        loop = asyncio.get_running_loop()
        while not shutdown_event.is_set():
            try:
                req = await loop.run_in_executor(
                    None, lambda: search_request_queue.get(timeout=1.0)
                )
            except queue.Empty:
                continue
            except Exception:
                await asyncio.sleep(0.1)
                continue

            try:
                t0 = time.monotonic()
                result = await self._handle(req)
                logger.info(
                    "%s %r: %d result(s) in %.3fs",
                    req.get("op", "search"),
                    req.get("query", ""),
                    len(result.get("tracks", result.get("albums", []))),
                    time.monotonic() - t0,
                )
            except Exception as e:
                logger.error("Search handler error: %s", e, exc_info=True)
                result = {"tracks": [], "albums": []}

            reply(search_response_queue, req, result)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(
        self,
        shutdown_event: asyncio.Event,
        search_request_queue: multiprocessing.Queue,
        search_response_queue: multiprocessing.Queue,
        nudge_queue: Optional[multiprocessing.Queue] = None,
        embedder_nudge_queue: Optional[multiprocessing.Queue] = None,
    ):
        # nudge_queue / embedder_nudge_queue are retained for call-site
        # compatibility but unused: the searcher has no offline work of its
        # own (CLAP indexing is the embedder's job), so it only serves queries.
        logger.info("SearchWorker started (CLAP KNN + mood ranking)")

        await self.db._check_vec_available()

        search_task = asyncio.create_task(
            self._run_search_handler(
                search_request_queue, search_response_queue, shutdown_event
            )
        )

        logger.info("Searcher ready")

        await shutdown_event.wait()

        # Clean shutdown
        search_task.cancel()
        try:
            await search_task
        except asyncio.CancelledError:
            pass

        logger.info("SearchWorker shutting down")


# ---------------------------------------------------------------------------
# Process entry points
# ---------------------------------------------------------------------------

_shutdown_event: Optional[asyncio.Event] = None


async def async_main(
    config: LocalFilesConfig,
    search_request_queue: multiprocessing.Queue,
    search_response_queue: multiprocessing.Queue,
    nudge_queue: Optional[multiprocessing.Queue] = None,
    embedder_nudge_queue: Optional[multiprocessing.Queue] = None,
    text_encode_request_queue: Optional[multiprocessing.Queue] = None,
    text_encode_response_queue: Optional[multiprocessing.Queue] = None,
) -> None:
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    db = AsyncSearcherDb(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown_event.set)

    worker = SearchWorker(
        config, db, text_encode_request_queue, text_encode_response_queue
    )
    try:
        await worker.run(
            _shutdown_event,
            search_request_queue,
            search_response_queue,
            nudge_queue,
            embedder_nudge_queue,
        )
    except asyncio.CancelledError:
        logger.info("Searcher task cancelled")
    except Exception:
        logger.exception("Searcher crashed")


def main(
    config: LocalFilesConfig,
    logger_queue: multiprocessing.Queue,
    search_request_queue: multiprocessing.Queue,
    search_response_queue: multiprocessing.Queue,
    nudge_queue: Optional[multiprocessing.Queue] = None,
    embedder_nudge_queue: Optional[multiprocessing.Queue] = None,
    text_encode_request_queue: Optional[multiprocessing.Queue] = None,
    text_encode_response_queue: Optional[multiprocessing.Queue] = None,
) -> None:
    """Entry point for the searcher subprocess."""
    set_proc_title("kal-searcher")

    configure_worker_logging(logger_queue)

    asyncio.run(
        async_main(
            config,
            search_request_queue,
            search_response_queue,
            nudge_queue,
            embedder_nudge_queue,
            text_encode_request_queue,
            text_encode_response_queue,
        )
    )
