import json
import os
import time
import aiosqlite
import logging
from typing import List, Dict, Optional, Any, Tuple


from ..config_model import LocalFilesConfig
from ..resolution.provenance_db import ProvenanceDb
from ..resolution.resolver import OBSERVED
from ..worker_utils import retry_db_locked

logger = logging.getLogger(__name__.split(".")[-1])

#: A cover source's size and nanosecond mtime: when either changes, the file
#: is worth reading again.
Fingerprint = Tuple[int, int]

#: The cover passes, as ``art_source_failures.kind``.
EMBEDDED_COVER = "embedded"

#: Where an album's cover decision is kept: the provenance of its image.
_COVER_FIELD = "image_url"


@retry_db_locked
class AsyncIndexerDb(ProvenanceDb):
    """
    Asynchronous database manager specifically for the file indexer.
    Handles operations required for scanning and indexing music files.
    """

    def __init__(self, config: LocalFilesConfig):
        self.db_path = os.path.expanduser(config.db_path)
        self.artwork_path = os.path.expanduser(config.artwork_path)
        self._columns_cache: Dict[str, set] = {}

        # Ensure directories exist
        db_dir = os.path.dirname(self.db_path)
        if db_dir:  # Only create directory if path is not empty
            os.makedirs(db_dir, exist_ok=True)
        os.makedirs(self.artwork_path, exist_ok=True)
        os.makedirs(os.path.join(self.artwork_path, "album"), exist_ok=True)
        os.makedirs(os.path.join(self.artwork_path, "artist"), exist_ok=True)

    async def get_track_by_path(self, file_path: str) -> Optional[Dict]:
        """Get track information by file path"""
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                """
                SELECT * FROM tracks WHERE file_path = ?
            """,
                (file_path,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_track_by_id(self, track_id: str) -> Optional[Dict]:
        """Get track information by ID"""
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                """
                SELECT t.*, a.title as album_title, ar.name as artist_name 
                FROM tracks t
                LEFT JOIN albums a ON t.album_id = a.id
                LEFT JOIN artists ar ON t.artist_id = ar.id
                WHERE t.id = ?
            """,
                (track_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_album_by_id(self, album_id: str) -> Optional[Dict]:
        """Get album information by ID"""
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                """
                SELECT a.*, ar.name as artist_name
                FROM albums a
                LEFT JOIN artists ar ON a.artist_id = ar.id
                WHERE a.id = ?
            """,
                (album_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_artist_by_id(self, artist_id: str) -> Optional[Dict]:
        """Get artist information by ID"""
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute("SELECT * FROM artists WHERE id = ?", (artist_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def _table_columns(self, conn, table: str) -> set:
        """Return the column names for *table*, cached (schema is static)."""
        cached = self._columns_cache.get(table)
        if cached is not None:
            return cached
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cursor.fetchall()}  # name at index 1
        self._columns_cache[table] = columns
        return columns

    async def _update(self, table: str, entity_id: str, data: Dict[str, Any]) -> None:
        """Update a row by id, ignoring keys that aren't real columns."""
        async with self._open() as conn:
            valid_columns = await self._table_columns(conn, table)
            filtered_data = {k: v for k, v in data.items() if k in valid_columns}
            if not filtered_data:
                logger.debug(f"No valid columns to update for {table} {entity_id}")
                return

            fields = [f"{key} = ?" for key in filtered_data]
            values = list(filtered_data.values()) + [entity_id]
            query = f"UPDATE {table} SET {', '.join(fields)} WHERE id = ?"
            await conn.execute(query, values)
            await conn.commit()

            filtered_out = set(data) - valid_columns
            if filtered_out:
                logger.debug(
                    f"Filtered out non-existent columns for {table} {entity_id}: "
                    f"{', '.join(filtered_out)}"
                )

    async def update_track(self, track_id: str, data: Dict[str, Any]) -> None:
        """Update track information"""
        await self._update("tracks", track_id, data)

    async def get_singles_missing_art(self) -> List[Tuple[str, str]]:
        """(track_id, file_path) for unknown_album tracks whose file embeds a
        cover (evidence art_phash) but that carry no track-level image yet."""
        async with self._open() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """
                SELECT t.id, t.file_path FROM tracks t
                JOIN track_evidence e ON e.track_id = t.id
                WHERE t.album_id = 'unknown_album'
                  AND t.image_url IS NULL
                  AND e.art_phash IS NOT NULL
                """
            )
            return [(row[0], row[1]) for row in await cursor.fetchall()]

    async def reassign_album(self, track_id: str, album_id: str) -> None:
        """The single owner of tracks.album_id writes — clustering / reconciler
        only. Album membership must flow through here, never through an
        enrichment plugin (§3 invariant: membership changes only in the
        clustering pass).

        A track that joins an album no longer needs a cover of its own, so
        what failed to give it one is forgotten."""
        async with self._open() as conn:
            await conn.execute(
                "UPDATE tracks SET album_id = ? WHERE id = ?", (album_id, track_id)
            )
            if album_id != "unknown_album":
                await conn.execute(
                    "DELETE FROM art_source_failures WHERE entity_id = ?",
                    (track_id,),
                )
            await conn.commit()

    async def update_album(self, album_id: str, data: Dict[str, Any]) -> None:
        """Update album information (only columns that exist in the table)."""
        await self._update("albums", album_id, data)

    async def insert_track(self, data: Dict[str, Any]) -> None:
        """Insert a new track"""
        async with self._open() as conn:
            cursor = await conn.cursor()

            # Build the query
            fields = ", ".join(data.keys())
            placeholders = ", ".join("?" for _ in data)
            values = list(data.values())

            query = f"INSERT OR REPLACE INTO tracks ({fields}) VALUES ({placeholders})"
            await cursor.execute(query, values)
            await conn.commit()

    async def insert_album(self, data: Dict[str, Any]) -> None:
        """Insert a new album"""
        async with self._open() as conn:
            cursor = await conn.cursor()

            # Build the query
            fields = ", ".join(data.keys())
            placeholders = ", ".join("?" for _ in data)
            values = list(data.values())

            query = f"INSERT OR REPLACE INTO albums ({fields}) VALUES ({placeholders})"
            await cursor.execute(query, values)
            await conn.commit()

    async def insert_artist(self, data: Dict[str, Any]) -> None:
        """Insert a new artist"""
        async with self._open() as conn:
            cursor = await conn.cursor()

            # Build the query
            fields = ", ".join(data.keys())
            placeholders = ", ".join("?" for _ in data)
            values = list(data.values())

            query = f"INSERT OR REPLACE INTO artists ({fields}) VALUES ({placeholders})"
            await cursor.execute(query, values)
            await conn.commit()

    async def upsert_library_file(
        self,
        file_id: str,
        current_path: str,
        size_bytes: int,
        modified_at: int,
        device_id: Optional[str],
        inode: Optional[str],
    ) -> None:
        """Maintain the file-identity row. first_indexed is written once and
        never rewritten (the durable add-time; last_updated is bumped by
        enrichment). Path/size/mtime/device/inode refresh on every pass."""
        async with self._open() as conn:
            await conn.execute(
                """
                INSERT INTO library_file
                    (file_id, current_path, size_bytes, modified_at,
                     device_id, inode, first_indexed)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    current_path = excluded.current_path,
                    size_bytes   = excluded.size_bytes,
                    modified_at  = excluded.modified_at,
                    device_id    = excluded.device_id,
                    inode        = excluded.inode
                """,
                (
                    file_id,
                    current_path,
                    size_bytes,
                    modified_at,
                    device_id,
                    inode,
                    int(time.time()),
                ),
            )
            await conn.commit()

    async def get_library_file_by_inode(
        self, device_id: str, inode: str, path: str
    ) -> Optional[Dict]:
        """The library_file row for a (device, inode) pair, if any — the
        move-detection lookup. The row already at ``path`` comes first: a
        share read under two spellings has a row at each for the same file,
        and the file is not moved onto a path it already holds."""
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM library_file WHERE device_id = ? AND inode = ? "
                "ORDER BY current_path = ? DESC",
                (device_id, inode, path),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def move_track(self, track_id: str, new_path: str) -> None:
        """Re-point a moved/renamed file to its new path, keeping identity.

        Updates tracks.file_path and library_file.current_path together; the
        id, first_indexed, evidence and enrichment all stay. This is the
        "rename is an UPDATE, not delete-plus-create" half of stable file
        identity.
        """
        async with self._open() as conn:
            await conn.execute(
                "UPDATE tracks SET file_path = ? WHERE id = ?",
                (new_path, track_id),
            )
            await conn.execute(
                "UPDATE library_file SET current_path = ? WHERE file_id = ?",
                (new_path, track_id),
            )
            await conn.commit()

    async def upsert_track_evidence(
        self, track_id: str, evidence: Dict[str, Any]
    ) -> None:
        """Upsert the current-snapshot evidence row. cue_sheet/cue_tracks/
        import_batch are COALESCE-kept when a refresh omits them, so a
        transient miss doesn't erase already-captured evidence. The art
        columns are written as read: a read failure raises before this, so a
        missing picture is a removed one."""
        raw_tags = evidence.get("raw_tags")
        stream_info = evidence.get("stream_info")
        cue_tracks = evidence.get("cue_tracks")
        async with self._open() as conn:
            await conn.execute(
                """
                INSERT INTO track_evidence
                    (track_id, raw_tags, stream_info, art_phash, art_type,
                     cue_sheet, cue_tracks, import_batch, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET
                    raw_tags     = excluded.raw_tags,
                    stream_info  = excluded.stream_info,
                    art_phash    = excluded.art_phash,
                    art_type     = excluded.art_type,
                    cue_sheet    = COALESCE(excluded.cue_sheet,
                                            track_evidence.cue_sheet),
                    cue_tracks   = COALESCE(excluded.cue_tracks,
                                            track_evidence.cue_tracks),
                    import_batch = COALESCE(excluded.import_batch,
                                            track_evidence.import_batch),
                    updated_at   = excluded.updated_at
                """,
                (
                    track_id,
                    json.dumps(raw_tags) if raw_tags is not None else None,
                    json.dumps(stream_info) if stream_info is not None else None,
                    evidence.get("art_phash"),
                    evidence.get("art_type"),
                    evidence.get("cue_sheet"),
                    json.dumps(cue_tracks) if cue_tracks is not None else None,
                    evidence.get("import_batch"),
                    int(time.time()),
                ),
            )
            await conn.commit()

    async def update_album_stats(self, album_id: str) -> None:
        """Update album statistics (track count and duration).

        The "Unknown Album" sentinel counts only unknown-artist tracks —
        known-artist loose tracks are browsed under their artist, and the
        card count must match that filtered listing (get_album_tracks)."""
        cond = (
            " AND artist_id = 'unknown_artist'"
            if album_id == "unknown_album" else ""
        )
        async with self._open() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                f"""
                UPDATE albums SET
                track_count = (SELECT COUNT(*) FROM tracks WHERE album_id = ?{cond}),
                duration = (SELECT SUM(duration) FROM tracks WHERE album_id = ?{cond})
                WHERE id = ?
            """,
                (album_id, album_id, album_id),
            )
            await conn.commit()

    async def get_tracks_with_evidence(self) -> List[Tuple[Dict, Dict]]:
        """All tracks paired with their track_evidence (empty dict if none),
        for the clustering pass."""
        _ev_cols = ("raw_tags", "stream_info", "art_phash", "cue_sheet",
                    "cue_tracks", "import_batch")
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT t.*, ar.name AS artist_name,
                       e.raw_tags, e.stream_info, e.art_phash,
                       e.cue_sheet, e.cue_tracks, e.import_batch
                FROM tracks t
                LEFT JOIN artists ar ON ar.id = t.artist_id
                LEFT JOIN track_evidence e ON e.track_id = t.id
                """
            )
            rows = await cur.fetchall()
        out: List[Tuple[Dict, Dict]] = []
        for r in rows:
            d = dict(r)
            ev = {c: d.pop(c) for c in _ev_cols}
            out.append((d, ev))
        return out

    async def get_all_tracks(self) -> List[Dict]:
        """Get all tracks in the database"""
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                """
                SELECT * FROM tracks
                ORDER BY file_path
            """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_track(self, track_id: str) -> bool:
        """Delete a track by ID. Returns True if successful."""
        async with self._open() as conn:
            cursor = await conn.cursor()
            await cursor.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
            deleted = cursor.rowcount > 0
            await cursor.execute(
                "DELETE FROM art_source_failures WHERE entity_id = ?", (track_id,)
            )
            await conn.commit()
            return deleted

    async def get_failure(self, file_path: str) -> Optional[Dict]:
        """Get the recorded extraction failure for a file path, if any."""
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT * FROM indexer_failures WHERE file_path = ?",
                (file_path,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def record_failure(
        self, file_path: str, file_size: int, mtime_ns: int, error: str
    ) -> int:
        """Record (or update) a metadata-extraction failure for a file.

        ``mtime_ns`` is the nanosecond-resolution mtime (``stat.st_mtime_ns``)
        so a fixed-but-broken file re-written within the same integer second
        doesn't collide on the key. Stored in the ``modified_time`` column.

        ``attempts`` is incremented only while the file is unchanged
        (same size + mtime); a changed file resets the counter so a
        still-uploading file is treated as a fresh attempt each time.
        Returns the resulting attempt count.
        """
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT file_size, modified_time, attempts "
                "FROM indexer_failures WHERE file_path = ?",
                (file_path,),
            )
            row = await cursor.fetchone()
            if (
                row
                and row["file_size"] == file_size
                and row["modified_time"] == mtime_ns
            ):
                attempts = row["attempts"] + 1
            else:
                attempts = 1
            await cursor.execute(
                """
                INSERT OR REPLACE INTO indexer_failures
                    (file_path, file_size, modified_time, error, attempts, last_attempt)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (file_path, file_size, mtime_ns, error, attempts, int(time.time())),
            )
            await conn.commit()
            return attempts

    async def clear_failure(self, file_path: str) -> None:
        """Remove any recorded extraction failure for a file path, and any
        cover that failed to come out of it: it has just been read again."""
        async with self._open() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "DELETE FROM indexer_failures WHERE file_path = ?", (file_path,)
            )
            await cursor.execute(
                "DELETE FROM art_source_failures WHERE source_path = ?",
                (file_path,),
            )
            await conn.commit()

    async def forget_failures_of_older_readers(self, version: int) -> int:
        """Forget every parked extraction failure unless ``version`` of the
        readers recorded them, then record ``version``.

        A file stays parked while it is unchanged, so readers that learn a
        format would otherwise never see the files they once refused.
        Returns how many failures were forgotten.
        """
        async with self._open() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT value FROM indexer_state WHERE key = 'readers'"
            )
            row = await cursor.fetchone()
            if row and row[0] == str(version):
                return 0
            await cursor.execute("DELETE FROM indexer_failures")
            forgotten = cursor.rowcount
            await cursor.execute(
                "INSERT OR REPLACE INTO indexer_state (key, value) "
                "VALUES ('readers', ?)",
                (str(version),),
            )
            await conn.commit()
            return forgotten

    async def get_failure_paths(self) -> List[str]:
        """Return all file paths currently in the failure cache."""
        async with self._open() as conn:
            cursor = await conn.cursor()
            await cursor.execute("SELECT file_path FROM indexer_failures")
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def get_art_failures(
        self, kind: str
    ) -> Dict[str, Dict[str, Fingerprint]]:
        """What one cover pass has written off, as entity id -> source path ->
        the fingerprint the source had then."""
        async with self._open() as conn:
            cursor = await conn.execute(
                "SELECT entity_id, source_path, size, mtime_ns "
                "FROM art_source_failures WHERE kind = ?",
                (kind,),
            )
            failures: Dict[str, Dict[str, Fingerprint]] = {}
            for entity_id, path, size, mtime_ns in await cursor.fetchall():
                failures.setdefault(entity_id, {})[path] = (size, mtime_ns)
            return failures

    async def replace_art_failures(
        self, entity_id: str, kind: str, sources: Dict[str, Fingerprint]
    ) -> None:
        """Make ``sources`` all that one cover pass has written off for
        ``entity_id``, forgetting what it had before."""
        async with self._open() as conn:
            await conn.execute(
                "DELETE FROM art_source_failures WHERE entity_id = ? AND kind = ?",
                (entity_id, kind),
            )
            await conn.executemany(
                "INSERT OR REPLACE INTO art_source_failures "
                "(entity_id, source_path, kind, size, mtime_ns) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (entity_id, path, kind, size, mtime_ns)
                    for path, (size, mtime_ns) in sources.items()
                ],
            )
            await conn.commit()

    async def set_album_cover(
        self, album_id: str, origin: Optional[Dict[str, Any]] = None
    ) -> None:
        """Point an album at the cover just stored for it, and forget every
        source that failed to give it one.

        @param origin The decision the cover was stored for, kept as the
            image's provenance; see :meth:`get_cover_origins`.
        """
        async with self._open() as conn:
            await conn.execute(
                "UPDATE albums SET image_url = ?, image_generated = 0 WHERE id = ?",
                (f"{album_id}.jpg", album_id),
            )
            await conn.execute(
                "DELETE FROM art_source_failures WHERE entity_id = ?", (album_id,)
            )
            await conn.commit()
        if origin is not None:
            await self.record_cover_origin(album_id, origin)

    async def clear_local_album_cover(
        self, album_id: str, origin: Dict[str, Any]
    ) -> None:
        """Take away a cover local art gave an album that no longer has any,
        and open the album to the online sources again."""
        async with self._open() as conn:
            await conn.execute(
                "UPDATE albums SET image_url = NULL, image_generated = 0, "
                "enriched = 0 WHERE id = ?",
                (album_id,),
            )
            await conn.commit()
        await self.record_cover_origin(album_id, origin)

    async def record_cover_origin(
        self, album_id: str, origin: Dict[str, Any]
    ) -> None:
        await self.record_resolved_origin(
            "album",
            album_id,
            _COVER_FIELD,
            origin.get("source", "none"),
            OBSERVED,
            json.dumps(origin),
        )

    async def get_cover_origins(self) -> Dict[str, Dict[str, Any]]:
        """Each album's cover decision: the cover it should have
        (``desired``), the ``source`` and file it got, and what its folders
        held (``folder``)."""
        async with self._open() as conn:
            cursor = await conn.execute(
                "SELECT entity_id, evidence_ref FROM resolved_origin "
                "WHERE entity_type = 'album' AND field = ?",
                (_COVER_FIELD,),
            )
            rows = await cursor.fetchall()
        origins: Dict[str, Dict[str, Any]] = {}
        for album_id, evidence in rows:
            try:
                origins[album_id] = json.loads(evidence)
            except (TypeError, ValueError):
                continue
        return origins

    async def get_album_covers(self) -> Dict[str, Tuple[Optional[str], int]]:
        """(image_url, image_generated) of every album."""
        async with self._open() as conn:
            cursor = await conn.execute(
                "SELECT id, image_url, COALESCE(image_generated, 0) FROM albums"
            )
            return {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}

    async def get_artwork_inputs(self) -> List[Dict[str, Any]]:
        """Every track with what its embedded cover is, for the cover pass."""
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT t.id, t.album_id, t.file_path, t.disc_number,
                       t.track_number, e.art_phash, e.art_type
                FROM tracks t
                LEFT JOIN track_evidence e ON e.track_id = t.id
                """
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def set_track_cover(self, track_id: str) -> None:
        """As :meth:`set_album_cover`, for a single's own cover."""
        async with self._open() as conn:
            await conn.execute(
                "UPDATE tracks SET image_url = ? WHERE id = ?",
                (f"{track_id}.jpg", track_id),
            )
            await conn.execute(
                "DELETE FROM art_source_failures WHERE entity_id = ?", (track_id,)
            )
            await conn.commit()

    async def set_scan_progress(
        self,
        total: int,
        processed: int,
        active: bool,
        *,
        changed: int = 0,
        last_scan_at: Optional[float] = None,
    ) -> None:
        """Publish scan progress for get_indexer_status().

        ``total`` comes from the listing walk, ``processed`` is the number
        of files handled so far. ``last_scan_at`` survives failed or aborted
        checks; the live worker flags determine availability and activity.
        """
        value = json.dumps(
            {
                "total": total,
                "processed": processed,
                "active": active,
                "changed": changed,
                "last_scan_at": last_scan_at,
                "updated_at": int(time.time()),
            }
        )
        async with self._open() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO indexer_state (key, value) "
                "VALUES ('scan_progress', ?)",
                (value,),
            )
            await conn.commit()

    async def get_indexer_state(self, key: str) -> Optional[str]:
        """One value from ``indexer_state``, or None when it was never set."""
        async with self._open() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT value FROM indexer_state WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set_indexer_state(self, key: str, value: str) -> None:
        async with self._open() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO indexer_state (key, value) VALUES (?, ?)",
                (key, value),
            )
            await conn.commit()

    async def get_latest_evidence_time(self) -> int:
        async with self._open() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(MAX(updated_at), 0) FROM track_evidence"
            )
            return (await cursor.fetchone())[0]

    async def get_tracks_read_before(self, since: int) -> List[Tuple[str, str]]:
        """(track_id, file_path) of the tracks whose evidence was last
        written before ``since``."""
        async with self._open() as conn:
            cursor = await conn.execute(
                "SELECT t.id, t.file_path FROM tracks t "
                "JOIN track_evidence e ON e.track_id = t.id "
                "WHERE COALESCE(e.updated_at, 0) < ?",
                (since,),
            )
            return [(row[0], row[1]) for row in await cursor.fetchall()]

    async def set_art_evidence(
        self, covers: List[Tuple[str, Optional[str], Optional[int]]]
    ) -> None:
        """Record ``(track_id, art_phash, art_type)``: what each track's
        embedded cover is, read on its own."""
        if not covers:
            return
        now = int(time.time())
        async with self._open() as conn:
            await conn.executemany(
                "UPDATE track_evidence SET art_phash = ?, art_type = ?, "
                "updated_at = ? WHERE track_id = ?",
                [(phash, kind, now, track_id) for track_id, phash, kind in covers],
            )
            await conn.commit()

    async def set_root_signature(self, root: str, identity: str) -> None:
        """Remember which mounted filesystem a music root was indexed from
        ("nfs4 192.168.1.5:/export"); the cleanup refuses to purge under a
        root whose current mount no longer matches."""
        async with self._open() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO indexer_state (key, value) "
                "VALUES (?, ?)",
                (f"root_signature:{root}", identity),
            )
            await conn.commit()

    async def get_root_signature(self, root: str) -> Optional[str]:
        """The identity stored by :meth:`set_root_signature`, or None."""
        async with self._open() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT value FROM indexer_state WHERE key = ?",
                (f"root_signature:{root}",),
            )
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def set_filename_model_identity(self, identity: str) -> None:
        """Remember which filename model produced the library's guessed
        values, so new weights can be noticed and those rows re-read."""
        async with self._open() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO indexer_state (key, value) "
                "VALUES ('filename_model', ?)",
                (identity,),
            )
            await conn.commit()

    async def get_filename_model_identity(self) -> Optional[str]:
        """The identity stored by :meth:`set_filename_model_identity`, or
        None on a library indexed before it was recorded."""
        async with self._open() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT value FROM indexer_state WHERE key = 'filename_model'"
            )
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def get_tracks_named_by_their_path(self, source: str) -> List[Dict]:
        """Tracks whose title nothing but their own path ever supplied.

        These are the only rows a new model may re-derive: a tag or an
        external match resolved every other title, and re-deriving one of
        those would undo it.
        """
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                """
                SELECT t.* FROM tracks t
                JOIN resolved_origin o
                  ON o.entity_type = 'track'
                 AND o.entity_id = t.id
                 AND o.field = 'title'
                WHERE o.source = ?
                """,
                (source,),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def get_scan_progress(self) -> Optional[Dict]:
        """Read the scan progress published by :meth:`set_scan_progress`.
        Returns None when absent or unreadable."""
        async with self._open() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT value FROM indexer_state WHERE key = 'scan_progress'"
            )
            row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None

    async def _forget_provenance(self, cursor, entity_type: str, ids: List[str]):
        """Drop the claims and origins of entities that no longer exist.

        Neither table has a foreign key, so a deleted row's provenance would
        otherwise outlive it — and entity ids are content-derived, so nothing
        ever reclaims one and cleans it up by accident.
        """
        if not ids:
            return
        holes = ", ".join(["?"] * len(ids))
        for table in ("resolved_origin", "metadata_claims"):
            await cursor.execute(
                f"DELETE FROM {table} WHERE entity_type = ? "
                f"AND entity_id IN ({holes})",
                [entity_type, *ids],
            )

    async def delete_orphaned_albums_and_artists(self) -> Tuple[int, int]:
        """Delete albums and artists that have no tracks referencing them,
        and the provenance and failed cover sources recorded against them.
        Returns tuple of (deleted_albums_count, deleted_artists_count)"""
        async with self._open() as conn:
            cursor = await conn.cursor()

            # Get albums with no tracks
            await cursor.execute(
                """
                SELECT id FROM albums 
                WHERE id NOT IN (SELECT DISTINCT album_id FROM tracks)
                AND id != 'unknown_album'
                """
            )
            rows = await cursor.fetchall()
            orphaned_albums = [row[0] for row in rows]

            # Delete orphaned albums
            deleted_albums = 0
            if orphaned_albums:
                albums_placeholders = ", ".join(["?"] * len(orphaned_albums))
                await cursor.execute(
                    f"DELETE FROM albums WHERE id IN ({albums_placeholders})",
                    orphaned_albums,
                )
                deleted_albums = cursor.rowcount
                await self._forget_provenance(cursor, "album", orphaned_albums)
                await cursor.execute(
                    "DELETE FROM art_source_failures "
                    f"WHERE entity_id IN ({albums_placeholders})",
                    orphaned_albums,
                )

            # Get artists with no tracks or albums. ``unknown_artist``
            # is excluded as the only sentinel — every other artist
            # row should be backed by at least one track or album.
            await cursor.execute(
                """
                SELECT id FROM artists
                WHERE id NOT IN (SELECT DISTINCT artist_id FROM tracks)
                AND id NOT IN (SELECT DISTINCT artist_id FROM albums)
                AND id != 'unknown_artist'
                """
            )
            rows = await cursor.fetchall()
            orphaned_artists = [row[0] for row in rows]

            # Delete orphaned artists
            deleted_artists = 0
            if orphaned_artists:
                artists_placeholders = ", ".join(["?"] * len(orphaned_artists))
                await cursor.execute(
                    f"DELETE FROM artists WHERE id IN ({artists_placeholders})",
                    orphaned_artists,
                )
                deleted_artists = cursor.rowcount
                await self._forget_provenance(cursor, "artist", orphaned_artists)

            await conn.commit()
            return deleted_albums, deleted_artists
