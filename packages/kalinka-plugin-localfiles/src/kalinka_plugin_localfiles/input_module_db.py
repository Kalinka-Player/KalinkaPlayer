import os
import re
import shutil
import sqlite3
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional, Any, Sequence, Tuple
import time
from functools import lru_cache
from pathlib import Path

from .config_model import LocalFilesConfig
from .utils.name_utils import fold_diacritics

logger = logging.getLogger(__name__.split(".")[-1])


_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def fold_for_match(value: Optional[str]) -> str:
    """Normalise a string for accent/case/punctuation-insensitive ``LIKE``
    matching. Applied identically to BOTH the column (via the SQL ``fold``
    function) and the query term so the two sides normalise the same way:

      * diacritic-fold   — ``Oxygene`` finds ``Oxygène``
      * casefold         — Unicode case-insensitivity. SQLite's ``LIKE`` only
        folds ASCII, so without this a lowercase Cyrillic query
        (``гребенщиков``) never matches a capitalised name (``Гребенщиков``).
      * punctuation→space — ``Jean Michel Jarre`` matches ``Jean-Michel Jarre``.
    """
    folded = fold_diacritics(value or "").casefold()
    folded = _NON_WORD_RE.sub(" ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def _sql_fold(value: Optional[str]) -> str:
    """SQLite-callable match-fold (see :func:`fold_for_match`). Registered as a
    deterministic ``fold`` function on every connection."""
    return fold_for_match(value)


# Tag writers separate multiple genres with any of these.
_GENRE_SPLIT_RE = re.compile(r"[,;/|]")


def genre_parts(value: Optional[str]) -> List[str]:
    """The genres a tag string names, as written."""
    if not value:
        return []
    return [part.strip() for part in _GENRE_SPLIT_RE.split(value) if part.strip()]


def split_genres(value: Optional[str]) -> List[str]:
    """[genre_parts] folded to their match form.

    One definition serves both the vocabulary and the matching, so a value the
    filter list offers is always a value the filter can find.
    """
    parts = (fold_for_match(part) for part in genre_parts(value))
    return [part for part in parts if part]


def _sql_has_genre(value: Optional[str], wanted: str) -> int:
    """SQLite-callable ``genre`` membership, registered as ``has_genre``.
    A substring ``LIKE`` would match ``rock`` inside ``rocksteady``."""
    return 1 if wanted in split_genres(value) else 0


@dataclass(frozen=True)
class _KindQuery:
    """How one entity kind is listed.

    One description drives that kind's own page, its count, and its slice of
    the library's mixed listing, so the three can never disagree about what
    the kind is or what narrows it.

    ``genre_predicate`` holds exactly one ``?`` testing a single genre against
    the row; a kind that carries no genre still consumes the parameter and
    matches nothing.
    """

    columns: str
    source: str
    id_expr: str
    ts_expr: str
    text_fields: Tuple[str, ...]
    genre_predicate: str


#: Artist row plus the albums credited to them, counted in the same statement
#: so a page of artists costs one query rather than one per artist. Requires
#: the row source to alias ``artists`` as ``ar``.
ARTIST_COLUMNS = (
    "ar.*, (SELECT COUNT(*) FROM albums al WHERE al.artist_id = ar.id)"
    " AS album_count"
)

KINDS: Dict[str, _KindQuery] = {
    "track": _KindQuery(
        columns=(
            "t.*, a.title as album_title, a.genre as album_genre,"
            " ar.name as artist_name"
        ),
        source=(
            "tracks t JOIN albums a ON t.album_id = a.id"
            " JOIN artists ar ON t.artist_id = ar.id"
        ),
        id_expr="t.id",
        ts_expr="t.last_updated",
        text_fields=("fold(t.title)", "fold(a.title)", "fold(ar.name)"),
        genre_predicate="has_genre(a.genre, ?)",
    ),
    "album": _KindQuery(
        columns="a.*, ar.name as artist_name",
        source="albums a JOIN artists ar ON a.artist_id = ar.id",
        id_expr="a.id",
        ts_expr="a.last_updated",
        text_fields=("fold(a.title)", "fold(ar.name)"),
        genre_predicate="has_genre(a.genre, ?)",
    ),
    "artist": _KindQuery(
        columns=ARTIST_COLUMNS,
        source="artists ar",
        id_expr="ar.id",
        ts_expr="ar.last_updated",
        text_fields=("fold(ar.name)",),
        # An artist carries no genre of its own: theirs is their albums'.
        genre_predicate=(
            "EXISTS (SELECT 1 FROM albums alg"
            " WHERE alg.artist_id = ar.id AND has_genre(alg.genre, ?))"
        ),
    ),
    "playlist": _KindQuery(
        columns="p.*",
        source="playlists p",
        id_expr="p.id",
        ts_expr="p.last_updated",
        text_fields=("fold(p.name)", "fold(p.description)"),
        genre_predicate="has_genre(NULL, ?)",
    ),
}


def folder_prefix(folder: str) -> str:
    """``folder`` as the start every path inside it shares.

    @note A root that already ends in the separator — ``/`` — keeps the one
        it has, or nothing would lie inside it.
    """
    return folder if folder.endswith("/") else folder + "/"


@dataclass(frozen=True)
class FolderSummary:
    """What a folder holds: its direct children counted, and every track
    beneath it however deep."""

    direct_folders: int
    direct_tracks: int
    direct_duration: int
    total_tracks: int


@dataclass(frozen=True)
class _FolderRange:
    """The tracks under a folder as a range of ``tracks.file_path``.

    A range rather than ``LIKE``, so a ``%`` or ``_`` in a folder name is
    literal and the index serves it: everything under ``/a/`` sorts after
    ``/a/`` and before ``/a0``, ``0`` being the character after ``/``.
    ``tail_start`` is where a path's part below the folder begins, counted
    from 1 in characters as ``substr`` counts them.
    """

    lo: str
    hi: str
    tail_start: int

    @classmethod
    def of(cls, folder: str) -> "_FolderRange":
        prefix = folder_prefix(folder)
        return cls(lo=prefix, hi=prefix[:-1] + "0", tail_start=len(prefix) + 1)

    def params(self, **extra: Any) -> Dict[str, Any]:
        return {"lo": self.lo, "hi": self.hi, "tail_start": self.tail_start, **extra}


@lru_cache(maxsize=4096)
def _name_order(name: str) -> Tuple[str, str]:
    """How a folder listing orders a name: regardless of case, then as is.
    Cached, since a walk meets each folder's name once per track in it."""
    return fold_for_match(name), name


def _listing_order(rest: str) -> Tuple[Tuple[int, str, str], ...]:
    """Where a path below a folder comes as the folder listings show it,
    read down through its subfolders: each folder's subfolders before its own
    tracks, both by name regardless of case."""
    *folders, name = rest.split("/")
    return (
        *((0, *_name_order(part)) for part in folders),
        (1, *_name_order(name)),
    )


#: A folder holding at most this many tracks is ordered in one go; a larger
#: one is walked a subfolder at a time, so a walk that stops at its limit
#: reads about that many tracks rather than the whole of a large library.
_ORDERED_IN_ONE = 2000


#: Each track under the folder, as its path below it.
_BELOW = """
    below AS (
        SELECT substr(t.file_path, :tail_start) AS rest, t.file_path AS path,
               t.duration AS duration
        FROM tracks t
        WHERE t.file_path > :lo AND t.file_path < :hi
    )"""

#: Each track inside a subfolder, by that subfolder's name and its path
#: below it.
_NESTED = """
    nested AS (
        SELECT substr(rest, 1, instr(rest, '/') - 1) AS name,
               substr(rest, instr(rest, '/') + 1) AS deeper, duration
        FROM below WHERE instr(rest, '/') > 0
    )"""

#: Each track under the folder that shows a cover, the album's before its own.
_COVERS = """
    covers AS (
        SELECT substr(t.file_path, :tail_start) AS rest, t.file_path AS path,
               CASE WHEN NULLIF(a.image_url, '') IS NOT NULL
                    THEN 'album' ELSE 'track' END AS kind,
               COALESCE(NULLIF(a.image_url, ''), NULLIF(t.image_url, ''))
                   AS image
        FROM tracks t JOIN albums a ON t.album_id = a.id
        WHERE t.file_path > :lo AND t.file_path < :hi
    )"""


def _summary_columns(rest: str) -> str:
    """A :class:`FolderSummary` as aggregates over ``rest``, each track's path
    below the folder summarised."""
    direct = f"instr({rest}, '/') = 0"
    return f"""
        COUNT(*) AS total_tracks,
        COALESCE(SUM({direct}), 0) AS direct_tracks,
        COALESCE(SUM(CASE WHEN {direct} THEN duration END), 0)
            AS direct_duration,
        COUNT(DISTINCT CASE WHEN NOT {direct}
            THEN substr({rest}, 1, instr({rest}, '/') - 1) END)
            AS direct_folders"""


def _summary_of(row: sqlite3.Row) -> FolderSummary:
    return FolderSummary(
        direct_folders=row["direct_folders"],
        direct_tracks=row["direct_tracks"],
        direct_duration=row["direct_duration"],
        total_tracks=row["total_tracks"],
    )


def _materialised(
    cursor: sqlite3.Cursor, page: List[Tuple[str, str]]
) -> List[Tuple[str, Dict]]:
    """The full rows behind a page of ``(kind, id)``, in the page's order, one
    query per kind. An id whose row has gone since the page was cut is left
    out."""
    rows: Dict[Tuple[str, str], Dict] = {}
    for kind in dict.fromkeys(kind for kind, _ in page):
        ids = [id for k, id in page if k == kind]
        query = KINDS[kind]
        placeholders = ",".join("?" for _ in ids)
        cursor.execute(
            f"SELECT {query.columns} FROM {query.source}"
            f" WHERE {query.id_expr} IN ({placeholders})",
            ids,
        )
        for row in cursor.fetchall():
            rows[(kind, row["id"])] = dict(row)
    return [(kind, rows[(kind, id)]) for kind, id in page if (kind, id) in rows]


def _image_id(image_url: str) -> str:
    return image_url[: -len(".jpg")] if image_url.endswith(".jpg") else image_url


@dataclass(frozen=True)
class ListingFilter:
    """What a browse asked a listing to narrow to.

    Stated in this layer's own terms rather than the SDK's, so the query layer
    stays a query layer and the module translates once. Genre values are match
    forms (see :func:`split_genres`).
    """

    text: str = ""
    genre_any: Tuple[str, ...] = ()
    genre_all: Tuple[str, ...] = ()
    genre_none: Tuple[str, ...] = ()


class LocalFilesInputModuleDb:
    """
    Database manager specifically for the LocalFilesInputModule.
    Handles read-only operations for browsing and retrieving music.
    """

    def __init__(self, config: LocalFilesConfig):
        self.db_path = Path(config.db_path).expanduser().resolve()
        self.artwork_path = Path(config.artwork_path).expanduser().resolve()
        self.db_state = None

    def purge_all(self):
        """Wipe everything derived from the library so the index is rebuilt
        from scratch, preserving finished CLAP audio embeddings.

        An audio embedding depends only on the file's bytes and the model,
        and track IDs are stable path hashes — so finished blobs are stashed
        in ``embedding_snapshot`` before the tables are cleared, and the
        embedder re-attaches them to re-indexed tracks instead of re-running
        the audio model (see ``AsyncEmbedderDb.restore_snapshot``). If the
        selective wipe fails (foreign schema, undroppable vec tables) the DB
        file is removed instead — correct, just recomputes the embeddings.

        Used by the "Rebuild library on next restart" one-shot. Call before
        any worker opens the DB (i.e. early in setup), so nothing recreates
        the tables mid-purge.
        """
        if not self._clear_database_keeping_embeddings():
            self._purge_database()
        self._purge_artwork()

    def _clear_database_keeping_embeddings(self) -> bool:
        """Snapshot audio embeddings, then empty every other table. Returns
        False when the DB can't be cleared in place (caller falls back to
        deleting the file)."""
        if not self.db_path.exists():
            logger.info("No existing database to purge.")
            return True
        try:
            conn = self._get_connection()
            try:
                self._drop_vec_tables(conn)
                conn.execute("DROP TABLE IF EXISTS embedding_snapshot")
                conn.execute(
                    """
                    CREATE TABLE embedding_snapshot AS
                    SELECT id AS track_id, embedding_clap_audio,
                           embedding_version, embedded_at,
                           mood_valence, mood_arousal,
                           file_size, modified_time
                    FROM tracks
                    WHERE embedding_clap_audio IS NOT NULL
                    """
                )
                tables = conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table'
                      AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'
                      AND name NOT LIKE 'vec\\_%' ESCAPE '\\'
                      AND name != 'embedding_snapshot'
                    """
                ).fetchall()
                for (table,) in tables:
                    conn.execute(f'DELETE FROM "{table}"')
                conn.commit()
                snapshot = conn.execute(
                    "SELECT COUNT(*) FROM embedding_snapshot"
                ).fetchone()[0]
            finally:
                conn.close()
        except Exception as e:
            logger.warning(
                f"In-place purge failed ({e}); removing the database file "
                "instead (embeddings will be recomputed)."
            )
            return False
        logger.info(
            f"Database cleared for rebuild; {snapshot} audio embedding(s) "
            "snapshotted for reuse."
        )
        return True

    def _drop_vec_tables(self, conn: sqlite3.Connection) -> None:
        """Drop the sqlite-vec virtual tables (schema init recreates them
        empty). Track vectors are re-derived from the snapshotted blobs, and
        text/aggregate vectors are recomputed, so a clean slate here also
        sheds rows for entities that no longer exist."""
        vec_tables = [
            row[0]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name LIKE 'vec\\_%' ESCAPE '\\'
                  AND sql LIKE 'CREATE VIRTUAL TABLE%'
                """
            ).fetchall()
        ]
        if not vec_tables:
            return
        import sqlite_vec

        conn.enable_load_extension(True)
        try:
            conn.load_extension(sqlite_vec.loadable_path())
        finally:
            conn.enable_load_extension(False)
        for table in vec_tables:
            conn.execute(f'DROP TABLE "{table}"')

    def _purge_database(self):
        """Remove the database file and its WAL/-shm sidecars."""
        removed = False
        for sidecar in ("", "-wal", "-shm"):
            path = (
                self.db_path
                if not sidecar
                else self.db_path.with_name(self.db_path.name + sidecar)
            )
            if path.exists():
                try:
                    path.unlink()
                    removed = True
                    logger.info(f"Removed {path}")
                except OSError as e:
                    logger.error(f"Failed to remove {path}: {e}")
        if not removed:
            logger.info("No existing database to purge.")

    def _purge_artwork(self):
        """Purge the artwork directory by removing all files and folders."""
        if self.artwork_path.exists() and self.artwork_path.is_dir():
            try:
                shutil.rmtree(self.artwork_path)
                logger.info(f"Artwork directory purged: {self.artwork_path}")
            except OSError as e:
                logger.error(f"Failed to purge artwork directory: {e}")
        else:
            logger.info("No existing artwork directory to purge.")

    def _get_connection(self):
        """Get a database connection with row factory"""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.create_function("fold", 1, _sql_fold, deterministic=True)
        conn.create_function("has_genre", 2, _sql_has_genre, deterministic=True)
        return conn

    def _is_database_functional(self) -> Dict[str, Any]:
        """
        Check if the database exists and is properly structured.

        Returns:
            A dictionary with status information:
            {
                'exists': bool - Whether the database file exists
                'can_connect': bool - Whether we can connect to the database
                'has_tables': bool - Whether essential tables exist
                'functional': bool - Whether the database is fully functional
                'missing_tables': List[str] - List of essential tables that are missing (if any)
                'error': str - Error message (if any)
            }
        """
        result = {
            "exists": False,
            "can_connect": False,
            "has_tables": False,
            "functional": False,
            "missing_tables": [],
            "error": None,
        }

        # Check if database file exists
        if not os.path.isfile(self.db_path):
            result["error"] = f"Database file not found: {self.db_path}"
            return result

        result["exists"] = True

        # Check database connection
        try:
            conn = self._get_connection()
            result["can_connect"] = True
        except sqlite3.Error as e:
            result["error"] = f"Cannot connect to database: {str(e)}"
            return result

        # Check essential tables
        try:
            cursor = conn.cursor()

            # List of essential tables that should be present
            essential_tables = [
                "tracks",
                "albums",
                "artists",
                "playlists",
                "playlist_tracks",
            ]

            # Get list of tables in the database
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            existing_tables = {row["name"] for row in cursor.fetchall()}

            # Check if all essential tables exist
            for table in essential_tables:
                if table not in existing_tables:
                    result["missing_tables"].append(table)

            if not result["missing_tables"]:
                result["has_tables"] = True
            else:
                result["error"] = (
                    f"Missing tables: {', '.join(result['missing_tables'])}"
                )
        except sqlite3.Error as e:
            result["error"] = f"Error checking tables: {str(e)}"
            return result
        finally:
            conn.close()

        # Database is functional if it exists, can connect, and has all essential tables
        result["functional"] = (
            result["exists"] and result["can_connect"] and result["has_tables"]
        )

        return result

    def is_good(self) -> bool:
        """
        Check if the database is functional.
        Returns True if the database is functional, False otherwise.
        """
        if not os.path.isfile(self.db_path):
            self.db_state = None
            return False

        if not self.db_state or not self.db_state.get("functional", False):
            self.db_state = self._is_database_functional()

        return self.db_state.get("functional", False)

    def get_track_by_id(self, track_id: str) -> Optional[Dict]:
        """Get track information by ID"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT t.*, a.title as album_title, a.genre as album_genre, ar.name as artist_name 
                FROM tracks t
                LEFT JOIN albums a ON t.album_id = a.id
                LEFT JOIN artists ar ON t.artist_id = ar.id
                WHERE t.id = ?
            """,
                (track_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_root_signature(self, root: str) -> Optional[str]:
        """The mount identity the indexer recorded for a music root, or None.
        Lets the playback path tell an unmounted static share (identity
        changed) from a genuinely missing file."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT value FROM indexer_state WHERE key = ?",
                (f"root_signature:{root}",),
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] else None
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def get_tracks_by_ids(self, track_ids: List[str]) -> List[Dict]:
        """Get track information by IDs, preserving the order of track_ids."""
        if not track_ids:
            return []

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            placeholders = ", ".join("?" for _ in track_ids)
            cursor.execute(
                f"""
                SELECT t.*, a.title as album_title, a.genre as album_genre, ar.name as artist_name
                FROM tracks t
                LEFT JOIN albums a ON t.album_id = a.id
                LEFT JOIN artists ar ON t.artist_id = ar.id
                WHERE t.id IN ({placeholders})
            """,
                track_ids,
            )
            rows_by_id = {row["id"]: dict(row) for row in cursor.fetchall()}
            return [rows_by_id[tid] for tid in track_ids if tid in rows_by_id]
        finally:
            conn.close()

    def get_albums_by_ids(self, album_ids: List[str]) -> List[Dict]:
        """Get album information by IDs, preserving the order of album_ids."""
        if not album_ids:
            return []

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            placeholders = ", ".join("?" for _ in album_ids)
            cursor.execute(
                f"""
                SELECT a.*, ar.name as artist_name
                FROM albums a
                LEFT JOIN artists ar ON a.artist_id = ar.id
                WHERE a.id IN ({placeholders})
                """,
                album_ids,
            )
            rows_by_id = {row["id"]: dict(row) for row in cursor.fetchall()}
            return [rows_by_id[aid] for aid in album_ids if aid in rows_by_id]
        finally:
            conn.close()

    def get_artists_by_ids(self, artist_ids: List[str]) -> List[Dict]:
        """Get artist information by IDs, preserving the order of artist_ids."""
        if not artist_ids:
            return []

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            placeholders = ", ".join("?" for _ in artist_ids)
            cursor.execute(
                f"SELECT {ARTIST_COLUMNS} FROM artists ar"
                f" WHERE ar.id IN ({placeholders})",
                artist_ids,
            )
            rows_by_id = {row["id"]: dict(row) for row in cursor.fetchall()}
            return [rows_by_id[aid] for aid in artist_ids if aid in rows_by_id]
        finally:
            conn.close()

    def get_playlists_by_ids(self, playlist_ids: List[str]) -> List[Dict]:
        """Get playlist information by IDs, preserving the order of playlist_ids."""
        if not playlist_ids:
            return []

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            placeholders = ", ".join("?" for _ in playlist_ids)
            cursor.execute(
                f"SELECT * FROM playlists WHERE id IN ({placeholders})",
                playlist_ids,
            )
            rows_by_id = {row["id"]: dict(row) for row in cursor.fetchall()}
            return [rows_by_id[pid] for pid in playlist_ids if pid in rows_by_id]
        finally:
            conn.close()

    def get_album_by_id(self, album_id: str) -> Optional[Dict]:
        """Get album information by ID"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT a.*, ar.name as artist_name
                FROM albums a
                LEFT JOIN artists ar ON a.artist_id = ar.id
                WHERE a.id = ?
            """,
                (album_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_artist_by_id(self, artist_id: str) -> Optional[Dict]:
        """Get artist information by ID"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT {ARTIST_COLUMNS} FROM artists ar WHERE ar.id = ?",
                (artist_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    @staticmethod
    def _token_where(query: str, fields: Tuple[str, ...]) -> Tuple[str, List[str]]:
        """WHERE clause requiring every folded query token to match at least
        one of ``fields`` (fold()-wrapped SQL expressions).

        Per-token matching lets a query span fields — "come together beatles"
        is title + artist, which a single whole-query LIKE can never match.
        A single-token query behaves exactly like the old whole-string scan."""
        tokens = fold_for_match(query).split()
        if not tokens:
            # Preserve the old empty-query behavior: match everything.
            return "1=1", []
        clause = " AND ".join(
            "(" + " OR ".join(f"{f} LIKE ?" for f in fields) + ")"
            for _ in tokens
        )
        params = [f"%{t}%" for t in tokens for _ in fields]
        return clause, params

    @staticmethod
    def _genre_where(filter: ListingFilter, predicate: str) -> Tuple[str, List[str]]:
        """WHERE clause for the genre part of ``filter``.

        ``predicate`` is a SQL fragment holding one ``?`` that tests a single
        genre against the row — ``has_genre(a.genre, ?)`` for a listing that
        joins albums, an EXISTS over albums for one that does not."""
        clauses: List[str] = []
        params: List[str] = []
        if filter.genre_any:
            clauses.append(
                "(" + " OR ".join(predicate for _ in filter.genre_any) + ")"
            )
            params.extend(filter.genre_any)
        for genre in filter.genre_all:
            clauses.append(predicate)
            params.append(genre)
        if filter.genre_none:
            clauses.append(
                "NOT (" + " OR ".join(predicate for _ in filter.genre_none) + ")"
            )
            params.extend(filter.genre_none)
        if not clauses:
            return "1=1", []
        return " AND ".join(clauses), params

    @classmethod
    def _listing_where(
        cls,
        filter: ListingFilter,
        text_fields: Tuple[str, ...],
        genre_predicate: str,
    ) -> Tuple[str, List[str]]:
        """WHERE clause narrowing a listing to ``filter``: every constraint
        must hold, which is what makes a filtered listing's COUNT the same
        query as its page."""
        text_clause, params = cls._token_where(filter.text, text_fields)
        genre_clause, genre_params = cls._genre_where(filter, genre_predicate)
        return f"({text_clause}) AND ({genre_clause})", params + genre_params

    def list_album_genres(
        self, offset: int = 0, limit: int = 50, q: str = ""
    ) -> Tuple[List[Tuple[str, str, int]], int]:
        """The library's own genre vocabulary: (match form, display name, album
        count), most albums first.

        Uncurated by design — the values are the user's tags, so ``Electro`` and
        ``Electronic`` stay two entries. The display name is the spelling most
        albums use."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT genre FROM albums WHERE genre IS NOT NULL AND genre != ''"
            )
            counts: Dict[str, int] = {}
            spellings: Dict[str, Dict[str, int]] = {}
            for row in cursor.fetchall():
                raw_parts = _GENRE_SPLIT_RE.split(row["genre"])
                for raw in raw_parts:
                    folded = fold_for_match(raw)
                    if not folded:
                        continue
                    counts[folded] = counts.get(folded, 0) + 1
                    seen = spellings.setdefault(folded, {})
                    display = raw.strip()
                    seen[display] = seen.get(display, 0) + 1
        finally:
            conn.close()

        needle = fold_for_match(q)
        values = [
            (folded, max(spellings[folded].items(), key=lambda kv: kv[1])[0], count)
            for folded, count in counts.items()
            if not needle or needle in folded
        ]
        values.sort(key=lambda value: (-value[2], value[0]))
        return values[offset : offset + limit], len(values)

    def search_tracks(
        self, query: str, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Dict], int]:
        """Search tracks by query (every token must match title/album/artist)"""
        where, params = self._token_where(
            query, ("fold(t.title)", "fold(a.title)", "fold(ar.name)")
        )
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # Get total count
            cursor.execute(
                f"""
                SELECT COUNT(*) as count FROM tracks t
                JOIN albums a ON t.album_id = a.id
                JOIN artists ar ON t.artist_id = ar.id
                WHERE {where}
            """,
                params,
            )
            total = cursor.fetchone()["count"]

            # Get results
            cursor.execute(
                f"""
                SELECT t.*, a.title as album_title, a.genre as album_genre, ar.name as artist_name
                FROM tracks t
                JOIN albums a ON t.album_id = a.id
                JOIN artists ar ON t.artist_id = ar.id
                WHERE {where}
                ORDER BY t.title
                LIMIT ? OFFSET ?
            """,
                (*params, limit, offset),
            )

            return [dict(row) for row in cursor.fetchall()], total
        finally:
            conn.close()

    def search_albums(
        self, query: str, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Dict], int]:
        """Search albums by query (every token must match title/artist)"""
        where, params = self._token_where(
            query, ("fold(a.title)", "fold(ar.name)")
        )
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # Get total count
            cursor.execute(
                f"""
                SELECT COUNT(*) as count FROM albums a
                JOIN artists ar ON a.artist_id = ar.id
                WHERE {where}
            """,
                params,
            )
            total = cursor.fetchone()["count"]

            # Get results
            cursor.execute(
                f"""
                SELECT a.*, ar.name as artist_name
                FROM albums a
                JOIN artists ar ON a.artist_id = ar.id
                WHERE {where}
                ORDER BY a.title
                LIMIT ? OFFSET ?
            """,
                (*params, limit, offset),
            )

            return [dict(row) for row in cursor.fetchall()], total
        finally:
            conn.close()

    def search_artists(
        self, query: str, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Dict], int]:
        """Search artists by query"""
        where, params = self._token_where(query, ("fold(name)",))
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # Get total count
            cursor.execute(
                f"SELECT COUNT(*) as count FROM artists WHERE {where}",
                params,
            )
            total = cursor.fetchone()["count"]

            # Get results
            cursor.execute(
                f"""
                SELECT {ARTIST_COLUMNS} FROM artists ar
                WHERE {where}
                ORDER BY name
                LIMIT ? OFFSET ?
            """,
                (*params, limit, offset),
            )

            return [dict(row) for row in cursor.fetchall()], total
        finally:
            conn.close()

    def list_kind(
        self,
        kind: str,
        offset: int = 0,
        limit: int = 50,
        filter: Optional[ListingFilter] = None,
    ) -> Tuple[List[Dict], int]:
        """One page of a single kind's listing, and how many it has in all.

        The page and the count come from one WHERE, which is what keeps
        pagination meaningful under a filter. Ties on the timestamp are broken
        by id so a row cannot cross pages between requests.
        """
        query = KINDS[kind]
        where, params = self._listing_where(
            filter or ListingFilter(), query.text_fields, query.genre_predicate
        )
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT COUNT(*) as count FROM {query.source} WHERE {where}",
                params,
            )
            total = cursor.fetchone()["count"]

            cursor.execute(
                f"""
                SELECT {query.columns} FROM {query.source}
                WHERE {where}
                ORDER BY {query.ts_expr} DESC, {query.id_expr}
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            )
            return [dict(row) for row in cursor.fetchall()], total
        finally:
            conn.close()

    def get_kind_total(self, kind: str, filter: Optional[ListingFilter] = None) -> int:
        """How many of ``kind`` the filter leaves, fetching no rows."""
        return self.list_kind(kind, 0, 0, filter)[1]

    def get_library_listing(
        self,
        kinds: Tuple[str, ...] = (),
        offset: int = 0,
        limit: int = 50,
        filter: Optional[ListingFilter] = None,
    ) -> Tuple[List[Tuple[str, Dict]], int]:
        """The library as one listing: every kind in ``kinds`` interleaved by
        recency, newest first, as ``(kind, row)`` pairs. Empty ``kinds`` means
        all of them.

        Each kind is narrowed by the same filter in its own terms, so one text
        query reaches a track's title, an album's title and an artist's name at
        once. Ordering across kinds needs no full rows, so identity is paged
        first and only the page is materialised.
        """
        wanted = tuple(kinds) or tuple(KINDS)
        filter = filter or ListingFilter()

        branches: List[str] = []
        params: List[Any] = []
        for kind in wanted:
            query = KINDS[kind]
            where, where_params = self._listing_where(
                filter, query.text_fields, query.genre_predicate
            )
            branches.append(
                f"SELECT ? AS kind, {query.id_expr} AS id,"
                f" {query.ts_expr} AS ts FROM {query.source} WHERE {where}"
            )
            params.append(kind)
            params.extend(where_params)
        union = " UNION ALL ".join(branches)

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) as count FROM ({union})", params)
            total = cursor.fetchone()["count"]

            cursor.execute(
                f"{union} ORDER BY ts DESC, kind, id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            page = [(row["kind"], row["id"]) for row in cursor.fetchall()]
            return _materialised(cursor, page), total
        finally:
            conn.close()

    def list_recently_added(
        self, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Tuple[str, Dict]], int]:
        """One page of what arrived in the library most recently, newest
        first, as ``(kind, row)`` pairs: each album as one entry dated by its
        first file, and each track that belongs to no album on its own.

        Dated by when a file was first indexed, which a re-tag or a move does
        not change, so an album the enricher touched does not resurface.
        """
        arrivals = """
            SELECT 'album' AS kind, t.album_id AS id,
                   MIN(lf.first_indexed) AS added
            FROM tracks t JOIN library_file lf ON lf.file_id = t.id
            WHERE t.album_id != 'unknown_album'
            GROUP BY t.album_id
            UNION ALL
            SELECT 'track', t.id, lf.first_indexed
            FROM tracks t JOIN library_file lf ON lf.file_id = t.id
            WHERE t.album_id = 'unknown_album'
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) AS count FROM ({arrivals})")
            total = cursor.fetchone()["count"]
            cursor.execute(
                f"{arrivals} ORDER BY added DESC, kind, id LIMIT ? OFFSET ?",
                (limit, offset),
            )
            page = [(row["kind"], row["id"]) for row in cursor.fetchall()]
            return _materialised(cursor, page), total
        finally:
            conn.close()

    def get_folder_summary(self, folder: str) -> FolderSummary:
        """What ``folder`` holds, from the tracks indexed under it."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                f"WITH {_BELOW} SELECT {_summary_columns('rest')} FROM below",
                _FolderRange.of(folder).params(),
            ).fetchone()
            return _summary_of(row)
        finally:
            conn.close()

    def list_subfolders(
        self, folder: str, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Tuple[str, FolderSummary]], int]:
        """One page of the folders directly in ``folder`` that hold indexed
        tracks, by name regardless of case, each with what it holds; and how
        many there are.

        One grouped pass over the folder's tracks answers every row's summary,
        so a page costs the same however many folders it shows.
        """
        params = _FolderRange.of(folder).params(limit=limit, offset=offset)
        conn = self._get_connection()
        try:
            total = conn.execute(
                f"WITH {_BELOW}, {_NESTED} SELECT COUNT(DISTINCT name) FROM nested",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                WITH {_BELOW}, {_NESTED}
                SELECT name, {_summary_columns('deeper')}
                FROM nested
                GROUP BY name
                ORDER BY fold(name), name
                LIMIT :limit OFFSET :offset
                """,
                params,
            ).fetchall()
            return [(row["name"], _summary_of(row)) for row in rows], total
        finally:
            conn.close()

    def list_track_ids_under(self, folder: str, limit: int) -> List[str]:
        """Up to ``limit`` tracks below ``folder``, subfolders included, in
        the order the folder listings show them.

        That order is per path segment, which SQL cannot sort by, so tracks
        are ordered here; see :data:`_ORDERED_IN_ONE` for how many at once.
        """
        conn = self._get_connection()
        try:
            return self._track_ids_under(conn, folder, limit)
        finally:
            conn.close()

    def _track_ids_under(
        self, conn: sqlite3.Connection, folder: str, limit: int
    ) -> List[str]:
        span = _FolderRange.of(folder)
        below = "FROM tracks t WHERE t.file_path > :lo AND t.file_path < :hi"
        count = conn.execute(f"SELECT COUNT(*) {below}", span.params()).fetchone()[0]
        if count <= _ORDERED_IN_ONE:
            rows = conn.execute(
                f"SELECT t.id AS id, substr(t.file_path, :tail_start) AS rest {below}",
                span.params(),
            ).fetchall()
            ordered = sorted(rows, key=lambda row: _listing_order(row["rest"]))
            return [row["id"] for row in ordered[:limit]]

        ids: List[str] = []
        names = conn.execute(
            f"""
            WITH {_BELOW}
            SELECT DISTINCT substr(rest, 1, instr(rest, '/') - 1) AS name
            FROM below WHERE instr(rest, '/') > 0
            """,
            span.params(),
        ).fetchall()
        prefix = folder_prefix(folder)
        for name in sorted((row["name"] for row in names), key=_name_order):
            if len(ids) >= limit:
                return ids
            ids += self._track_ids_under(conn, prefix + name, limit - len(ids))
        if len(ids) < limit:
            own = conn.execute(
                f"SELECT t.id AS id, substr(t.file_path, :tail_start) AS rest {below}"
                " AND instr(substr(t.file_path, :tail_start), '/') = 0",
                span.params(),
            ).fetchall()
            ordered = sorted(own, key=lambda row: _name_order(row["rest"]))
            ids += [row["id"] for row in ordered[: limit - len(ids)]]
        return ids

    def list_folder_tracks(
        self, folder: str, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Dict], int]:
        """One page of the tracks directly in ``folder``, by file name
        regardless of case; and how many there are."""
        query = KINDS["track"]
        where = (
            "t.file_path > :lo AND t.file_path < :hi"
            " AND instr(substr(t.file_path, :tail_start), '/') = 0"
        )
        params = _FolderRange.of(folder).params(limit=limit, offset=offset)
        conn = self._get_connection()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM {query.source} WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT {query.columns} FROM {query.source}
                WHERE {where}
                ORDER BY fold(substr(t.file_path, :tail_start)), t.file_path
                LIMIT :limit OFFSET :offset
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows], total
        finally:
            conn.close()

    def get_folder_covers(
        self, folder: str, per_folder: int
    ) -> List[Tuple[str, str]]:
        """The first ``per_folder`` distinct covers under ``folder`` in path
        order, as ``(kind, image id)`` where kind is ``album`` or ``track``."""
        return self._covers(folder, "''", "1", (), per_folder).get("", [])

    def get_subfolder_covers(
        self, folder: str, names: Sequence[str], per_folder: int
    ) -> Dict[str, List[Tuple[str, str]]]:
        """:meth:`get_folder_covers` for each of the folders ``names`` directly
        in ``folder``, in one query."""
        if not names:
            return {}
        return self._covers(
            folder,
            "substr(rest, 1, instr(rest, '/') - 1)",
            "instr(rest, '/') > 0",
            names,
            per_folder,
        )

    def _covers(
        self,
        folder: str,
        name_expr: str,
        inside: str,
        names: Sequence[str],
        per_folder: int,
    ) -> Dict[str, List[Tuple[str, str]]]:
        wanted = {f"n{index}": name for index, name in enumerate(names)}
        among = (
            f" AND {name_expr} IN ({', '.join(':' + key for key in wanted)})"
            if wanted
            else ""
        )
        conn = self._get_connection()
        try:
            rows = conn.execute(
                f"""
                WITH {_COVERS},
                distinct_covers AS (
                    SELECT {name_expr} AS name, kind, image, MIN(path) AS first_path
                    FROM covers
                    WHERE image IS NOT NULL AND {inside}{among}
                    GROUP BY name, kind, image
                ),
                ranked AS (
                    SELECT name, kind, image, first_path, ROW_NUMBER() OVER (
                        PARTITION BY name ORDER BY first_path
                    ) AS rank
                    FROM distinct_covers
                )
                SELECT name, kind, image FROM ranked
                WHERE rank <= :per_folder
                ORDER BY name, first_path
                """,
                _FolderRange.of(folder).params(per_folder=per_folder, **wanted),
            ).fetchall()
        finally:
            conn.close()
        covers: Dict[str, List[Tuple[str, str]]] = {}
        for row in rows:
            covers.setdefault(row["name"], []).append(
                (row["kind"], _image_id(row["image"]))
            )
        return covers

    def get_album_tracks(
        self, album_id: str, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Dict], int]:
        """Get tracks for an album.

        The "Unknown Album" sentinel only owns tracks whose artist is also
        unknown: a loose track WITH a known artist already surfaces under
        that artist (``get_artist_orphan_tracks``), so listing it here too
        would show the same track in two places in the library."""
        artist_cond = (
            " AND t.artist_id = 'unknown_artist'"
            if album_id == "unknown_album" else ""
        )
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # Get total count
            cursor.execute(
                "SELECT COUNT(*) as count FROM tracks t WHERE t.album_id = ?"
                + artist_cond,
                (album_id,),
            )
            total = cursor.fetchone()["count"]

            # Get results
            cursor.execute(
                """
                SELECT t.*, a.title as album_title, a.genre as album_genre, ar.name as artist_name
                FROM tracks t
                JOIN albums a ON t.album_id = a.id
                JOIN artists ar ON t.artist_id = ar.id
                WHERE t.album_id = ?
                """
                + artist_cond
                + """
                ORDER BY COALESCE(t.disc_number, 1), t.track_number, t.title
                LIMIT ? OFFSET ?
            """,
                (album_id, limit, offset),
            )

            return [dict(row) for row in cursor.fetchall()], total
        finally:
            conn.close()

    def get_artist_albums(
        self, artist_id: str, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Dict], int]:
        """Get albums for an artist"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # Get total count
            cursor.execute(
                "SELECT COUNT(*) as count FROM albums WHERE artist_id = ?", (artist_id,)
            )
            total = cursor.fetchone()["count"]

            # Get results
            cursor.execute(
                """
                SELECT a.*, ar.name as artist_name
                FROM albums a
                JOIN artists ar ON a.artist_id = ar.id
                WHERE a.artist_id = ?
                ORDER BY a.title
                LIMIT ? OFFSET ?
            """,
                (artist_id, limit, offset),
            )

            return [dict(row) for row in cursor.fetchall()], total
        finally:
            conn.close()

    def get_artist_orphan_tracks(
        self, artist_id: str, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Dict], int]:
        """Get tracks attributed to an artist whose album is anchored
        elsewhere, so they wouldn't show up under ``get_artist_albums``.

        Two real cases this catches:

          * ``album_id == 'unknown_album'`` (anchored to
            ``unknown_artist``) — the enricher couldn't pick a release.
            Original use case.

          * ``album.artist_id == 'various_artists'`` — a V/A compilation
            the indexer coalesced. The track's ``artist_id`` correctly
            points at the real artist, but the album is anchored to
            the V/A sentinel, so it never surfaces in the
            albums-by-artist query. Without this, an artist with only
            tracks-on-compilations (e.g. one of the 59 distinct artists
            on a Jamendo playlist folder) appears empty in the UI.

        Generalised to ``album.artist_id != track.artist_id`` so any
        future "track anchored under a different album-owner" case
        gets picked up too.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT COUNT(*) as count
                FROM tracks t
                JOIN albums a ON t.album_id = a.id
                WHERE t.artist_id = ? AND a.artist_id != t.artist_id
                """,
                (artist_id,),
            )
            total = cursor.fetchone()["count"]

            cursor.execute(
                """
                SELECT t.*, a.title as album_title, a.genre as album_genre, ar.name as artist_name
                FROM tracks t
                JOIN albums a ON t.album_id = a.id
                JOIN artists ar ON t.artist_id = ar.id
                WHERE t.artist_id = ? AND a.artist_id != t.artist_id
                ORDER BY t.title COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                (artist_id, limit, offset),
            )

            return [dict(row) for row in cursor.fetchall()], total
        finally:
            conn.close()

    def get_artist_recent_tracks(
        self, artist_id: str, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Dict], int]:
        """Get top tracks for an artist"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # Get total count
            cursor.execute(
                """
                SELECT COUNT(*) as count FROM tracks WHERE artist_id = ?
                """,
                (artist_id,),
            )
            total = cursor.fetchone()["count"]

            # Get results
            cursor.execute(
                """
                SELECT t.*, a.title as album_title, a.genre as album_genre, ar.name as artist_name
                FROM tracks t
                JOIN albums a ON t.album_id = a.id
                JOIN artists ar ON t.artist_id = ar.id
                WHERE t.artist_id = ?
                ORDER BY t.last_updated DESC
                LIMIT ? OFFSET ?
                """,
                (artist_id, limit, offset),
            )

            return [dict(row) for row in cursor.fetchall()], total
        finally:
            conn.close()

    # Playlist related methods
    def get_playlist_by_id(self, playlist_id: str) -> Optional[Dict]:
        """Get playlist information by ID"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_playlist_tracks(
        self, playlist_id: str, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Dict], int]:
        """Get tracks for a playlist"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # Get total count
            cursor.execute(
                """
                SELECT COUNT(*) as count 
                FROM playlist_tracks 
                WHERE playlist_id = ?
                """,
                (playlist_id,),
            )
            total = cursor.fetchone()["count"]

            # Get results
            cursor.execute(
                """
                SELECT t.*, a.title as album_title, a.genre as album_genre, ar.name as artist_name, 
                       pt.position, pt.playlist_track_id
                FROM playlist_tracks pt
                JOIN tracks t ON pt.track_id = t.id
                JOIN albums a ON t.album_id = a.id
                JOIN artists ar ON t.artist_id = ar.id
                WHERE pt.playlist_id = ?
                ORDER BY pt.position
                LIMIT ? OFFSET ?
                """,
                (playlist_id, limit, offset),
            )

            return [dict(row) for row in cursor.fetchall()], total
        finally:
            conn.close()

    def get_playlist_track_album_ids(
        self, playlist_id: str, limit: int = 4
    ) -> List[str]:
        """
        Get distinct album IDs for tracks in a playlist.
        Useful for generating playlist artwork.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT DISTINCT t.album_id
                FROM playlist_tracks pt
                JOIN tracks t ON pt.track_id = t.id
                WHERE pt.playlist_id = ?
                LIMIT ?
                """,
                (playlist_id, limit),
            )

            return [row["album_id"] for row in cursor.fetchall()]
        finally:
            conn.close()

    def search_playlists(
        self, query: str, offset: int = 0, limit: int = 50
    ) -> Tuple[List[Dict], int]:
        """Search playlists by name/description"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            search_term = f"%{fold_for_match(query)}%"

            # Get total count
            cursor.execute(
                """
                SELECT COUNT(*) as count FROM playlists
                WHERE fold(name) LIKE ? OR fold(description) LIKE ?
                """,
                (search_term, search_term),
            )
            total = cursor.fetchone()["count"]

            # Get results
            cursor.execute(
                """
                SELECT * FROM playlists
                WHERE fold(name) LIKE ? OR fold(description) LIKE ?
                ORDER BY name
                LIMIT ? OFFSET ?
                """,
                (search_term, search_term, limit, offset),
            )

            return [dict(row) for row in cursor.fetchall()], total
        finally:
            conn.close()

    def create_playlist(
        self, playlist_id: str, name: str, description: str, created_by: str
    ) -> None:
        """Create a new playlist"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            current_time = int(time.time())

            cursor.execute(
                """
                INSERT INTO playlists (
                    id, name, description, track_count, duration,
                    created_by, created_at, last_updated
                ) VALUES (?, ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    playlist_id,
                    name,
                    description,
                    created_by,
                    current_time,
                    current_time,
                ),
            )

            conn.commit()
        except Exception as e:
            logger.error(f"Error creating playlist: {str(e)}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def delete_playlist(self, playlist_id: str) -> bool:
        """Delete a playlist by ID. Returns True if successful."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            # Playlist tracks will be automatically deleted due to the ON DELETE CASCADE constraint
            cursor.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
        except Exception as e:
            logger.error(f"Error deleting playlist: {str(e)}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_playlist(
        self,
        playlist_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        """Update playlist information. Returns True if successful."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            current_time = int(time.time())

            # Build the SET clause with only provided fields
            fields = ["last_updated = ?"]
            values: List[Any] = [current_time]

            if name is not None:
                fields.append("name = ?")
                values.append(name)

            if description is not None:
                fields.append("description = ?")
                values.append(description)

            # Add playlist_id to the values
            values.append(playlist_id)

            query = f"UPDATE playlists SET {', '.join(fields)} WHERE id = ?"
            cursor.execute(query, values)
            updated = cursor.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            logger.error(f"Error updating playlist: {str(e)}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_tracks_to_playlist(
        self, playlist_id: str, track_ids: List[str], allow_duplicates: bool = False
    ) -> int:
        """
        Add tracks to a playlist.

        Args:
            playlist_id: ID of the playlist
            track_ids: List of track IDs to add
            allow_duplicates: Whether to allow duplicate tracks in the playlist

        Returns:
            Number of tracks added
        """
        if not track_ids:
            return 0

        from .utils.id_generator import generate_playlist_track_id

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            placeholders = ", ".join("?" for _ in track_ids)
            cursor.execute(
                f"SELECT id FROM tracks WHERE id IN ({placeholders})",
                track_ids,
            )

            # Get set of valid IDs from DB
            valid_id_set = {row["id"] for row in cursor.fetchall()}
            # Preserve order from input
            valid_track_ids = [
                track_id for track_id in track_ids if track_id in valid_id_set
            ]
            if not valid_track_ids:
                return 0

            current_time = int(time.time())

            # Get the next position number
            if allow_duplicates:
                cursor.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 as next_pos FROM playlist_tracks WHERE playlist_id = ?",
                    (playlist_id,),
                )
            else:
                # Remove any track IDs that are already in the playlist
                placeholders = ", ".join("?" for _ in valid_track_ids)
                cursor.execute(
                    f"""
                    SELECT track_id FROM playlist_tracks 
                    WHERE playlist_id = ? AND track_id IN ({placeholders})
                    """,
                    [playlist_id] + valid_track_ids,
                )
                existing_tracks = {row["track_id"] for row in cursor.fetchall()}
                valid_track_ids = [
                    track_id
                    for track_id in valid_track_ids
                    if track_id not in existing_tracks
                ]

                if not valid_track_ids:
                    return 0

                # Get the next position number
                cursor.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 as next_pos FROM playlist_tracks WHERE playlist_id = ?",
                    (playlist_id,),
                )

            next_pos = cursor.fetchone()["next_pos"]

            # Insert the tracks with unique playlist_track_ids
            for i, track_id in enumerate(valid_track_ids):
                playlist_track_id = generate_playlist_track_id()
                cursor.execute(
                    """
                    INSERT INTO playlist_tracks (playlist_track_id, playlist_id, track_id, position, added_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        playlist_track_id,
                        playlist_id,
                        track_id,
                        next_pos + i,
                        current_time,
                    ),
                )

            # Update the playlist's track count and duration
            self._update_playlist_stats(cursor, playlist_id)

            conn.commit()
            return len(valid_track_ids)
        except Exception as e:
            logger.error(f"Error adding tracks to playlist: {str(e)}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_playlist_image(self, playlist_id: str, image_url: str) -> bool:
        """
        Update the image path for a playlist.

        Args:
            playlist_id: ID of the playlist
            image_url: Path to the playlist image

        Returns:
            True if the update was successful, False otherwise
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            current_time = int(time.time())

            cursor.execute(
                """
                UPDATE playlists SET
                image_url = ?,
                last_updated = ?
                WHERE id = ?
                """,
                (image_url, current_time, playlist_id),
            )

            updated = cursor.rowcount > 0
            conn.commit()
            return updated
        except Exception as e:
            logger.error(f"Error updating playlist image: {str(e)}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def remove_tracks_from_playlist(
        self, playlist_id: str, playlist_track_ids: List[str]
    ) -> int:
        """
        Remove tracks from a playlist using playlist track IDs.

        Args:
            playlist_id: ID of the playlist
            playlist_track_ids: List of playlist track IDs to remove

        Returns:
            Number of tracks removed
        """
        if not playlist_track_ids:
            return 0

        from .utils.id_generator import generate_playlist_track_id

        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # Delete the tracks by playlist_track_id
            placeholders = ", ".join("?" for _ in playlist_track_ids)
            cursor.execute(
                f"""
                DELETE FROM playlist_tracks 
                WHERE playlist_id = ? AND playlist_track_id IN ({placeholders})
                """,
                [playlist_id] + playlist_track_ids,
            )

            removed_count = cursor.rowcount

            if removed_count > 0:
                # Reindex the remaining tracks to ensure positions are continuous
                cursor.execute(
                    """
                    SELECT playlist_track_id, track_id FROM playlist_tracks
                    WHERE playlist_id = ?
                    ORDER BY position
                    """,
                    (playlist_id,),
                )

                remaining_tracks = [
                    (row["playlist_track_id"], row["track_id"])
                    for row in cursor.fetchall()
                ]

                # Delete all tracks
                cursor.execute(
                    "DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
                )

                # Re-insert with new positions
                current_time = int(time.time())
                for position, (playlist_track_id, track_id) in enumerate(
                    remaining_tracks
                ):
                    cursor.execute(
                        """
                        INSERT INTO playlist_tracks (playlist_track_id, playlist_id, track_id, position, added_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            playlist_track_id,
                            playlist_id,
                            track_id,
                            position,
                            current_time,
                        ),
                    )

                # Update the playlist's track count and duration
                self._update_playlist_stats(cursor, playlist_id)

            conn.commit()
            return removed_count
        except Exception as e:
            logger.error(f"Error removing tracks from playlist: {str(e)}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def _update_playlist_stats(self, cursor, playlist_id: str) -> None:
        """Update playlist statistics (track count and duration)"""
        cursor.execute(
            """
            UPDATE playlists SET
            track_count = (
                SELECT COUNT(*) 
                FROM playlist_tracks 
                WHERE playlist_id = ?
            ),
            duration = (
                SELECT COALESCE(SUM(t.duration), 0)
                FROM playlist_tracks pt
                JOIN tracks t ON pt.track_id = t.id
                WHERE pt.playlist_id = ?
            ),
            last_updated = ?
            WHERE id = ?
            """,
            (playlist_id, playlist_id, int(time.time()), playlist_id),
        )
