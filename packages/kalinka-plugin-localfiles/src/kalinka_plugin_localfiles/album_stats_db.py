"""An album's track count and duration, kept in step with its tracks."""

from __future__ import annotations

from .db_connection import LibraryDb
from .worker_utils import retry_db_locked

_STALE = """
    SELECT 1 FROM albums a
    WHERE a.id = ?
      AND (a.track_count IS NOT (SELECT COUNT(*) FROM tracks WHERE album_id = a.id)
           OR a.duration IS NOT (SELECT SUM(duration) FROM tracks WHERE album_id = a.id))
"""

_REFRESH = """
    UPDATE albums SET
        track_count = (SELECT COUNT(*) FROM tracks WHERE album_id = albums.id),
        duration = (SELECT SUM(duration) FROM tracks WHERE album_id = albums.id)
    WHERE id = ?
"""


@retry_db_locked
class AlbumStatsDb(LibraryDb):
    """The album statistics the indexer and the enricher both refresh."""

    async def update_album_stats(self, album_id: str) -> None:
        """Count every album member, including known-artist loose tracks.

        @note Statistics that are already right are left alone, without a
            write: a rescan refreshes every album of the library, and an
            UPDATE takes the write lock even when it changes nothing.
        """
        async with self._open() as conn:
            cursor = await conn.cursor()
            await cursor.execute(_STALE, (album_id,))
            if await cursor.fetchone() is None:
                return
            await cursor.execute(_REFRESH, (album_id,))
            await conn.commit()
