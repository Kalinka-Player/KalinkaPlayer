"""
Database layer for the search subprocess.

Provides the CLAP KNN (audio/text) and mood (valence/arousal) lookups used
to rank search results, plus the sqlite-vec availability check.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Sequence

from ..config_model import LocalFilesConfig
from ..db_connection import LibraryConnection, LibraryDb
from ..worker_utils import retry_db_locked
from .context_filter import TRACK_FACTS, TRACK_LANGUAGE, Facet, TrackContext

logger = logging.getLogger(__name__.split(".")[-1])

#: Ids bound per statement, well inside SQLite's variable limit.
_BATCH = 500
#: The most nearest neighbours sqlite-vec answers in one query.
KNN_MAX = 4096


@dataclass(frozen=True)
class _VectorHome:
    """Where one kind's CLAP vectors are kept: its KNN index and its row's mirror column."""

    vec_table: str
    vec_key: str
    table: str
    column: str


_TRACK_VECTORS = _VectorHome("vec_tracks_clap", "track_id", "tracks", "embedding_clap_audio")
_ALBUM_VECTORS = _VectorHome("vec_albums_clap", "album_id", "albums", "embedding_clap")


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
        found: dict[str, tuple[float, float]] = {}
        try:
            async with self._open() as conn:
                for start in range(0, len(track_ids), _BATCH):
                    chunk = track_ids[start : start + _BATCH]
                    cursor = await conn.execute(
                        f"SELECT id, mood_valence, mood_arousal FROM tracks "
                        f"WHERE id IN ({','.join('?' * len(chunk))}) AND mood_valence IS NOT NULL",
                        chunk,
                    )
                    found.update({row[0]: (row[1], row[2]) for row in await cursor.fetchall()})
            return found
        except Exception as e:
            logger.warning("Bulk V-A fetch failed: %s", e)
            return {}

    async def knn_search_albums(self, query_blob: bytes, limit: int = 50) -> list[dict]:
        """KNN search on vec_albums_clap, the mean audio vector of each album.

        Returns [{"album_id": str, "distance": float}] sorted by distance (ascending).
        """
        if not self._vec_available:
            return []
        try:
            async with self._open() as conn:
                await self._load_vec(conn)
                cursor = await conn.execute(
                    "SELECT album_id, distance FROM vec_albums_clap"
                    " WHERE embedding MATCH vec_int8(?) ORDER BY distance LIMIT ?",
                    (query_blob, limit),
                )
                rows = await cursor.fetchall()
            return [{"album_id": row[0], "distance": row[1]} for row in rows]
        except Exception as e:
            logger.warning("KNN album search failed: %s", e)
            return []

    async def get_track_vectors(self, track_ids: Sequence[str]) -> dict[str, bytes]:
        """{track_id: int8 CLAP audio vector} for the tracks that have one."""
        return await self._vectors(_TRACK_VECTORS, track_ids)

    async def get_album_vectors(self, album_ids: Sequence[str]) -> dict[str, bytes]:
        """{album_id: int8 mean CLAP audio vector} for the albums that have one."""
        return await self._vectors(_ALBUM_VECTORS, album_ids)

    async def _vectors(self, where: _VectorHome, ids: Sequence[str]) -> dict[str, bytes]:
        """Each id's vector from the KNN index search ranks with, else from the
        row's mirror column: a library can hold either without the other. An
        index row whose track or album is gone is not one."""
        found: dict[str, bytes] = {}
        wanted = list(dict.fromkeys(ids))
        try:
            async with self._open() as conn:
                if self._vec_available:
                    await self._load_vec(conn)
                    indexed = await self._select_in(conn, where.vec_table, where.vec_key, "embedding", wanted)
                    present = await self._select_in(conn, where.table, "id", "id", list(indexed))
                    found.update({i: v for i, v in indexed.items() if i in present})
                missing = [i for i in wanted if i not in found]
                found.update(await self._select_in(conn, where.table, "id", where.column, missing))
        except Exception as e:
            logger.warning("Fetching %s vectors failed: %s", where.table, e)
        return found

    @staticmethod
    async def _select_in(conn, table: str, key: str, column: str, ids: list[str]) -> dict[str, bytes]:
        rows: dict[str, bytes] = {}
        for start in range(0, len(ids), _BATCH):
            chunk = ids[start : start + _BATCH]
            cursor = await conn.execute(
                f"SELECT {key}, {column} FROM {table}"
                f" WHERE {key} IN ({','.join('?' * len(chunk))}) AND {column} IS NOT NULL",
                chunk,
            )
            rows.update({row[0]: row[1] for row in await cursor.fetchall()})
        return rows

    async def recent_plays(self, limit: int) -> list[tuple[str, bool, bool]]:
        """The latest plays, newest first, as (track_id, completed, skipped)."""
        try:
            async with self._open() as conn:
                cursor = await conn.execute(
                    "SELECT track_id, completed, skipped FROM play_history"
                    " ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
                rows = await cursor.fetchall()
            return [(row[0], bool(row[1]), bool(row[2])) for row in rows]
        except Exception as e:
            logger.warning("Reading the play history failed: %s", e)
            return []

    async def albums_of_tracks(self, track_ids: Sequence[str]) -> set[str]:
        """The albums ``track_ids`` belong to."""
        albums: set[str] = set()
        wanted = list(dict.fromkeys(track_ids))
        try:
            async with self._open() as conn:
                for start in range(0, len(wanted), _BATCH):
                    chunk = wanted[start : start + _BATCH]
                    cursor = await conn.execute(
                        f"SELECT DISTINCT album_id FROM tracks WHERE id IN ({','.join('?' * len(chunk))})",
                        chunk,
                    )
                    albums.update(row[0] for row in await cursor.fetchall())
        except Exception as e:
            logger.warning("Looking up albums of tracks failed: %s", e)
        return albums

    async def recently_added_albums(self, limit: int) -> list[str]:
        """The albums whose files arrived last, newest first."""
        try:
            async with self._open() as conn:
                cursor = await conn.execute(
                    "SELECT t.album_id FROM tracks t JOIN library_file lf ON lf.file_id = t.id"
                    " WHERE t.album_id != 'unknown_album'"
                    " GROUP BY t.album_id ORDER BY MAX(lf.first_indexed) DESC LIMIT ?",
                    (limit,),
                )
                rows = await cursor.fetchall()
            return [row[0] for row in rows]
        except Exception as e:
            logger.warning("Listing recently added albums failed: %s", e)
            return []

    async def facets_known(self, facets: Sequence[Facet]) -> list[Facet]:
        """Those of ``facets`` the library can tell: some track carries what each reads."""
        known: list[Facet] = []
        try:
            async with self._open() as conn:
                for facet in facets:
                    cursor = await conn.execute(
                        f"SELECT 1 FROM {TRACK_FACTS} WHERE {facet.known()} LIMIT 1"
                    )
                    if await cursor.fetchone() is not None:
                        known.append(facet)
        except Exception as e:
            logger.warning("Checking what the library can tell failed: %s", e)
            return []
        return known

    async def tracks_matching(self, facets: Sequence[Facet]) -> Optional[list[str]]:
        """Tracks every one of ``facets`` matches; None when the library could not be read."""
        clauses, params = [], []
        for facet in facets:
            clause, values = facet.matches()
            clauses.append(f"({clause})")
            params.extend(values)
        try:
            async with self._open() as conn:
                cursor = await conn.execute(
                    f"SELECT t.id FROM {TRACK_FACTS} WHERE {' AND '.join(clauses)}", params
                )
                rows = await cursor.fetchall()
            return [row[0] for row in rows]
        except Exception as e:
            logger.warning("Listing tracks by facet failed: %s", e)
            return None

    async def track_contexts(self, track_ids: Sequence[str]) -> dict[str, TrackContext]:
        """What a queue's rules read of each of ``track_ids`` the library holds."""
        select = f"SELECT t.id, {TRACK_LANGUAGE}, a.genre, a.title, t.title FROM {TRACK_FACTS}"
        rows: list = []
        wanted = list(dict.fromkeys(track_ids))
        try:
            async with self._open() as conn:
                for start in range(0, len(wanted), _BATCH):
                    chunk = wanted[start : start + _BATCH]
                    cursor = await conn.execute(
                        f"{select} WHERE t.id IN ({','.join('?' * len(chunk))})", chunk
                    )
                    rows.extend(await cursor.fetchall())
        except Exception as e:
            logger.warning("Reading track contexts failed: %s", e)
            return {}
        return {row[0]: TrackContext.read(*row[1:]) for row in rows}
