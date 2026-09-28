"""
Centralized database schema initialization.

Creates the complete schema in a single transaction before any subprocess
starts, eliminating race conditions from concurrent ALTER TABLE / CREATE TABLE
calls across indexer, searcher, embedder, and enricher processes.

Called once from module_setup.py in the main server process.
"""

from __future__ import annotations

import logging
import os
import time

import aiosqlite

from .embedding_utils import CLAP_EMBED_FORMAT_VERSION, VA_HEAD_VERSION

logger = logging.getLogger(__name__.split(".")[-1])

_CLAP_DIMS = 512

_VEC_AUDIO_TABLES = [
    ("vec_tracks_clap", "track_id"),
    ("vec_albums_clap", "album_id"),
    ("vec_artists_clap", "artist_id"),
]

_VEC_TEXT_TABLES = [
    ("vec_tracks_clap_text", "track_id"),
    ("vec_albums_clap_text", "album_id"),
    ("vec_artists_clap_text", "artist_id"),
]


async def init_db(db_path: str) -> None:
    """Create the complete database schema. Safe to call on every startup."""
    db_path = os.path.expanduser(db_path)
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        cursor = await conn.cursor()

        # ---------------------------------------------------------------
        # Core tables (indexer)
        # ---------------------------------------------------------------

        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS artists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mbid TEXT,
                image_url TEXT,
                country TEXT,
                area TEXT,
                enriched INTEGER DEFAULT 0,
                match_score INTEGER,
                match_similarity INTEGER,
                last_updated INTEGER,
                embedding_clap BLOB,
                embedding_clap_text BLOB
            )
            """
        )

        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS albums (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artist_id TEXT,
                year INTEGER,
                original_year INTEGER,
                genre TEXT,
                language TEXT,
                image_url TEXT,
                image_generated INTEGER DEFAULT 0,
                mbid TEXT,
                track_count INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0,
                enriched INTEGER DEFAULT 0,
                match_score INTEGER,
                match_similarity INTEGER,
                last_updated INTEGER,
                embedding_clap BLOB,
                embedding_clap_text BLOB,
                FOREIGN KEY (artist_id) REFERENCES artists (id)
            )
            """
        )

        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                album_id TEXT,
                artist_id TEXT,
                duration INTEGER,
                track_number INTEGER,
                disc_number INTEGER,
                file_path TEXT NOT NULL,
                format TEXT NOT NULL,
                file_size BIGINT,
                modified_time INTEGER,
                mbid TEXT,
                match_score INTEGER,
                match_similarity INTEGER,
                replaygain_peak REAL,
                replaygain_gain REAL,
                enriched INTEGER DEFAULT 0,
                last_updated INTEGER,
                embedding_clap_audio BLOB,
                embedding_version INTEGER DEFAULT 0,
                embedded_at TIMESTAMP,
                embedding_clap_text BLOB,
                mood_valence REAL,
                mood_arousal REAL,
                image_url TEXT,
                FOREIGN KEY (album_id) REFERENCES albums (id),
                FOREIGN KEY (artist_id) REFERENCES artists (id)
            )
            """
        )

        # Stable per-file identity. current_path is a mutable attribute, so a
        # rename keeps the id. file_id == tracks.id; first_indexed is never
        # rewritten. device/inode corroborate move detection later.
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS library_file (
                file_id       TEXT PRIMARY KEY,
                current_path  TEXT NOT NULL UNIQUE,
                size_bytes    INTEGER,
                modified_at   INTEGER,
                device_id     TEXT,
                inode         TEXT,
                content_hash  TEXT,
                first_indexed INTEGER NOT NULL
            )
            """
        )

        # Verbatim per-file evidence the display tables don't carry (raw tags
        # incl. albumartist/compilation, stream info, art hash, cue,
        # chromaprint). Current snapshot: upserted, not versioned.
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS track_evidence (
                track_id       TEXT PRIMARY KEY,
                raw_tags       TEXT,
                stream_info    TEXT,
                art_phash      TEXT,
                cue_sheet      TEXT,
                cue_tracks     TEXT,
                fingerprint    TEXT,
                fp_computed_at INTEGER,
                import_batch   TEXT,
                updated_at     INTEGER
            )
            """
        )

        # Negative cache for files whose metadata could not be extracted.
        # Keyed by path + (size, mtime) so a still-uploading / partially
        # written file — which changes size or mtime between scans — keeps
        # missing this cache and is retried, while a stably broken file is
        # parked and skipped instead of being re-read on every scan.
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS indexer_failures (
                file_path     TEXT PRIMARY KEY,
                file_size     BIGINT,
                modified_time INTEGER,
                error         TEXT,
                attempts      INTEGER NOT NULL DEFAULT 1,
                last_attempt  INTEGER
            )
            """
        )

        # The same for cover sources that would not decode, per entity and
        # per pass (kind 'folder' or 'embedded').
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS art_source_failures (
                entity_id   TEXT NOT NULL,
                source_path TEXT NOT NULL,
                kind        TEXT NOT NULL,
                size        INTEGER NOT NULL,
                mtime_ns    INTEGER NOT NULL,
                PRIMARY KEY (entity_id, source_path)
            )
            """
        )

        # Per local-album-cluster grouping metadata, keyed 1:1 to an albums
        # row. The folders a cluster occupies are derived from member tracks;
        # primary_folder is the dominant one (display/blocking only).
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS album_cluster (
                album_id       TEXT PRIMARY KEY,
                primary_folder TEXT NOT NULL,
                grouping_conf  REAL NOT NULL DEFAULT 1.0,
                grouping_basis TEXT,
                kind           TEXT,
                generation     INTEGER NOT NULL DEFAULT 0,
                needs_review   INTEGER NOT NULL DEFAULT 0,
                review_reason  TEXT
            )
            """
        )

        # Durable user grouping decisions the reconciler treats as hard
        # constraints (a relationship, not a metadata field).
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS membership_constraint (
                id               TEXT PRIMARY KEY,
                kind             TEXT NOT NULL,
                track_id         TEXT NOT NULL,
                album_id         TEXT,
                related_track_id TEXT,
                created_at       INTEGER NOT NULL
            )
            """
        )

        # Redirects a replaced id (after a cluster split/merge or a track-move)
        # to its current one, so artwork caches, playlists and bookmarks keep
        # resolving. Chains are flattened on write, so resolution is one hop.
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS entity_id_alias (
                old_id      TEXT PRIMARY KEY,
                current_id  TEXT NOT NULL,
                entity_type TEXT NOT NULL
            )
            """
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_entity_id_alias_current "
            "ON entity_id_alias(current_id)"
        )

        # Field-level claims (Phase 2c). SPARSE: only rows worth keeping are
        # stored — winning non-local claims (external/inference values that can
        # expire or be rejected), pinned claims, and conflicts. Uncontested
        # purely-local values resolve straight into the entity row and are
        # re-derivable from track_evidence, so they are not stored here; their
        # provenance still lands in resolved_origin.
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata_claims (
                entity_type TEXT NOT NULL,
                entity_id   TEXT NOT NULL,
                field       TEXT NOT NULL,
                value       TEXT NOT NULL,
                source      TEXT NOT NULL,
                tier        TEXT NOT NULL,
                created_at  INTEGER,
                PRIMARY KEY (entity_type, entity_id, field, source)
            )
            """
        )

        # Provenance for EVERY resolved field, including uncontested ones. This
        # is what keeps sparse claims compatible with re-derivability: claims
        # record only conflicts, but the winner's origin is always known, so the
        # resolver can tell whether a value must be recomputed when its source's
        # evidence changes or a provider is disabled.
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS resolved_origin (
                entity_type  TEXT NOT NULL,
                entity_id    TEXT NOT NULL,
                field        TEXT NOT NULL,
                source       TEXT NOT NULL,
                tier         TEXT NOT NULL,
                evidence_ref TEXT,
                resolved_at  INTEGER,
                PRIMARY KEY (entity_type, entity_id, field)
            )
            """
        )

        # Semantic links between two live entities (same_identity, variant_of,
        # release_group_member, ...) — the propagation channels the library-wide
        # inference pass depends on. Distinct from entity_id_alias, which only
        # redirects a replaced id; a relation asserts a fact ABOUT two entities.
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS entity_relation (
                source_type TEXT NOT NULL,
                source_id   TEXT NOT NULL,
                relation    TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id   TEXT NOT NULL,
                status      TEXT NOT NULL,
                source      TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                PRIMARY KEY (source_type, source_id, relation, target_type, target_id)
            )
            """
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_entity_relation_target "
            "ON entity_relation(target_type, target_id)"
        )

        # Album-level external matching (Phase 3). A local album cluster is
        # scored against candidate provider releases by tracklist alignment;
        # the top few are kept here with their coverage + track_map so an
        # accepted candidate can drive per-track lookups and its resolved
        # values can be invalidated if it is later rejected/expired.
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS release_candidates (
                album_id     TEXT NOT NULL,
                provider     TEXT NOT NULL,          -- musicbrainz|qobuz|deezer
                release_id   TEXT NOT NULL,          -- release mbid / catalogue id
                rg_id        TEXT,                   -- release-group mbid
                score        REAL NOT NULL,
                coverage     REAL NOT NULL,          -- fraction of members explained
                track_map    TEXT,                   -- JSON: track_id -> [disc,pos,rec_id]
                status       TEXT NOT NULL DEFAULT 'candidate',  -- candidate|accepted|rejected
                fetched_at   INTEGER,
                PRIMARY KEY (album_id, provider, release_id)
            )
            """
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_release_candidates_album "
            "ON release_candidates(album_id, status)"
        )

        # Per-track recording identity (AcoustID / MB), separate from grouping.
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS recording_identity (
                track_id  TEXT NOT NULL,
                provider  TEXT NOT NULL,
                rec_id    TEXT NOT NULL,
                score     REAL NOT NULL,
                PRIMARY KEY (track_id, provider, rec_id)
            )
            """
        )

        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                track_count INTEGER DEFAULT 0,
                image_url TEXT,
                duration INTEGER DEFAULT 0,
                created_by TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_updated INTEGER NOT NULL
            )
            """
        )

        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                playlist_track_id TEXT PRIMARY KEY,
                playlist_id TEXT,
                track_id TEXT,
                position INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                FOREIGN KEY (playlist_id) REFERENCES playlists (id) ON DELETE CASCADE,
                FOREIGN KEY (track_id) REFERENCES tracks (id) ON DELETE CASCADE
            )
            """
        )

        # ---------------------------------------------------------------
        # Indexes
        # ---------------------------------------------------------------

        await cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist_id
            ON playlist_tracks (playlist_id, position)
            """
        )

        # Albums by artist — the artist listing counts them per row.
        await cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_albums_artist_id
            ON albums (artist_id)
            """
        )

        # ---------------------------------------------------------------
        # Views
        # ---------------------------------------------------------------

        await cursor.execute(
            """
            CREATE VIEW IF NOT EXISTS recently_added AS
            SELECT * FROM tracks
            ORDER BY last_updated DESC
            """
        )

        # ---------------------------------------------------------------
        # Embedding job queue (shared by searcher + embedder)
        # ---------------------------------------------------------------

        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_jobs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type   TEXT NOT NULL,
                entity_id     TEXT NOT NULL,
                stage         TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending',
                model_version INTEGER NOT NULL DEFAULT 1,
                attempts      INTEGER NOT NULL DEFAULT 0,
                error         TEXT,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(entity_type, entity_id, stage, model_version)
            )
            """
        )

        await cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_pending
                ON embedding_jobs(status, stage)
                WHERE status IN ('pending', 'failed')
            """
        )

        # get_embedding_coverage()'s MAX-per-stage subquery is O(n²) without this.
        await cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_stage_version
                ON embedding_jobs(stage, model_version)
            """
        )

        # ---------------------------------------------------------------
        # Model version registry
        # ---------------------------------------------------------------

        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_model_versions (
                model_name TEXT PRIMARY KEY,
                version    INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for model_name in ("clap_audio", "clap_text"):
            await cursor.execute(
                "INSERT OR IGNORE INTO embedding_model_versions VALUES (?, 1, CURRENT_TIMESTAMP)",
                (model_name,),
            )

        # ---------------------------------------------------------------
        # Enricher state (key/value)
        # ---------------------------------------------------------------
        # Small grab-bag of enricher-owned scalars. Currently holds the
        # "enrichment fingerprint" — a signature of the active plugin
        # set / versions / match-affecting config — used to decide
        # whether a restart should re-open previously-FAILED rows.

        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS enricher_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

        # ---------------------------------------------------------------
        # Indexer state (key/value)
        # ---------------------------------------------------------------
        # Scan progress published by the indexer subprocess (total files
        # discovered by the pre-count walk vs. files processed so far) so
        # get_indexer_status() can report an "indexing" stage while a scan
        # is running.

        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS indexer_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

        # The fts_tracks FTS5 index is retired — BEST MATCH moved to the server
        # (it now uses the input modules' search()). Drop the orphaned table from
        # existing DBs (idempotent; the search_indexed_at column, if present on an
        # old DB, is simply left unused).
        await cursor.execute("DROP TABLE IF EXISTS fts_tracks")

        # ---------------------------------------------------------------
        # Default data
        # ---------------------------------------------------------------

        current_time = int(time.time())
        await cursor.execute(
            "INSERT OR IGNORE INTO artists (id, name, last_updated) VALUES ('unknown_artist', 'Unknown Artist', ?)",
            (current_time,),
        )
        await cursor.execute(
            "INSERT OR IGNORE INTO albums (id, title, artist_id, last_updated) VALUES ('unknown_album', 'Unknown Album', 'unknown_artist', ?)",
            (current_time,),
        )

        # ---------------------------------------------------------------
        # Column migrations for existing DBs (CREATE TABLE IF NOT EXISTS won't
        # add columns). Idempotent; runs in the single-writer startup txn.
        # ---------------------------------------------------------------
        await cursor.execute("PRAGMA table_info(tracks)")
        track_cols = {row[1] for row in await cursor.fetchall()}
        for col in ("mood_valence", "mood_arousal"):
            if col not in track_cols:
                await cursor.execute(f"ALTER TABLE tracks ADD COLUMN {col} REAL")
                logger.info("Added tracks.%s column", col)
        # Per-field origin/era columns (Phase 2c) — one column, one meaning, so
        # inference can never launder a release fact into a recording fact.
        # recording_year: a track can predate the release it appears on;
        # language: a track can differ from the release's language.
        if "recording_year" not in track_cols:
            await cursor.execute("ALTER TABLE tracks ADD COLUMN recording_year INTEGER")
            logger.info("Added tracks.recording_year column")
        if "language" not in track_cols:
            await cursor.execute("ALTER TABLE tracks ADD COLUMN language TEXT")
            logger.info("Added tracks.language column")
        # Track-level cover for singles: a track on unknown_album has no album
        # row to carry its embedded art.
        if "image_url" not in track_cols:
            await cursor.execute("ALTER TABLE tracks ADD COLUMN image_url TEXT")
            logger.info("Added tracks.image_url column")

        # Origin/era metadata (nationality + language + first-release year) so
        # queries like "italian 80s" resolve on structured facts instead of CLAP,
        # which can't perceive nationality or decade from audio. Best-effort
        # columns — NOT added to *_REQUIRED_FIELDS, so a missing value never
        # marks an entity as failed-enrichment.
        await cursor.execute("PRAGMA table_info(artists)")
        artist_cols = {row[1] for row in await cursor.fetchall()}
        for col in ("country", "area"):
            if col not in artist_cols:
                await cursor.execute(f"ALTER TABLE artists ADD COLUMN {col} TEXT")
                logger.info("Added artists.%s column", col)

        await cursor.execute("PRAGMA table_info(albums)")
        album_cols = {row[1] for row in await cursor.fetchall()}
        if "language" not in album_cols:
            await cursor.execute("ALTER TABLE albums ADD COLUMN language TEXT")
            logger.info("Added albums.language column")
        if "original_year" not in album_cols:
            await cursor.execute("ALTER TABLE albums ADD COLUMN original_year INTEGER")
            logger.info("Added albums.original_year column")
        # Release country (Phase 2c) — distinct from artists.country (origin);
        # a release can be issued in a different country than the artist's.
        if "country" not in album_cols:
            await cursor.execute("ALTER TABLE albums ADD COLUMN country TEXT")
            logger.info("Added albums.country column")
        # Marks covers produced by the procedural artwork generator, so the
        # FAILED-row retry sweep can clear them and give real sources
        # another chance when the enrichment setup changes.
        if "image_generated" not in album_cols:
            await cursor.execute(
                "ALTER TABLE albums ADD COLUMN image_generated INTEGER DEFAULT 0"
            )
            logger.info("Added albums.image_generated column")

        # Parsed cue tracklist (JSON) for single-file CD rips.
        await cursor.execute("PRAGMA table_info(track_evidence)")
        ev_cols = {row[1] for row in await cursor.fetchall()}
        if "cue_tracks" not in ev_cols:
            await cursor.execute("ALTER TABLE track_evidence ADD COLUMN cue_tracks TEXT")
            logger.info("Added track_evidence.cue_tracks column")

        # Backfill file identity from existing tracks (path-hash id becomes
        # file_id; last_updated is the best first_indexed for legacy rows).
        # Columns selected conditionally so a pre-size/mtime tracks table still
        # migrates. Idempotent via OR IGNORE.
        size_expr = "file_size" if "file_size" in track_cols else "NULL"
        mtime_expr = "modified_time" if "modified_time" in track_cols else "NULL"
        first_expr = (
            "COALESCE(last_updated, ?)" if "last_updated" in track_cols else "?"
        )
        await cursor.execute(
            f"""
            INSERT OR IGNORE INTO library_file
                (file_id, current_path, size_bytes, modified_at, first_indexed)
            SELECT id, file_path, {size_expr}, {mtime_expr}, {first_expr}
            FROM tracks
            """,
            (current_time,),
        )

        # VA (mood) head migration (after the columns above exist). On a head
        # version change, clear stale mood so the backfill recomputes it with the
        # new head — no manual cleanup. (A re-embed clears mood separately.)
        await cursor.execute(
            "SELECT version FROM embedding_model_versions WHERE model_name = 'va_head'"
        )
        row = await cursor.fetchone()
        if (row[0] if row else 0) != VA_HEAD_VERSION:
            await cursor.execute(
                "UPDATE tracks SET mood_valence = NULL, mood_arousal = NULL "
                "WHERE mood_valence IS NOT NULL"
            )
            await cursor.execute(
                "INSERT INTO embedding_model_versions (model_name, version, updated_at) "
                "VALUES ('va_head', ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(model_name) DO UPDATE SET "
                "version = excluded.version, updated_at = CURRENT_TIMESTAMP",
                (VA_HEAD_VERSION,),
            )
            logger.info("VA head v%d: cleared mood (V,A) for recompute", VA_HEAD_VERSION)

        await conn.commit()

    # ---------------------------------------------------------------
    # sqlite-vec virtual tables (optional — loaded as extension)
    # ---------------------------------------------------------------

    await _init_vec_tables(db_path)

    logger.info("Database schema initialized: %s", db_path)


async def _init_vec_tables(db_path: str) -> None:
    """Load sqlite-vec and create CLAP vector tables, migrating older formats.

    vec0 columns are typed. Legacy builds stored ``float[512]``; we now store
    ``int8[512]``. On a format mismatch (PRAGMA user_version) we drop the typed
    vec tables and clear the embedding blobs; the bumped
    ``CLAP_MODEL_VERSION`` reschedules the jobs that recompute them.
    Until they refill, a typed-mismatch MATCH raises and the search layer reads
    it as "no vector hits" — KNN search is empty but never crashes.
    """
    try:
        import sqlite_vec
    except Exception as e:
        logger.warning("sqlite-vec not available (%s); KNN search disabled", e)
        return

    try:
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.enable_load_extension(True)
            await conn.load_extension(sqlite_vec.loadable_path())
            await conn.enable_load_extension(False)

            cursor = await conn.cursor()
            await cursor.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            fmt = row[0] if row else 0
            if fmt != CLAP_EMBED_FORMAT_VERSION:
                if fmt != 0:
                    logger.warning(
                        "CLAP embedding format v%d -> v%d: dropping vector tables "
                        "and clearing stored embeddings for recompute",
                        fmt,
                        CLAP_EMBED_FORMAT_VERSION,
                    )
                # Drop the typed vec0 tables (DROP needs the loaded extension)
                # so they are recreated below with the new dtype.
                for vec_table, _pk in _VEC_AUDIO_TABLES + _VEC_TEXT_TABLES:
                    await cursor.execute(f"DROP TABLE IF EXISTS {vec_table}")
                # Clear stale embedding blobs so the embedder recomputes them and
                # mean-pooled aggregates never mix old- and new-format vectors.
                await cursor.execute(
                    "UPDATE tracks SET embedding_clap_audio = NULL, "
                    "embedding_clap_text = NULL, embedding_version = 0, "
                    "embedded_at = NULL"
                )
                await cursor.execute(
                    "UPDATE albums SET embedding_clap = NULL, "
                    "embedding_clap_text = NULL"
                )
                await cursor.execute(
                    "UPDATE artists SET embedding_clap = NULL, "
                    "embedding_clap_text = NULL"
                )
                # PRAGMA can't be parameterised; the value is a trusted constant.
                await cursor.execute(
                    f"PRAGMA user_version = {int(CLAP_EMBED_FORMAT_VERSION)}"
                )
                await conn.commit()

            for vec_table, pk_col in _VEC_AUDIO_TABLES + _VEC_TEXT_TABLES:
                await cursor.execute(
                    f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS {vec_table}
                    USING vec0({pk_col} TEXT PRIMARY KEY, embedding int8[{_CLAP_DIMS}])
                    """
                )
            await conn.commit()

        logger.info("sqlite-vec extension loaded; CLAP int8 vector tables ready")
    except Exception as e:
        logger.warning("sqlite-vec vec table init failed (%s); KNN search disabled", e)
