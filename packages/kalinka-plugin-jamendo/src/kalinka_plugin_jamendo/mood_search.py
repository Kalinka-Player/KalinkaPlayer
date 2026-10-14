"""Mood/semantic search index for the Jamendo plugin.

Embeds the query with the server's shared MiniLM embedder (SDK
``context.embedder``) and KNN-matches it (cosine, sqlite-vec) against
precomputed MiniLM embeddings of the JamendoMaxCaps captions, one vector per
track. The index is a downloaded asset; this opens it read-only and returns
(track_id, distance). If the index can't be fetched or the embedder is
unavailable, ``available()`` is False and ai_search returns nothing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import urllib.request
from typing import List, Optional, Tuple

from kalinka_plugin_sdk.embedding import TextEmbedder

logger = logging.getLogger(__name__.split(".")[-1])

# The index vectors were computed with exactly this model and asset version
# (see kalinka-training jamendomaxcaps_embed.py); a different shared model —
# or re-exported assets of the same model — would silently return near-random
# neighbours, so refuse to search instead. When the server ships a new
# embedder version, this plugin must be updated together with a rebuilt index.
_EXPECTED_MODEL_ID = "all-MiniLM-L6-v2"
_EXPECTED_MODEL_VERSION = 1


def download_file(url: str, dest: str) -> bool:
    """Download url -> dest atomically (via a .part temp). True on success."""
    tmp = dest + ".part"
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        logger.info("downloading %s", url)
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, dest)
        return True
    except Exception as e:
        logger.warning("download failed (%s): %s", url, e)
        if os.path.exists(tmp):
            os.remove(tmp)
        return False

# Our shipped mood-index naming scheme: jamendo_index.sqlite (v1) and
# jamendo_index_v<N>.sqlite (v2+). The index is fetched only when the configured
# path is absent, so a content change ships a renamed asset; this pattern lets
# _remove_legacy_indexes() reclaim every superseded version (~140 MB each)
# without maintaining a hardcoded per-version list. Anchored so it only ever
# matches our own assets, never a user-pinned custom filename. Also clears a
# leftover ``.part`` from an interrupted download of one of those assets.
_INDEX_NAME_RE = re.compile(r"^jamendo_index(?:_v\d+)?\.sqlite(?:\.part)?$")


class JamendoMoodIndex:
    def __init__(self, index_path: str, index_url: Optional[str],
                 embedder: Optional[TextEmbedder]):
        self._index_path = os.path.expanduser(index_path)
        self._index_url = index_url
        self._embedder = embedder
        self._lock = asyncio.Lock()  # one provisioning; concurrent callers await it
        self._available: Optional[bool] = None
        self._dtype = "float32"
        self._int8_scale = 508.0

    async def available(self) -> bool:
        if self._embedder is None:
            # Server predates the shared embedder (SDK < 1.2) — no model to
            # encode queries with.
            return False
        if self._available is None:
            async with self._lock:
                if self._available is None:
                    ok = True
                    if (
                        self._embedder.model_id != _EXPECTED_MODEL_ID
                        or self._embedder.model_version
                        != _EXPECTED_MODEL_VERSION
                    ):
                        logger.warning(
                            "shared embedder is %s v%s but the index needs "
                            "%s v%s; ai_search off (update the Jamendo "
                            "plugin/index)",
                            self._embedder.model_id,
                            self._embedder.model_version,
                            _EXPECTED_MODEL_ID, _EXPECTED_MODEL_VERSION)
                        ok = False
                    loop = asyncio.get_running_loop()
                    # Cheap checks before the model's and the index's downloads.
                    if ok:
                        ok = await loop.run_in_executor(
                            None, self._index_obtainable)
                    if ok:
                        ok = await self._embedder.available()
                        if not ok:
                            logger.warning(
                                "shared text embedder unavailable; ai_search off")
                    if ok:
                        ok = await loop.run_in_executor(None, self._provision)
                    self._available = ok
        return self._available

    def _index_obtainable(self) -> bool:
        """Whether the index can be queried once present, and is present or
        can be fetched; checked without downloading anything."""
        try:
            import sqlite_vec  # noqa: F401
        except Exception as e:
            logger.warning("sqlite-vec unavailable (%s); ai_search off", e)
            return False
        if not os.path.exists(self._index_path) and not self._index_url:
            logger.warning("Jamendo index missing (%s); ai_search off",
                           self._index_path)
            return False
        return True

    def _provision(self) -> bool:
        """Fetch the index if missing; read the index dtype/scale.

        Blocking (downloads, sqlite) — always called via an executor, and
        only once _index_obtainable() has passed.
        """
        if not os.path.exists(self._index_path) and not (
            self._index_url and download_file(self._index_url, self._index_path)
        ):
            logger.warning("could not fetch the Jamendo index to %s; "
                           "ai_search off", self._index_path)
            return False
        try:
            meta = self._read_meta()
            self._dtype = meta.get("dtype", "float32")
            self._int8_scale = float(meta.get("int8_scale", 508.0))
        except Exception as e:  # corrupt/partial index -> stay off, don't crash
            logger.warning("unreadable index %s (%s); ai_search off",
                           self._index_path, e)
            return False
        logger.info("Jamendo mood index ready (%s, %s)",
                    self._index_path, self._dtype)
        self._remove_legacy_indexes()
        return True

    def _remove_legacy_indexes(self) -> None:
        """Best-effort: delete superseded index assets in the index dir.

        Removes every older versioned index file (and stale ``.part`` temps)
        matching our naming scheme, so a version bump self-cleans without a
        hardcoded list. Runs only after the current index is confirmed good,
        never touches the index we're actually using, and never a file outside
        our scheme (e.g. a user-pinned custom name).
        """
        index_dir = os.path.dirname(self._index_path)
        current = os.path.basename(self._index_path)
        try:
            entries = os.listdir(index_dir)
        except OSError:
            return
        for name in entries:
            if name == current or not _INDEX_NAME_RE.match(name):
                continue
            stale = os.path.join(index_dir, name)
            try:
                os.remove(stale)
                logger.info("removed superseded Jamendo index %s", stale)
            except OSError as e:
                logger.warning("could not remove stale index %s: %s", stale, e)

    def _read_meta(self) -> dict:
        con = sqlite3.connect(f"file:{self._index_path}?mode=ro", uri=True)
        try:
            return dict(con.execute("SELECT key, value FROM meta").fetchall())
        finally:
            con.close()

    async def search(self, query: str, limit: int) -> List[Tuple[int, float]]:
        if not await self.available():
            return []
        try:
            # The shared embedder serializes inference internally.
            vec = (await self._embedder.embed([query]))[0]
        except Exception as e:
            logger.warning("query encode failed: %s", e)
            return []
        return await self._knn(self._to_blob(vec), limit)

    def _to_blob(self, vec) -> bytes:
        """Pack the float32 L2-normalized query vector for sqlite-vec,
        quantized to int8 when the index is int8."""
        if self._dtype != "int8":
            return vec.tobytes()
        import numpy as np
        return np.clip(np.round(vec * self._int8_scale), -127, 127).astype(
            np.int8).tobytes()

    async def _knn(self, blob: bytes, limit: int) -> List[Tuple[int, float]]:
        import aiosqlite
        import sqlite_vec

        match = "vec_int8(?)" if self._dtype == "int8" else "?"
        try:
            async with aiosqlite.connect(
                f"file:{self._index_path}?mode=ro", uri=True, timeout=5.0
            ) as conn:
                await conn.enable_load_extension(True)
                await conn.load_extension(sqlite_vec.loadable_path())
                await conn.enable_load_extension(False)
                cur = await conn.execute(
                    "SELECT track_id, distance FROM vec_tracks"
                    f" WHERE embedding MATCH {match} ORDER BY distance LIMIT ?",
                    (blob, limit),
                )
                rows = await cur.fetchall()
            return [(int(r[0]), float(r[1])) for r in rows]
        except Exception as e:
            logger.warning("Jamendo KNN query failed: %s", e)
            return []
