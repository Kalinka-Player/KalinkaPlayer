"""
Database layer for the search subprocess.

Provides the CLAP KNN (audio/text) and mood (valence/arousal) lookups used
to rank search results, plus the sqlite-vec availability check.
"""

from __future__ import annotations

import logging
import os

from ..config_model import LocalFilesConfig
from ..db_connection import LibraryConnection, LibraryDb
from ..worker_utils import retry_db_locked

logger = logging.getLogger(__name__.split(".")[-1])



@retry_db_locked
class AsyncSearcherDb(LibraryDb):
    """
    Async database layer for the search subprocess.

    All methods open short-lived connections (same pattern as AsyncEmbedderDb).
    """

    def __init__(self, config: LocalFilesConfig):
        self.db_path = os.path.expanduser(config.db_path)
        self._vec_available = False

    # ------------------------------------------------------------------
    # Schema initialisation
    # ------------------------------------------------------------------

    async def _check_vec_available(self) -> None:
        """Lazily detect whether sqlite-vec is available."""
        if self._vec_available:
            return
        try:
            import sqlite_vec

            async with self._open() as conn:
                await conn.enable_load_extension(True)
                await conn.load_extension(sqlite_vec.loadable_path())
                await conn.enable_load_extension(False)

            self._vec_available = True
            logger.info("sqlite-vec loaded; KNN search enabled")
        except Exception as e:
            logger.warning("sqlite-vec not available (%s); KNN search disabled", e)

    async def _load_vec(self, conn: LibraryConnection) -> None:
        """Load sqlite-vec extension into an open connection."""
        import sqlite_vec

        await conn.enable_load_extension(True)
        await conn.load_extension(sqlite_vec.loadable_path())
        await conn.enable_load_extension(False)

    # ------------------------------------------------------------------
    # KNN search (CLAP vector tables)
    # ------------------------------------------------------------------

    async def knn_search_text(self, query_blob: bytes, limit: int = 50) -> list[dict]:
        """KNN search on vec_tracks_clap_text.

        Returns [{"track_id": str, "distance": float}] sorted by distance (ascending).
        """
        if not self._vec_available:
            return []
        try:
            async with self._open() as conn:
                await self._load_vec(conn)
                cursor = await conn.execute(
                    "SELECT track_id, distance FROM vec_tracks_clap_text"
                    " WHERE embedding MATCH vec_int8(?) ORDER BY distance LIMIT ?",
                    (query_blob, limit),
                )
                rows = await cursor.fetchall()
            return [{"track_id": row[0], "distance": row[1]} for row in rows]
        except Exception as e:
            logger.warning("KNN text search failed: %s", e)
            return []

    async def knn_search_audio(self, query_blob: bytes, limit: int = 50) -> list[dict]:
        """KNN search on vec_tracks_clap (audio embeddings).

        Returns [{"track_id": str, "distance": float}] sorted by distance (ascending).
        """
        if not self._vec_available:
            return []
        try:
            async with self._open() as conn:
                await self._load_vec(conn)
                cursor = await conn.execute(
                    "SELECT track_id, distance FROM vec_tracks_clap"
                    " WHERE embedding MATCH vec_int8(?) ORDER BY distance LIMIT ?",
                    (query_blob, limit),
                )
                rows = await cursor.fetchall()
            return [{"track_id": row[0], "distance": row[1]} for row in rows]
        except Exception as e:
            logger.warning("KNN audio search failed: %s", e)
            return []

    async def knn_search_mood(
        self, valence: float, arousal: float, limit: int = 200
    ) -> list[dict]:
        """Tracks closest to a target (valence, arousal), as
        [{"track_id", "distance"}] (Euclidean) ascending. Full scan, no index.
        """
        try:
            async with self._open() as conn:
                cursor = await conn.execute(
                    "SELECT id, "
                    "(mood_valence - ?) * (mood_valence - ?) + "
                    "(mood_arousal - ?) * (mood_arousal - ?) AS d2 "
                    "FROM tracks WHERE mood_valence IS NOT NULL "
                    "ORDER BY d2 LIMIT ?",
                    (valence, valence, arousal, arousal, limit),
                )
                rows = await cursor.fetchall()
            return [{"track_id": row[0], "distance": row[1] ** 0.5} for row in rows]
        except Exception as e:
            logger.warning("Mood V-A search failed: %s", e)
            return []

    async def get_tracks_va_bulk(
        self, track_ids: list[str]
    ) -> dict[str, tuple[float, float]]:
        """Return {track_id: (valence, arousal)} for tracks that have mood set."""
        if not track_ids:
            return {}
        try:
            async with self._open() as conn:
                placeholders = ",".join("?" * len(track_ids))
                cursor = await conn.execute(
                    f"SELECT id, mood_valence, mood_arousal FROM tracks "
                    f"WHERE id IN ({placeholders}) AND mood_valence IS NOT NULL",
                    track_ids,
                )
                rows = await cursor.fetchall()
            return {row[0]: (row[1], row[2]) for row in rows}
        except Exception as e:
            logger.warning("Bulk V-A fetch failed: %s", e)
            return {}

    async def get_track_clap_embedding(self, track_id: str) -> bytes | None:
        """Fetch the CLAP audio embedding for a single track."""
        if not self._vec_available:
            return None
        try:
            async with self._open() as conn:
                await self._load_vec(conn)
                cursor = await conn.execute(
                    "SELECT embedding FROM vec_tracks_clap WHERE track_id = ?",
                    (track_id,),
                )
                row = await cursor.fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.warning("Failed to fetch CLAP embedding for %s: %s", track_id, e)
            return None
