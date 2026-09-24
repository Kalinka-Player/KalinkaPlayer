#!/usr/bin/env python3
import os
import re
import sys
import io
import json
import time
import logging
import asyncio
import multiprocessing
from typing import (
    Any,
    AsyncIterator,
    BinaryIO,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
)

from pathlib import Path

from PIL import Image

from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.id3 import ID3

from ..config_model import LocalFilesConfig
from ..filename_model import get_parser, names_a_vinyl_side, parse_music_path
from ..resolution.resolver import (
    FILENAME,
    FOLDER_NAME,
    GUESSED,
    LOCALLY_DERIVED,
    OBSERVED,
    TAG_CONSENSUS,
)
from ..utils.artwork_store import save_artwork_from_file, save_artwork_images
from ..utils.folder_art import FolderCover, find_folder_cover
from ..worker_utils import nudge
from ..storage import (
    PROBE_TIMEOUT_S,
    ChangeKind,
    ChangeWatcher,
    FileStorage,
    RootStatus,
    StorageResolver,
    build_resolver,
    library_roots,
    media_type_of,
)
from ..utils.name_utils import (
    album_folder_for_path,
    clean_display_name,
    repair_tag_text,
)
from .cue import find_cue_for, parse_cue
from .id_generator import (
    generate_artist_id,
    generate_album_id,
    generate_track_id,
)
from .indexer_db import AsyncIndexerDb


# V/A-compilation classification (thresholds, folder heuristics, title
# helpers) lives in clustering.classify so the planner and this pass share it.
from ..clustering.classify import (  # noqa: E402
    GENERIC_FOLDER_RE as _GENERIC_FOLDER_RE,
    VA_MIN_ARTIST_UNIQUENESS,
    VA_MIN_DISTINCT_ARTISTS,
    VARIOUS_ARTISTS_ID,
    compilation_title as _compilation_title,
    strip_artist_prefix as _strip_artist_prefix,
)


SUPPORTED_AUDIO_EXTENSIONS = {".mp3", ".flac"}

# How often the file watcher checks whether a lost root came back.
REARM_CHECK_INTERVAL_S = 15.0

# How long a watcher blocks for a batch of changes. Long enough that the
# wait sits in the kernel rather than spinning, short enough that a stop
# signal is noticed promptly.
WATCH_POLL_INTERVAL_S = 1.0

def rearm_needs_rescan(
    unmount_seen: bool,
    identity: Optional[str],
    stored_signature: Optional[str],
) -> bool:
    """Whether a re-armed root owes a rescan, or was just an autofs bounce.

    An unmounted root that returns with the mount identity the library was
    indexed from is the same filesystem — remote changes are invisible to
    inotify regardless, so the periodic scan covers them and a
    bounce-triggered rescan adds nothing. ``unmount_seen`` is False when a
    rescan is owed unconditionally (startup deferral, deleted root).
    """
    if not unmount_seen:
        return True
    return not (stored_signature and identity == stored_signature)


# Configure logger for watchfiles.main only to WARNING level
watchfiles_logger = logging.getLogger("watchfiles.main")
watchfiles_logger.setLevel(logging.WARNING)
aiosqlite_logger = logging.getLogger("aiosqlite")
aiosqlite_logger.setLevel(logging.WARNING)

logger = logging.getLogger("indexer")


# Global variables to manage indexer state
_indexer_task: Optional[asyncio.Task] = None
_indexer_queue: asyncio.Queue = asyncio.Queue()
_file_watcher_task: Optional[asyncio.Task] = None
_file_watcher_stop_event: asyncio.Event = asyncio.Event()
_shutdown_event = asyncio.Event()
# In-process handoff to the enricher worker (both run in the librarian
# process). Set by librarian.async_main; None only in unit tests that
# exercise FileIndexer without a running enricher.
_enricher_queue: Optional[asyncio.Queue] = None
_embedder_nudge_queue: Optional[multiprocessing.Queue] = None


async def trigger_enricher_update(data):
    """Trigger the enricher update with the given data"""
    if not data:
        logger.warning("No data provided to trigger enricher update")
        return

    logger.debug(f"Triggering enricher update with data: {data}")

    # clap_audio waits only on the index — wake the embedder even when the
    # enricher is disabled.
    nudge(_embedder_nudge_queue)

    if _enricher_queue is None:
        logger.debug("Enricher queue not initialized; skipping enrich trigger")
        return

    await _enricher_queue.put(data)


def is_supported_audio_file(filename: str) -> bool:
    """Check if the file is a supported audio format."""
    ext = os.path.splitext(filename.lower())[1]
    return ext in SUPPORTED_AUDIO_EXTENSIONS


#: Rows many files contribute to, where a guess must not displace a tag.
_SHARED_ENTITIES = ("artist", "album")
#: The library-wide rows that stand in for "nobody named this".
_PLACEHOLDER_IDS = ("unknown_artist", "unknown_album")


def _tag_else_path(metadata: Dict, from_path, field: str):
    """The tag's value for ``field``, or what the path said, or None."""
    value = metadata.get(field)
    if value is None and from_path is not None:
        value = getattr(from_path, field)
    return value


def _tags_leave_a_gap(metadata: Dict, file_path: str) -> bool:
    """Whether the file's own tags left anything the path could still supply.

    Asked before parsing rather than after, because a fully tagged file would
    have every path-derived value discarded anyway. A missing disc number
    counts only where a path can actually name one: inside a disc
    subdirectory, or on a file whose own name opens on a vinyl side. Most
    single-disc rips are neither and carry no DISCNUMBER at all.

    A side-lettered rip is otherwise invisible here — it tags artist, title,
    year and a track number that restarts at 1 on every side, so without this
    the four sides interleave and nothing ever asks the path why.
    """
    return (
        not metadata.get("artist")
        or not metadata.get("title")
        or metadata.get("track_number") is None
        or metadata.get("year") is None
        or (
            metadata.get("disc_number") is None
            and (
                album_folder_for_path(file_path) != os.path.dirname(file_path)
                or names_a_vinyl_side(file_path)
            )
        )
    )


class FileIndexer:
    def __init__(
        self,
        config: LocalFilesConfig,
        db_manager: AsyncIndexerDb,
        storage: Optional[StorageResolver] = None,
    ):
        self.config = config
        self.db_manager = db_manager
        # Every file this class reads goes through the resolver, so a music
        # folder may be a path on this machine or a share it talks to
        # itself. Injectable so a test can drive the indexer over storage
        # that is neither.
        self.storage = storage or build_resolver(config)
        # Canonical form of each music folder and source. This is the access
        # boundary: only files under one of these are indexed, and
        # cleanup_stale_tracks purges anything that falls outside them.
        self.music_folders = library_roots(config, self.storage)
        self.artwork_path = Path(config.artwork_path).expanduser().resolve()
        self.running = False
        self.lock = asyncio.Lock()
        # Scan progress (full scans only): total from the pre-count walk,
        # processed incremented per file. Published to the indexer_state
        # table in batches so /indexer/status can show an "indexing" stage.
        # _scan_active gates publishing — scan_folder also runs for inotify
        # dir-added batches, where there is no meaningful total.
        self._scan_active = False
        self._scan_total = 0
        self._scan_processed = 0
        self._scan_progress_written_at = 0.0

    async def start(self):
        """Start the indexer process"""
        async with self.lock:
            if self.running:
                logger.warning("Indexer already running, skipping")
                return
            self.running = True

        try:
            await self.run_scan()
            logger.debug("Indexer scan completed")
        except Exception as e:
            logger.exception(f"Error running indexer scan: {str(e)}")
        finally:
            async with self.lock:
                self.running = False

    async def run_scan(self):
        """Scan all music folders for files"""
        logger.debug(f"Starting music file scan in folders: {self.music_folders}")

        changed_items: Dict[str, Set[str]] = {
            "artists": set(),
            "albums": set(),
            "tracks": set(),
        }

        # An unavailable root must be skipped, not scanned as an empty tree.
        root_status = await self._probe_music_roots()
        available_folders = [
            folder
            for folder in self.music_folders
            if root_status[folder].available
        ]
        for folder in self.music_folders:
            if folder not in available_folders:
                logger.warning(
                    f"Music folder is not available, skipping scan: "
                    f"{folder} ({root_status[folder].reason})"
                )

        # Publish a zeroed row first: it overwrites anything a scan killed
        # harder than `finally` (OOM, power loss) left behind, and an
        # all-zero row reads as "no outstanding work" while the pre-count
        # below runs.
        self._scan_total = 0
        self._scan_processed = 0
        self._scan_active = True
        await self._publish_scan_progress(force=True)

        # Listed once and kept, not counted and listed again: every listing
        # is a round trip on a share, and the count is what the per-file loop
        # below reports progress against.
        files_by_folder = await self._audio_files_by_folder(available_folders)
        self._scan_total = sum(len(files) for files in files_by_folder.values())
        await self._publish_scan_progress(force=True)

        # Record the identity only where music was found, so a bare
        # mountpoint never overwrites the mark the purge guard trusts.
        for folder in available_folders:
            identity = root_status[folder].identity
            if files_by_folder.get(folder) and identity:
                await self.db_manager.set_root_signature(folder, identity)

        try:
            for folder in available_folders:
                logger.debug(f"Scanning folder: {folder}")
                await self._index_files(files_by_folder[folder], changed_items)
        finally:
            # Mark the scan inactive even on failure so the status reader
            # never shows a stuck "indexing" stage.
            self._scan_active = False
            await self._publish_scan_progress(force=True)

        # Delete stale entries after scanning but before enrichment
        cleanup_results = await self.cleanup_stale_tracks()

        # If we removed any tracks, ensure we don't trigger enrichment for them
        if cleanup_results["tracks"] > 0:
            logger.info(
                "Removed stale tracks from database, proceeding with enrichment for valid tracks only"
            )

        reparsed = await self._reparse_paths_on_model_change(
            available_folders, changed_items
        )
        if reparsed:
            logger.info(
                "Filename model changed; re-read the path for %d track(s) "
                "nothing else had named",
                reparsed,
            )

        # Group tracks into albums. Folder-first clustering (when enabled)
        # supersedes the V/A-only coalesce pass — it does V/A folding plus
        # tag-variance merging, untagged-rip titling and multi-album splits.
        # Both run after stale-track cleanup and before the enricher nudge.
        if getattr(self.config, "folder_first_clustering", False):
            res = await self.recluster()
            if res.get("reassigned"):
                logger.info(
                    "Clustering: %d album(s), %d track(s) re-pointed, "
                    "%d alias(es), %d orphan album(s) deleted",
                    res["clusters"], res["reassigned"],
                    res["aliases"], res["orphans"],
                )
        else:
            va_results = await self.orphan_va_folder_tracks()
            if va_results["folders"]:
                logger.info(
                    f"V/A coalesce: {va_results['folders']} folder(s), "
                    f"{va_results['tracks']} track(s) re-pointed, "
                    f"{va_results['orphans']} orphan album(s) deleted"
                )

        backfilled = await self.backfill_embedded_art(available_folders)
        if any(backfilled.values()):
            logger.info(
                "Embedded art restored for %d album(s) and %d single(s)",
                backfilled["albums"], backfilled["tracks"],
            )

        from_folders = await self.backfill_folder_art(available_folders)
        if from_folders["albums"]:
            logger.info(
                "Cover taken from the album folder for %d album(s)",
                from_folders["albums"],
            )

        # If anything changed and we have an enricher callback, notify it
        if any(changed_items.values()):
            logger.info(
                f"Scan completed with changes: Artists={len(changed_items['artists'])}, "
                f"Albums={len(changed_items['albums'])}, Tracks={len(changed_items['tracks'])}"
            )
        else:
            logger.debug("Scan completed with no changes")

        # Trigger the enricher run regardless of changes
        # as there might be old files pending enrichment
        await trigger_enricher_update("enrich")

    async def handle_incremental_changes(self, changes: Set[Tuple[str, str]]):
        """Process filesystem changes detected by the inotify watcher."""
        changed_items: Dict[str, Set[str]] = {
            "artists": set(),
            "albums": set(),
            "tracks": set(),
        }

        processed_dirs = set()
        processed_files = set()

        for change_type, file_path in changes:
            logger.debug(f"Change detected: {change_type} - {file_path}")

            if change_type == "dir_added":
                logger.info(f"New directory detected: {file_path}")
                try:
                    await self.scan_folder(file_path, changed_items)
                    processed_dirs.add(file_path)
                    continue
                except Exception as e:
                    logger.exception(
                        f"Error scanning new directory {file_path}: {str(e)}"
                    )
                    continue

            if change_type in (ChangeKind.PATH_REMOVED, ChangeKind.DIR_REMOVED):
                # No per-path work: the cleanup_stale_tracks() pass at the
                # end of this batch drops database rows for files that are
                # no longer there.
                logger.info(f"Path removed, cleanup scheduled: {file_path}")
                continue

            if file_path in processed_files:
                continue

            parent_processed = False
            for processed_dir in processed_dirs:
                if file_path.startswith(processed_dir + os.sep):
                    logger.debug(
                        f"Skipping file in already processed directory: {file_path}"
                    )
                    parent_processed = True
                    break
            if parent_processed:
                continue

            if not self._is_supported_audio_file(file_path):
                continue

            if change_type in ("file_closed", "file_moved"):
                # File was closed after write or moved (atomic rename) - safe to process
                try:
                    logger.info(f"Processing changed file: {file_path}")
                    result_changes = await self.process_file(file_path)
                    processed_files.add(file_path)
                    if result_changes:
                        for key, value in result_changes.items():
                            if value:
                                changed_items[key].add(value)
                except Exception as e:
                    logger.exception(
                        f"Error processing changed file {file_path}: {str(e)}"
                    )

        await self.cleanup_stale_tracks()

        if any(changed_items.values()):
            logger.info(
                f"File changes detected: Artists={len(changed_items['artists'])}, "
                f"Albums={len(changed_items['albums'])}, Tracks={len(changed_items['tracks'])}"
            )
            await trigger_enricher_update("enrich")

    async def _iter_audio_files(self, folder: str) -> AsyncIterator[str]:
        """Supported audio files under ``folder``, depth first.

        One directory listing at a time and each off the event loop, because
        a listing is a round trip on a share and never returns at all on a
        hung mount. Symbolic links to directories are not descended, which
        is what ``os.walk`` does by default and what keeps a link loop from
        walking forever.
        """
        storage = self.storage.for_path(folder)
        pending = [folder]
        while pending:
            directory = pending.pop()
            try:
                entries = await asyncio.to_thread(storage.listdir, directory)
            except OSError as e:
                logger.warning(f"Could not list {directory}: {e}")
                continue
            for entry in entries:
                if entry.is_dir:
                    pending.append(entry.path)
                elif self._is_supported_audio_file(entry.name):
                    yield entry.path

    async def scan_folder(self, folder: str, changed_items: Dict[str, Set[str]]):
        """Recursively scan a folder for music files"""
        await self._index_files(
            [path async for path in self._iter_audio_files(folder)], changed_items
        )

    async def _index_files(
        self, files: List[str], changed_items: Dict[str, Set[str]]
    ):
        """Index files a walk has already listed, reporting progress as they
        go."""
        for file_path in files:
            try:
                result_changes = await self.process_file(file_path)
                if result_changes:
                    for key, value in result_changes.items():
                        if value:
                            changed_items[key].add(value)
            except Exception as e:
                logger.exception(f"Error processing file {file_path}: {str(e)}")
            if self._scan_active:
                self._scan_processed += 1
                await self._publish_scan_progress()

    async def _audio_files_by_folder(
        self, folders: List[str]
    ) -> Dict[str, List[str]]:
        """The supported audio files under each folder.

        Directory listings only — no per-file measurement — so the cost is
        one round trip per directory and nothing per file. Walks only the
        folders given: touching an unavailable root here would re-trigger a
        failed automount or hang on a dead mount.
        """
        found: Dict[str, List[str]] = {}
        for folder in folders:
            found[folder] = [path async for path in self._iter_audio_files(folder)]
        return found

    async def _publish_scan_progress(self, force: bool = False):
        """Write scan progress to the database, throttled to one write per
        couple of seconds unless forced (scan start/end)."""
        now = time.monotonic()
        if not force and now - self._scan_progress_written_at < 2.0:
            return
        self._scan_progress_written_at = now
        try:
            await self.db_manager.set_scan_progress(
                self._scan_total, self._scan_processed, self._scan_active
            )
        except Exception as e:
            logger.debug(f"Failed to publish scan progress: {e}")

    def _is_supported_audio_file(self, filename: str) -> bool:
        """Check if the file is a supported audio format."""
        return is_supported_audio_file(filename)

    async def _path_gone(self, path: str) -> bool:
        """Whether one path no longer exists, asked off the event loop."""
        storage = self.storage.for_path(path)
        return not await asyncio.to_thread(storage.exists, path)

    async def _missing_paths(self, paths: Iterable[str]) -> Set[str]:
        """Which of ``paths`` no longer exist.

        Grouped into one call per storage rather than one per path: the
        cleanup sweep asks about every row in the library, and a thread hop
        each would cost more than the checks themselves.
        """
        grouped: Dict[FileStorage, List[str]] = {}
        for path in paths:
            grouped.setdefault(self.storage.for_path(path), []).append(path)

        def absent(storage: FileStorage, batch: List[str]) -> Set[str]:
            return {path for path in batch if not storage.exists(path)}

        missing: Set[str] = set()
        for storage, batch in grouped.items():
            missing |= await asyncio.to_thread(absent, storage, batch)
        return missing

    async def process_file(
        self, file_path: str, force: bool = False
    ) -> Optional[Dict[str, Optional[str]]]:
        """Process a music file and update the database. Returns changed items IDs."""
        # Access boundary: never index a file whose canonical path escapes the
        # configured music folders — e.g. a symlink sitting under a watched
        # folder that resolves elsewhere. Enforced here, using the same
        # symlink-resolving check as cleanup_stale_tracks, so the scan and the
        # cleanup agree on what is in scope; otherwise such a file would be
        # indexed on every scan and purged again, churning the database.
        if not self.storage.within_roots(file_path, self.music_folders):
            logger.debug(f"Skipping file outside configured folders: {file_path}")
            return None

        storage = self.storage.for_path(file_path)
        try:
            stat = await asyncio.to_thread(storage.stat, file_path)
        except OSError as e:
            logger.debug(f"File disappeared before processing: {file_path} ({e})")
            return None

        # Quiescence guard: skip files that may still be in mid-upload.
        # POSIX has no "upload complete" signal, so we infer it from mtime/size
        # stability over a short window. If the file is still changing, defer —
        # the next CLOSE_WRITE/MOVED_TO event or scheduled rescan will retry it.
        # A negative age means the mtime came from a clock ahead of ours, as an
        # unsynchronised NAS reports; waiting out the skew would stall the scan
        # for hours per file, and the heuristic cannot say anything either way.
        quiescence_seconds = self.config.quiescence_seconds
        if quiescence_seconds > 0:
            age = time.time() - stat.mtime
            if 0 <= age < quiescence_seconds:
                await asyncio.sleep(quiescence_seconds - age)
                try:
                    stat_after = await asyncio.to_thread(storage.stat, file_path)
                except OSError:
                    logger.debug(
                        f"File disappeared during quiescence wait: {file_path}"
                    )
                    return None
                if (
                    stat_after.size != stat.size
                    or stat_after.mtime_ns != stat.mtime_ns
                ):
                    logger.info(
                        f"File still being written, deferring: {file_path} "
                        f"(size {stat.size}->{stat_after.size}, "
                        f"mtime {stat.mtime}->{stat_after.mtime})"
                    )
                    return None
                stat = stat_after

        file_size = stat.size
        modified_time = stat.mtime
        identity = stat.identity
        device_id = identity.device if identity else None
        inode = identity.inode if identity else None
        # Nanosecond mtime for the failure-cache key. With second resolution a
        # broken file that gets fixed within the same integer second and keeps
        # the same size would collide on the key and never be retried. The
        # tracks table stays on second-resolution modified_time.
        mtime_ns = stat.mtime_ns

        existing_track = await self.db_manager.get_track_by_path(file_path)
        # Storage that will not identify a file cannot have a move detected
        # on it; such a rename reads as a new file plus a stale row.
        if existing_track is None and identity is not None:
            # Move/rename detection: an unknown path whose (device, inode)
            # matches a known file — with the same size and the old path gone —
            # is that file after a rename, not a new one. Re-point the paths
            # and keep the identity (id, first_indexed, evidence, enrichment).
            # A copy (old path still present) or a cross-device move (new
            # inode) legitimately mints a new identity. Works for both inotify
            # moves and rescans, because stale-row cleanup runs after this.
            known = await self.db_manager.get_library_file_by_inode(
                device_id, inode, file_path
            )
            if (
                known
                and known["current_path"] != file_path
                and known["size_bytes"] == file_size
                and await self._path_gone(known["current_path"])
            ):
                logger.info(
                    f"File moved: {known['current_path']} -> {file_path}"
                )
                await self.db_manager.move_track(known["file_id"], file_path)
                existing_track = await self.db_manager.get_track_by_path(
                    file_path
                )
        if (
            existing_track
            and not force
            and existing_track["modified_time"] == modified_time
            and existing_track["file_size"] == file_size
        ):
            # Unchanged content (incl. a pure rename — the paths were just
            # re-pointed above; regrouping for a folder move happens in the
            # clustering pass at the end of this scan).
            logger.debug(f"File unchanged, skipping: {file_path}")
            return None

        # Negative cache: if this exact file (same size + mtime) already
        # failed extraction, don't re-read it on every scan. A file that is
        # still being uploaded changes size/mtime between scans, so it keeps
        # a different key here and is retried until it stabilizes — only a
        # genuinely broken, unchanging file stays parked.
        failure = await self.db_manager.get_failure(file_path)
        if (
            failure
            and failure["modified_time"] == mtime_ns
            and failure["file_size"] == file_size
        ):
            logger.debug(
                f"Skipping previously failed file (unchanged, "
                f"{failure['attempts']} attempt(s)): {file_path}"
            )
            return None

        metadata = await asyncio.to_thread(
            self._extract_metadata, storage, file_path
        )
        if not metadata:
            attempts = await self.db_manager.record_failure(
                file_path, file_size, mtime_ns, "metadata extraction failed"
            )
            logger.warning(
                f"Failed to extract metadata from {file_path} "
                f"(attempt {attempts}); will retry only if the file changes"
            )
            return None

        changes: Dict[str, Optional[str]] = {
            "artists": None,
            "albums": None,
            "tracks": None,
        }

        metadata["file_path"] = file_path
        metadata["file_size"] = file_size
        metadata["modified_time"] = modified_time
        metadata["last_updated"] = int(time.time())

        # Read once per file rather than per field, and only when the tags
        # left something for it to answer.
        from_path = (
            parse_music_path(
                file_path, self.storage.root_of(file_path, self.music_folders)
            )
            if _tags_leave_a_gap(metadata, file_path)
            else None
        )

        artist_id, created = await self._artist_for(metadata, from_path)
        if created:
            changes["artists"] = artist_id
        album_id, created = await self._album_for(
            metadata, from_path, file_path, artist_id
        )
        if created:
            changes["albums"] = album_id

        track_number = _tag_else_path(metadata, from_path, "track_number")
        disc_number = _tag_else_path(metadata, from_path, "disc_number")

        tagged_title = metadata.get("title")
        # The bare filename is reached only when the model is unavailable —
        # the parser already falls back to the stem on its own.
        if tagged_title or from_path:
            title = self._repair_tag_text(tagged_title or from_path.title)
        else:
            title = os.path.basename(file_path)

        track_id = generate_track_id(file_path)
        track_data: Dict[str, Any] = {
            "id": track_id,
            "title": title,
            "album_id": album_id,
            "artist_id": artist_id,
            "duration": metadata.get("duration", 0),
            "track_number": track_number,
            "disc_number": disc_number,
            "file_path": file_path,
            "format": metadata.get("format", "unknown"),
            "file_size": file_size,
            "modified_time": modified_time,
            "replaygain_peak": metadata.get("replaygain_peak"),
            "replaygain_gain": metadata.get("replaygain_gain"),
            "enriched": 0,
            "last_updated": int(time.time()),
        }
        # A single on unknown_album has no album row to carry its embedded
        # cover, so it gets track-level art. Clustering demotions are covered
        # by backfill_embedded_art after the recluster pass.
        if album_id == "unknown_album":
            saved = "album_art" in metadata and await asyncio.to_thread(
                self._save_images, metadata["album_art"], track_id, "track"
            )
            # Written either way: a re-index of a file whose art was removed
            # must drop the cover, which the surgical update would keep.
            track_data["image_url"] = f"{track_id}.jpg" if saved else None
        if existing_track is None:
            await self.db_manager.insert_track(track_data)
        else:
            # Surgical update, not INSERT OR REPLACE: track_data carries only
            # indexer-owned columns, so mbid/embeddings/mood survive. enriched
            # resets to 0, so a changed file re-enriches but reuses embeddings.
            await self.db_manager.update_track(track_id, track_data)
        changes["tracks"] = track_id
        await self._record_origin(
            "track", track_id, "title",
            from_tag=bool(tagged_title), path_source=FILENAME,
        )

        await self.db_manager.upsert_library_file(
            track_id, file_path, file_size, modified_time, device_id, inode
        )

        art_phash = (
            await asyncio.to_thread(self._art_phash, metadata["album_art"])
            if "album_art" in metadata
            else None
        )
        await self.db_manager.upsert_track_evidence(
            track_id,
            {
                "raw_tags": metadata.get("raw_tags"),
                "stream_info": metadata.get("stream_info"),
                "art_phash": art_phash,
                "cue_sheet": metadata.get("cue_sheet"),
                "cue_tracks": metadata.get("cue_tracks"),
            },
        )

        await self.db_manager.update_album_stats(album_id)
        # Successfully indexed — drop any stale failure record (e.g. an
        # earlier partial upload that has since completed).
        await self.db_manager.clear_failure(file_path)
        logger.debug(f"Processed file: {file_path}")
        return changes

    async def _artist_for(
        self, metadata: Dict, from_path
    ) -> Tuple[str, bool]:
        """The artist row this file belongs to, and whether it was created.

        A tag names the artist; failing that, a directory in the path does;
        failing both, the file joins the ``unknown_artist`` sentinel. The
        name is repaired and cleaned first, so two spellings of one artist
        still collapse to one row.
        """
        tagged = metadata.get("artist")
        raw = self._repair_tag_text(
            tagged or (from_path.artist if from_path else None) or "Unknown Artist"
        )
        name = clean_display_name(raw) or "Unknown Artist"
        artist_id = generate_artist_id(name)

        created = await self.db_manager.get_artist_by_id(artist_id) is None
        if created:
            await self.db_manager.insert_artist(
                {
                    "id": artist_id,
                    "name": name,
                    "enriched": 0,
                    "last_updated": int(time.time()),
                }
            )
        await self._record_origin(
            "artist", artist_id, "name",
            from_tag=bool(tagged), path_source=FOLDER_NAME,
        )
        return artist_id, created

    async def _album_for(
        self, metadata: Dict, from_path, file_path: str, artist_id: str
    ) -> Tuple[str, bool]:
        """The album row this file belongs to, and whether it was created.

        Only a tag titles an album here. A folder name is one folder's worth
        of evidence and the clustering pass at the end of the scan is what
        weighs it, so an untagged file joins ``unknown_album`` until then —
        but a year read off that folder is kept, since nothing later supplies
        one for a release no source could identify.
        """
        raw = self._repair_tag_text(metadata.get("album", "Unknown Album"))
        title = clean_display_name(raw) or "Unknown Album"
        album_id = generate_album_id(title, album_folder_for_path(file_path))

        if await self.db_manager.get_album_by_id(album_id) is not None:
            return album_id, False

        album_data: Dict[str, Any] = {
            "id": album_id,
            "title": title,
            "artist_id": artist_id,
            "enriched": 0,
            "last_updated": int(time.time()),
        }
        year = _tag_else_path(metadata, from_path, "year")
        if year is not None:
            album_data["year"] = year
        if "genre" in metadata:
            album_data["genre"] = metadata["genre"]
        if "album_art" in metadata:
            await asyncio.to_thread(
                self._save_images, metadata["album_art"], album_id, "album"
            )
            album_data["image_url"] = f"{album_id}.jpg"
        await self.db_manager.insert_album(album_data)
        if year is not None:
            await self._record_origin(
                "album", album_id, "year",
                from_tag=metadata.get("year") is not None,
                path_source=FOLDER_NAME,
            )
        return album_id, True

    async def _reparse_paths_on_model_change(
        self, available_folders: List[str], changed_items: Dict[str, Set[str]]
    ) -> int:
        """Re-read the path for tracks only their path ever named.

        A scan skips a file whose size and mtime are unchanged, so new model
        weights would otherwise never reach a library that is already
        indexed. This is the channel that replaces the enrichment
        fingerprint, which used to re-open those rows before path parsing
        moved out of the enricher — and it is narrower: rows a tag or an
        external match resolved are never touched.
        """
        identity = json.dumps(get_parser().identity(), sort_keys=True)
        if await self.db_manager.get_filename_model_identity() == identity:
            return 0

        tracks = await self.db_manager.get_tracks_named_by_their_path(FILENAME)
        reparsed = 0
        unreached = False
        for track in tracks:
            path = track.get("file_path")
            if not path:
                continue
            if self.storage.root_of(path, available_folders) is None:
                unreached = True
                continue
            try:
                result = await self.process_file(path, force=True)
            except Exception as e:
                logger.exception(f"Error re-reading {path}: {e}")
                continue
            reparsed += 1
            for key, value in (result or {}).items():
                if value:
                    changed_items[key].add(value)

        if reparsed:
            # A re-read may move a track onto a differently-named artist or
            # album, leaving the old row with nothing in it.
            await self.db_manager.delete_orphaned_albums_and_artists()
        # Recording it now would close the door on the tracks whose root was
        # not mounted; they are re-read once a scan can reach them.
        if not unreached:
            await self.db_manager.set_filename_model_identity(identity)
        return reparsed

    async def _record_origin(
        self,
        entity_type: str,
        entity_id: str,
        field: str,
        *,
        from_tag: bool,
        path_source: str,
    ) -> None:
        """Say whether this scan read the value from a tag or off the path.

        A guess is not allowed to displace a stronger origin on a row more
        than one file contributes to, or the order a folder happened to be
        walked in would decide its provenance. A track row is one file's, so
        it always records what this scan just derived.
        """
        if entity_id in _PLACEHOLDER_IDS:
            return
        await self.db_manager.record_resolved_origin(
            entity_type,
            entity_id,
            field,
            TAG_CONSENSUS if from_tag else path_source,
            OBSERVED if from_tag else GUESSED,
            keep_stronger=not from_tag and entity_type in _SHARED_ENTITIES,
        )

    async def _title_is_ours_to_derive(self, album_id: str) -> bool:
        """Whether this album's title is still locally derived.

        False once an external source has resolved it — see the caller.
        An album with no recorded origin predates provenance and is treated
        as locally derived, which is what it was.
        """
        origin = await self.db_manager.get_resolved_origin(
            "album", album_id, "title"
        )
        return origin is None or origin["source"] in LOCALLY_DERIVED

    def _album_facts_from_path(self, folder_rows: List) -> Dict[str, Any]:
        """What the filename model reads about the album of one folder.

        Parsed from a member file rather than per track: every file in the
        folder shares the directory the album name comes from, so the first
        one that names an album answers for all of them.
        """
        for track, _ in folder_rows:
            path = track.get("file_path")
            if not path:
                continue
            parsed = parse_music_path(
                path, self.storage.root_of(path, self.music_folders)
            )
            if parsed and parsed.album:
                return {
                    "path_album_title": parsed.album,
                    "path_album_year": parsed.year,
                }
        return {}

    def _repair_tag_text(self, text: str) -> str:
        """Mojibake, web-entity and dotted-abbreviation repair for one tag
        string ("Â.Öîé" / "&amp;" / "В.Цой" all read right afterwards)."""
        return repair_tag_text(text, self.config.legacy_tag_encoding)

    def _extract_metadata(
        self, storage: FileStorage, file_path: str
    ) -> Optional[Dict]:
        """Extract metadata from a music file"""
        try:
            mime_type = media_type_of(file_path) or "application/octet-stream"
            if mime_type == "audio/x-flac":
                mime_type = "audio/flac"

            metadata = {
                "format": mime_type,
            }
            if "audio/mpeg" in mime_type:
                with storage.open(file_path) as audio:
                    metadata = self._extract_mp3_metadata(audio, metadata)
            elif "audio/flac" in mime_type:
                with storage.open(file_path) as audio:
                    metadata = self._extract_flac_metadata(audio, metadata)
            else:
                logger.warning(
                    f"Unsupported file format: {file_path}, format: {mime_type}"
                )
                return None
            if metadata is not None:
                self._augment_with_cue(storage, file_path, metadata)
            return metadata
        except Exception as e:
            logger.exception(f"Error extracting metadata from {file_path}: {str(e)}")
            return None

    def _augment_with_cue(
        self, storage: FileStorage, file_path: str, metadata: Dict
    ) -> None:
        """Fill blank fields from a sibling .cue (embedded tags still win) and
        stash the tracklist as evidence. Untagged single-file rips carry their
        real metadata in the .cue, not the folder name."""
        cue_path = find_cue_for(storage, file_path)
        if not cue_path:
            return
        sheet = parse_cue(storage, cue_path)
        if sheet is None:
            return

        my_tracks = sheet.tracks_for(os.path.basename(file_path))
        metadata["cue_sheet"] = cue_path
        # Disc header stored too, so clustering can prefer the cue album title.
        metadata["cue_tracks"] = {
            "album": sheet.title,
            "artist": sheet.performer,
            "tracks": [
                {
                    "number": t.number,
                    "title": t.title,
                    "performer": t.performer,
                    "start_seconds": t.start_seconds,
                }
                for t in my_tracks
            ],
        }

        # One cue track for this file -> per-track rip (use its title/number);
        # many tracks in one file -> whole-disc blob (use the disc title).
        one = my_tracks[0] if len(my_tracks) == 1 else None

        if not metadata.get("artist") and sheet.performer:
            metadata["artist"] = sheet.performer
        if not metadata.get("album") and sheet.title:
            metadata["album"] = sheet.title
        if not metadata.get("title"):
            if one and one.title:
                metadata["title"] = one.title
            elif sheet.title:
                metadata["title"] = sheet.title
        if one and one.number and not metadata.get("track_number"):
            metadata["track_number"] = one.number
        if not metadata.get("genre") and sheet.genre:
            metadata["genre"] = sheet.genre
        if not metadata.get("year") and sheet.date:
            m = re.search(r"\d{4}", sheet.date)
            if m:
                metadata["year"] = int(m.group())

    def _extract_mp3_metadata(self, audio: BinaryIO, metadata: Dict) -> Dict:
        """Extract metadata from an open MP3 file.

        Errors propagate to ``_extract_metadata``, which logs them once.
        """
        mp3 = MP3(audio)
        # MP3 has already read the frames, and re-reading them would mean a
        # second pass over the file — a second network read once the file is
        # on a share. A missing ID3 header is a valid, fully supported case,
        # reported as no tags at all: the audio plays fine, so fall back to
        # an empty set and let the track be indexed from its filename rather
        # than failing.
        id3 = mp3.tags if mp3.tags is not None else ID3()
        metadata["duration"] = int(mp3.info.length)
        if "TIT2" in id3:
            metadata["title"] = str(id3["TIT2"])
        if "TPE1" in id3:
            metadata["artist"] = str(id3["TPE1"])
        if "TALB" in id3:
            metadata["album"] = str(id3["TALB"])
        if "TRCK" in id3:
            track_str = str(id3["TRCK"])
            if "/" in track_str:
                track_str = track_str.split("/")[0]
            try:
                metadata["track_number"] = int(track_str)
            except ValueError:
                pass
        if "TPOS" in id3:
            disc_str = str(id3["TPOS"])
            if "/" in disc_str:
                disc_str = disc_str.split("/")[0]
            try:
                metadata["disc_number"] = int(disc_str)
            except ValueError:
                pass
        if "TDRC" in id3:
            try:
                metadata["year"] = int(str(id3["TDRC"]).split("-")[0])
            except (ValueError, IndexError):
                pass
        if "TCON" in id3:
            metadata["genre"] = str(id3["TCON"])
        if "TXXX:replaygain_track_gain" in id3:
            gain_str = str(id3["TXXX:replaygain_track_gain"])
            try:
                metadata["replaygain_gain"] = float(gain_str.replace(" dB", ""))
            except ValueError:
                pass
        if "TXXX:replaygain_track_peak" in id3:
            peak_str = str(id3["TXXX:replaygain_track_peak"])
            try:
                metadata["replaygain_peak"] = float(peak_str)
            except ValueError:
                pass
        cover = self._mp3_cover(id3)
        if cover is not None:
            metadata["album_art"] = cover
        # Verbatim text frames (incl. TPE2 album-artist, TCMP compilation) +
        # stream info for the clusterer. Binary frames (APIC) skipped.
        metadata["raw_tags"] = {
            key: str(frame)
            for key, frame in id3.items()
            if not key.startswith("APIC")
        }
        metadata["stream_info"] = {
            "sample_rate": getattr(mp3.info, "sample_rate", None),
            "channels": getattr(mp3.info, "channels", None),
            "bitrate": getattr(mp3.info, "bitrate", None),
            "codec": "mp3",
            "encoder": str(id3["TSSE"])
            if "TSSE" in id3
            else (str(id3["TENC"]) if "TENC" in id3 else None),
        }
        return metadata

    def _extract_flac_metadata(self, audio: BinaryIO, metadata: Dict) -> Dict:
        """Extract metadata from an open FLAC file.

        Errors propagate to ``_extract_metadata``, which logs them once.
        """
        flac = FLAC(audio)
        metadata["duration"] = int(flac.info.length)
        if "title" in flac:
            metadata["title"] = flac["title"][0]
        if "artist" in flac:
            metadata["artist"] = flac["artist"][0]
        if "album" in flac:
            metadata["album"] = flac["album"][0]
        if "tracknumber" in flac:
            track_str = flac["tracknumber"][0]
            if "/" in track_str:
                track_str = track_str.split("/")[0]
            try:
                metadata["track_number"] = int(track_str)
            except ValueError:
                pass
        if "discnumber" in flac:
            disc_str = flac["discnumber"][0]
            if "/" in disc_str:
                disc_str = disc_str.split("/")[0]
            try:
                metadata["disc_number"] = int(disc_str)
            except ValueError:
                pass
        if "date" in flac:
            try:
                metadata["year"] = int(flac["date"][0].split("-")[0])
            except (ValueError, IndexError):
                pass
        if "genre" in flac:
            metadata["genre"] = flac["genre"][0]
        if "replaygain_track_gain" in flac:
            gain_str = flac["replaygain_track_gain"][0]
            try:
                metadata["replaygain_gain"] = float(gain_str.replace(" dB", ""))
            except ValueError:
                pass
        if "replaygain_track_peak" in flac:
            peak_str = flac["replaygain_track_peak"][0]
            try:
                metadata["replaygain_peak"] = float(peak_str)
            except ValueError:
                pass
        cover = self._flac_cover(flac)
        if cover is not None:
            metadata["album_art"] = cover
        # Every Vorbis comment verbatim (incl. albumartist/compilation) +
        # stream info. Keys can repeat, so values are lists.
        metadata["raw_tags"] = {key: list(flac[key]) for key in flac.keys()}
        metadata["stream_info"] = {
            "sample_rate": getattr(flac.info, "sample_rate", None),
            "bits_per_sample": getattr(flac.info, "bits_per_sample", None),
            "channels": getattr(flac.info, "channels", None),
            "codec": "flac",
            "encoder": flac["encoder"][0] if "encoder" in flac else None,
        }
        return metadata

    @staticmethod
    def _mp3_cover(id3) -> Optional[bytes]:
        """Front-cover bytes from an ID3 tag set, or None."""
        for tag in ("APIC:", "APIC:Cover", "APIC:CoverFront"):
            if tag in id3:
                return id3[tag].data
        return None

    @staticmethod
    def _flac_cover(flac) -> Optional[bytes]:
        """Front-cover bytes from a FLAC picture block, or None.

        Prefers picture type 3 (cover front), falls back to the first picture.
        """
        pictures = flac.pictures
        for pic in pictures:
            if pic.type == 3:
                return pic.data
        return pictures[0].data if pictures else None

    def _embedded_art(
        self, storage: FileStorage, file_path: str
    ) -> Optional[bytes]:
        """Read just the embedded cover from a file, or None."""
        try:
            mime_type = media_type_of(file_path) or ""
            if "audio/mpeg" in mime_type:
                with storage.open(file_path) as audio:
                    return self._mp3_cover(ID3(audio))
            if mime_type in ("audio/flac", "audio/x-flac"):
                with storage.open(file_path) as audio:
                    return self._flac_cover(FLAC(audio))
        except Exception as e:
            logger.debug("Could not read embedded art from %s: %s", file_path, e)
        return None

    @staticmethod
    def _art_phash(image_data: bytes) -> Optional[str]:
        """64-bit row-difference hash (dHash) of embedded art, 16 hex or None.

        Adjacent-pixel brightness on a 9x8 grayscale downscale. Same cover ->
        small Hamming distance; robust to re-encoding/resize.
        """
        try:
            img = Image.open(io.BytesIO(image_data)).convert("L").resize(
                (9, 8), Image.Resampling.LANCZOS
            )
            px = img.tobytes()  # 72 bytes, one grayscale value per pixel
            bits = 0
            for row in range(8):
                for col in range(8):
                    left = px[row * 9 + col]
                    right = px[row * 9 + col + 1]
                    bits = (bits << 1) | (1 if left > right else 0)
            return f"{bits:016x}"
        except Exception as e:
            logger.debug("art phash failed: %s", e)
            return None

    def _save_images(self, image_data: bytes, entity_id: str, entity_type: str):
        return save_artwork_images(
            self.artwork_path, image_data, entity_id, entity_type
        )

    async def backfill_embedded_art(
        self, available_folders: List[str]
    ) -> Dict[str, int]:
        """Re-extract embedded covers that clustering left behind.

        Two shapes of loss, one cure. An album row minted by a re-cluster
        starts with no cover even when its files embed one (a procedural
        placeholder may have papered over it since). And a loose track
        demoted to ``unknown_album`` loses its per-file album row — and the
        cover reference with it — so it gets track-level art instead.

        Each entity costs one file read, once: the evidence ``art_phash``
        gates the pass to files known to embed a picture, and a recorded
        ``image_url`` stops repeats. Only files under currently available
        roots are touched. Runs before the enricher nudge so embedded art
        wins over a procedural cover.
        """
        counts = {"albums": 0, "tracks": 0}
        for album_id, file_path in await self.db_manager.get_albums_missing_art():
            if await self._restore_embedded_art(
                album_id, file_path, "album", available_folders
            ):
                await self.db_manager.update_album(
                    album_id,
                    {"image_url": f"{album_id}.jpg", "image_generated": 0},
                )
                counts["albums"] += 1
        for track_id, file_path in await self.db_manager.get_singles_missing_art():
            if await self._restore_embedded_art(
                track_id, file_path, "track", available_folders
            ):
                await self.db_manager.update_track(
                    track_id, {"image_url": f"{track_id}.jpg"}
                )
                counts["tracks"] += 1
        return counts

    async def backfill_folder_art(
        self, available_folders: List[str]
    ) -> Dict[str, int]:
        """Take covers from image files sitting in the album's own folder.

        Runs after the embedded-art pass, so a picture inside the file still
        wins: it is unambiguously this record's, while a folder may hold a
        whole sleeve set. What is left is the case nothing else covers — a
        needledrop or a download whose art was saved beside the audio rather
        than tagged into it, which no online source can improve on.

        Costs one directory listing plus a header read per candidate image,
        for albums that have no real cover yet; a recorded ``image_url``
        stops repeats.
        """
        counts = {"albums": 0}
        for album_id, file_path in await self.db_manager.get_albums_without_cover():
            if self.storage.root_of(file_path, available_folders) is None:
                continue
            storage = self.storage.for_path(file_path)
            folder = album_folder_for_path(file_path)
            cover = await asyncio.to_thread(find_folder_cover, storage, folder)
            if not cover:
                continue
            if not await asyncio.to_thread(
                self._save_folder_cover, storage, cover, album_id
            ):
                continue
            await self.db_manager.update_album(
                album_id, {"image_url": f"{album_id}.jpg", "image_generated": 0}
            )
            counts["albums"] += 1
            panel = " (front panel)" if cover.box else ""
            logger.info(f"Cover for album {album_id} taken from {cover.path}{panel}")
        return counts

    def _save_folder_cover(
        self, storage: FileStorage, cover: FolderCover, album_id: str
    ) -> bool:
        """Store an album's cover from an image file beside its audio."""
        try:
            with storage.open(cover.path) as image:
                return save_artwork_from_file(
                    self.artwork_path, image, album_id, "album", cover.box
                )
        except OSError as e:
            logger.debug(f"Could not read folder cover {cover.path}: {e}")
            return False

    async def _restore_embedded_art(
        self,
        entity_id: str,
        file_path: str,
        entity_type: str,
        available_folders: List[str],
    ) -> bool:
        if self.storage.root_of(file_path, available_folders) is None:
            return False
        storage = self.storage.for_path(file_path)
        art = await asyncio.to_thread(self._embedded_art, storage, file_path)
        if not art:
            return False
        return await asyncio.to_thread(self._save_images, art, entity_id, entity_type)

    async def recluster(self) -> Dict[str, int]:
        """Folder-first album grouping over the whole library.

        Plans each folder (folder-first partition + V/A classification), merges
        disc-sibling folders, assigns stable album ids (overlap reattach so an
        unchanged library is a no-op), then applies: album rows, guarded
        album_id reassignment, album_cluster rows, and id aliases. Replaces the
        V/A-only pass when folder_first_clustering is on.
        """
        from ..clustering.cluster_db import AsyncClusterDb
        from ..clustering.identity import assign_stable_ids
        from ..clustering.merge import merge_disc_siblings
        from ..clustering.planner import plan_folder

        rows = await self.db_manager.get_tracks_with_evidence()
        if not rows:
            return {"clusters": 0, "reassigned": 0, "aliases": 0, "orphans": 0}

        by_folder: Dict[str, List] = {}
        current_album_of: Dict[str, str] = {}
        for track, ev in rows:
            folder = album_folder_for_path(track.get("file_path") or "")
            if not folder:
                continue
            by_folder.setdefault(folder, []).append((track, ev))
            current_album_of[track["id"]] = track.get("album_id") or "unknown_album"

        clusters = []
        for folder, folder_rows in by_folder.items():
            clusters.extend(
                plan_folder(
                    folder,
                    folder_rows,
                    legacy_encoding=self.config.legacy_tag_encoding,
                    **self._album_facts_from_path(folder_rows),
                ).clusters
            )
        clusters = merge_disc_siblings(clusters)

        ids, aliases = assign_stable_ids(clusters, current_album_of)

        cluster_db = AsyncClusterDb(self.config)
        va_seeded = False
        reassigned = 0
        affected_albums: Set[str] = set()

        for cluster, album_id in zip(clusters, ids):
            if cluster.kind != "singles_pool":
                anchor = cluster.anchor_artist_id
                if cluster.kind == "compilation" and not va_seeded:
                    await self._ensure_various_artists()
                    va_seeded = True
                existing = await self.db_manager.get_album_by_id(album_id)
                title_is_ours = True
                if existing is None:
                    row = {
                        "id": album_id,
                        "title": cluster.title or "Unknown Album",
                        "artist_id": anchor,
                        "enriched": 0,
                        "last_updated": int(time.time()),
                    }
                    if cluster.year is not None:
                        row["year"] = cluster.year
                    await self.db_manager.insert_album(row)
                else:
                    # Refresh the anchor (e.g. re-point to Various Artists) and
                    # the title. A title an external source resolved is left
                    # alone: this pass rebuilds one from the folder on every
                    # scan, so re-deriving it would revert that correction
                    # every scan. Covers are untouched either way.
                    fixes = {}
                    title_is_ours = await self._title_is_ours_to_derive(album_id)
                    if existing.get("artist_id") != anchor:
                        fixes["artist_id"] = anchor
                    if (
                        cluster.title
                        and existing.get("title") != cluster.title
                        and title_is_ours
                    ):
                        fixes["title"] = cluster.title
                    # A year read off the folder only ever fills a gap: an
                    # album some source already dated keeps that date.
                    if cluster.year is not None and existing.get("year") is None:
                        fixes["year"] = cluster.year
                    if fixes:
                        await self.db_manager.update_album(album_id, fixes)
                if cluster.year is not None:
                    await self._record_origin(
                        "album", album_id, "year", from_tag=False,
                        path_source=FOLDER_NAME,
                    )
                # Only for a title this pass owns: a tag consensus recorded
                # over an external source's origin would read as locally
                # derived on the next scan, and the correction would be
                # rebuilt away by the pass that must not touch it.
                if cluster.title and title_is_ours:
                    await self._record_origin(
                        "album", album_id, "title",
                        from_tag=cluster.title_source == TAG_CONSENSUS,
                        path_source=FOLDER_NAME,
                    )
                await cluster_db.upsert_cluster(
                    album_id,
                    primary_folder=cluster.folder,
                    grouping_conf=1.0,
                    grouping_basis=json.dumps(cluster.grouping_basis),
                    kind=cluster.kind,
                    bump_generation=True,
                )
            affected_albums.add(album_id)
            for tid in cluster.track_ids:
                if current_album_of.get(tid) != album_id:
                    await self.db_manager.reassign_album(tid, album_id)
                    affected_albums.add(current_album_of.get(tid) or "unknown_album")
                    reassigned += 1

        for old_id, new_id in aliases:
            await cluster_db.add_alias(old_id, new_id, "album")

        for album_id in affected_albums:
            if album_id and album_id != "unknown_album":
                await self.db_manager.update_album_stats(album_id)

        deleted_albums, _ = await self.db_manager.delete_orphaned_albums_and_artists()
        return {
            "clusters": len(clusters),
            "reassigned": reassigned,
            "aliases": len(aliases),
            "orphans": deleted_albums,
        }

    async def orphan_va_folder_tracks(self) -> Dict[str, int]:
        """Coalesce a V/A folder's per-track albums into one compilation album.

        Context: the indexer creates one album row per
        ``(album_folder, normalized_title)`` pair. In a V/A folder
        where each track carries its own album tag (e.g. a Jamendo
        playlist), that produces N single-track albums anchored to N
        different artists — a noisy mess in the album list.

        Behaviour: a qualifying compilation folder is collapsed into a
        single album anchored to the ``various_artists`` sentinel and
        titled after the folder (a ``VA -`` prefix is stripped). Each
        track keeps its real ``artist_id`` and still surfaces under
        that artist via ``LocalFilesInputModuleDb.get_artist_orphan_tracks``,
        which returns tracks whose ``album.artist_id != track.artist_id``
        — so the artist-navigation regression that sank an earlier
        umbrella-album attempt no longer applies.

        Two exceptions to the Various-Artists anchoring:
          * A generic dumping ground (a top-level ``music`` dir, a
            personal ``90s Mixes`` pile — see ``_compilation_title``) is
            *not* a real compilation: its tracks are left loose under
            ``unknown_album`` and surface under their artists the same way,
            without inventing a junk album.
          * A folder under a real artist's directory whose tracks are
            remixer-credited (e.g. ``.../Netsky/Remixes``) is that
            artist's own release, so the album is anchored to *them*, not
            Various Artists — see ``_parent_artist_for_folder``.
        The orphaned per-track albums are removed by the cleanup pass.

        Detection criterion is unchanged: a folder qualifies when
        its tracks span ≥``VA_MIN_DISTINCT_ARTISTS`` real artists
        AND the unique-artist-per-track ratio is ≥
        ``VA_MIN_ARTIST_UNIQUENESS``. That keeps mistagging artifacts
        (Abbey Road with 2-3 wrong-artist tags out of 17) from
        flipping to V/A.

        Returns counts of (coalesced_folders, repointed_tracks,
        deleted_orphans).
        """
        tracks = await self.db_manager.get_all_tracks()
        if not tracks:
            return {"folders": 0, "tracks": 0, "orphans": 0}

        # Group tracks by their album folder, and track which folders each
        # artist appears in (used to tell a real artist's remix album from a
        # genuine various-artists compilation).
        folder_tracks: Dict[str, List[Dict]] = {}
        artist_folders: Dict[str, Set[str]] = {}
        for t in tracks:
            folder = album_folder_for_path(t.get("file_path") or "")
            if not folder:
                continue
            folder_tracks.setdefault(folder, []).append(t)
            aid = t.get("artist_id")
            if aid:
                artist_folders.setdefault(aid, set()).add(folder)

        coalesced_folders = 0
        repointed_tracks = 0
        va_seeded = False  # create the Various-Artists sentinel at most once
        for folder, ts in folder_tracks.items():
            distinct_artists = {t["artist_id"] for t in ts if t.get("artist_id")}
            distinct_artists.discard("unknown_artist")
            n_artists = len(distinct_artists)
            n_tracks = len(ts)
            if n_artists < VA_MIN_DISTINCT_ARTISTS:
                continue
            if (n_artists / n_tracks) < VA_MIN_ARTIST_UNIQUENESS:
                continue

            # Decide where the folder's tracks go:
            #   * generic dump        -> stay loose under unknown_album
            #   * folder under a real artist (e.g. ".../Netsky/Remixes") whose
            #     tracks are remixer-credited -> that artist's album (so it
            #     doesn't masquerade as a Various-Artists compilation)
            #   * otherwise           -> a Various-Artists compilation album
            comp_title = _compilation_title(folder)
            album_reanchored = False
            if comp_title is None:
                target_id = "unknown_album"
                dest = "unknown_album"
            else:
                parent_artist = await self._parent_artist_for_folder(
                    folder, artist_folders
                )
                if parent_artist is not None:
                    owner_id = parent_artist["id"]
                    display_title = _strip_artist_prefix(
                        comp_title, parent_artist["name"]
                    )
                    dest = f"album '{display_title}' under {parent_artist['name']}"
                else:
                    owner_id = VARIOUS_ARTISTS_ID
                    display_title = comp_title
                    dest = f"compilation '{display_title}'"
                    if not va_seeded:
                        await self._ensure_various_artists()
                        va_seeded = True
                # Key the id on the title we actually store, so it honours the
                # generate_album_id (folder, normalized_title) invariant and
                # stays stable across re-scans.
                target_id = generate_album_id(display_title, folder)
                album_reanchored = await self._ensure_compilation_album(
                    target_id, display_title, owner_id
                )

            folder_repointed = 0
            for t in ts:
                # Re-point against target_id (not just "!= unknown_album") so
                # this also heals DBs detached by the older behaviour.
                if t["album_id"] != target_id:
                    await self.db_manager.update_track(t["id"], {"album_id": target_id})
                    folder_repointed += 1

            # A folder counts as coalesced when tracks moved OR an existing
            # shared-tag album was re-anchored to Various Artists in place
            # (no re-point needed, but still real work worth reporting).
            if folder_repointed or album_reanchored:
                coalesced_folders += 1
                repointed_tracks += folder_repointed
                # The tracks now belong to target_id; recompute its
                # track_count/duration (the compilation album was created
                # without them, and any source albums are about to be deleted).
                if target_id != "unknown_album":
                    await self.db_manager.update_album_stats(target_id)
                if folder_repointed:
                    logger.info(
                        f"V/A folder '{folder}' ({n_tracks} tracks, "
                        f"{n_artists} artists): {folder_repointed} track(s) -> {dest}"
                    )
                else:
                    logger.info(
                        f"V/A folder '{folder}' ({n_tracks} tracks, "
                        f"{n_artists} artists): re-anchored album -> {dest}"
                    )
            else:
                # Already coalesced on a prior scan; idempotent no-op.
                logger.debug(
                    f"V/A folder '{folder}' already coalesced "
                    f"({n_tracks} tracks, {n_artists} artists)"
                )

        deleted_albums = 0
        if repointed_tracks > 0:
            deleted_albums, _ = (
                await self.db_manager.delete_orphaned_albums_and_artists()
            )

        return {
            "folders": coalesced_folders,
            "tracks": repointed_tracks,
            "orphans": deleted_albums,
        }

    async def _ensure_various_artists(self) -> None:
        """Seed the Various-Artists sentinel artist (compilation albums hang
        off it; real per-track artists are preserved on the tracks)."""
        if not await self.db_manager.get_artist_by_id(VARIOUS_ARTISTS_ID):
            await self.db_manager.insert_artist(
                {
                    "id": VARIOUS_ARTISTS_ID,
                    "name": "Various Artists",
                    "enriched": 0,
                    "last_updated": int(time.time()),
                }
            )

    async def _parent_artist_for_folder(
        self, folder: str, artist_folders: Dict[str, Set[str]]
    ) -> Optional[Dict]:
        """Return the artist that owns ``folder``'s parent directory when it's
        a real artist with a catalog *outside* this folder — i.e. the folder
        is that artist's own remix/mix release, not a true compilation.

        Returns None for generic parents (``music`` etc.) and for netlabels
        whose only content is this folder (they have no tracks elsewhere).
        """
        parent_name = os.path.basename(os.path.dirname(folder)).strip()
        if not parent_name or _GENERIC_FOLDER_RE.match(parent_name):
            return None
        artist_id = generate_artist_id(clean_display_name(parent_name) or parent_name)
        if not (artist_folders.get(artist_id, set()) - {folder}):
            return None
        return await self.db_manager.get_artist_by_id(artist_id)

    async def _ensure_compilation_album(
        self, album_id: str, title: str, artist_id: str
    ) -> bool:
        """Create the compilation album, or re-anchor an existing same-id album
        (process_file may have created it under the first track's artist with a
        different title).

        Returns True if the album was created or re-anchored this call, False if
        it already matched (so a re-scan can tell real work from a no-op).
        """
        existing = await self.db_manager.get_album_by_id(album_id)
        if not existing:
            await self.db_manager.insert_album(
                {
                    "id": album_id,
                    "title": title,
                    "artist_id": artist_id,
                    "enriched": 0,
                    "last_updated": int(time.time()),
                }
            )
            return True
        if existing.get("artist_id") != artist_id or existing.get("title") != title:
            await self.db_manager.update_album(
                album_id,
                {
                    "artist_id": artist_id,
                    "title": title,
                    "last_updated": int(time.time()),
                },
            )
            return True
        return False

    async def _probe_music_roots(self) -> Dict[str, RootStatus]:
        """Availability of each configured root, giving a pending automount a
        bounded chance to complete before the root is called unavailable.

        Together rather than in turn: each root that is not there costs the
        whole retry window, and a scan should not pay for them end to end.
        """
        roots = list(self.music_folders)
        statuses = await asyncio.gather(
            *(
                self.storage.for_path(root).await_root_available(root)
                for root in roots
            )
        )
        return dict(zip(roots, statuses))

    async def _blocked_reason(
        self, status: RootStatus, stored_signature: Optional[str]
    ) -> Optional[str]:
        """Why nothing under this root may be purged right now, or None.

        Three signatures of storage that is gone rather than emptied: the
        root is unavailable; the mount identity no longer matches the one
        the library was indexed from (a static share silently gave way to
        the directory underneath it); or the root is empty with no recorded
        identity to vouch for it. An empty root whose identity still matches
        is a genuinely emptied library and purges normally.

        Emptiness is asked here rather than carried on the verdict because
        this is the only caller that needs it, and the listing it costs is a
        round trip on a share.
        """
        if not status.available:
            return status.reason
        if (
            stored_signature
            and status.identity
            and status.identity != stored_signature
        ):
            return (
                f"the mount changed (indexed from {stored_signature}, "
                f"now {status.identity})"
            )
        if not stored_signature and await self._holds_nothing(status.root):
            return "the folder is empty while the library expects files there"
        return None

    async def _holds_nothing(self, root: str) -> bool:
        """Whether the root offers nothing to index, or will not say.

        A listing that fails or never comes back is not evidence that the
        library was emptied, so it weighs the same way an empty folder does:
        as a reason to keep the rows.
        """
        storage = self.storage.for_path(root)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(storage.is_empty, root), PROBE_TIMEOUT_S
            )
        except (asyncio.TimeoutError, TimeoutError, OSError) as e:
            logger.warning("Could not list %s: %s", root, e)
            return True

    async def cleanup_stale_tracks(self) -> Dict[str, int]:
        """Remove entries that are no longer valid for the file system.

        A track is dropped when its file either no longer exists, or falls
        outside the currently configured music folders. The latter handles a
        changed folder config: when a parent folder is removed from the
        config, its files are no longer accessible to this module and must be
        purged so they aren't served or played. Because ``run_scan`` (and thus
        this method) runs on startup, the cleanup happens immediately after a
        restart with the new config.

        Guarded against unmounted storage: rows under a root that
        :meth:`_blocked_reason` rejects are never purged, and each
        candidate's root is re-probed right before deletion so a share going
        offline mid-sweep cannot masquerade as a deleted library.
        """
        logger.debug("Checking for stale files in the database...")
        root_status = await self._probe_music_roots()
        signatures = {
            root: await self.db_manager.get_root_signature(root)
            for root in self.music_folders
        }
        blocked_reasons = {}
        for root, status in root_status.items():
            reason = await self._blocked_reason(status, signatures[root])
            if reason is not None:
                blocked_reasons[root] = reason
        blocked = set(blocked_reasons)

        all_tracks = await self.db_manager.get_all_tracks()
        candidates: List[Tuple[Dict[str, Any], Optional[str]]] = []
        under_live_root: List[Tuple[Dict[str, Any], str]] = []
        kept_per_root: Dict[str, int] = {}
        for track in all_tracks:
            file_path = track["file_path"]
            root = self.storage.root_of(file_path, self.music_folders)
            if root in blocked:
                kept_per_root[root] = kept_per_root.get(root, 0) + 1
            elif root is None:
                # A file the boundary check also puts outside the roots —
                # left by config change, not by unmount.
                if not self.storage.within_roots(file_path, self.music_folders):
                    candidates.append((track, None))
            else:
                under_live_root.append((track, root))

        gone = await self._missing_paths(
            track["file_path"] for track, _ in under_live_root
        )
        candidates.extend(
            (track, root)
            for track, root in under_live_root
            if track["file_path"] in gone
        )

        for root, count in kept_per_root.items():
            logger.warning(
                f"Music folder {root} looks unmounted "
                f"({blocked_reasons[root]}); "
                f"keeping {count} track(s) that cannot be verified"
            )

        removed_tracks = 0
        # Fresh probe and stat at delete time: the share may have gone
        # offline during the sweep.
        rechecked = sorted({r for _, r in candidates if r is not None})
        recheck = dict(
            zip(
                rechecked,
                await asyncio.gather(
                    *(
                        self.storage.for_path(root).probe_root(root)
                        for root in rechecked
                    )
                ),
            )
        )
        for track, root in candidates:
            file_path = track["file_path"]
            if root is not None:
                status = recheck[root]
                blocked_now = await self._blocked_reason(
                    status, signatures.get(root)
                )
                if blocked_now is not None or not await self._path_gone(file_path):
                    continue
                logger.info(
                    f"File no longer exists, removing from database: {file_path}"
                )
            else:
                logger.info(
                    "File is outside the configured music folders, removing "
                    f"from database: {file_path}"
                )
            await self.db_manager.delete_track(track["id"])
            removed_tracks += 1

        # Drop failure-cache rows for files that have since been deleted or
        # moved out of the configured folders, so the cache doesn't accumulate
        # entries for files this module no longer manages. Rows under a
        # blocked root are kept for the same reason their tracks are.
        checkable = [
            path
            for path in await self.db_manager.get_failure_paths()
            if self.storage.root_of(path, self.music_folders) not in blocked
        ]
        gone_failures = await self._missing_paths(checkable)
        for failed_path in checkable:
            if failed_path in gone_failures or not self.storage.within_roots(
                failed_path, self.music_folders
            ):
                await self.db_manager.clear_failure(failed_path)

        removed_albums, removed_artists = (
            await self.db_manager.delete_orphaned_albums_and_artists()
        )
        if removed_tracks > 0 or removed_albums > 0 or removed_artists > 0:
            logger.info(
                f"Cleanup completed: {removed_tracks} tracks, {removed_albums} albums, "
                f"and {removed_artists} artists removed"
            )
        else:
            logger.debug("No stale entries found in the database")
        return {
            "tracks": removed_tracks,
            "albums": removed_albums,
            "artists": removed_artists,
        }


async def _indexer_worker(config: LocalFilesConfig, db_manager: AsyncIndexerDb):
    """Background worker task for the indexer"""
    global _shutdown_event, _indexer_queue

    try:
        indexer_instance = FileIndexer(config, db_manager)
        await indexer_instance.start()
        logger.info("Indexer worker started")
        interval_minutes = config.scan_interval_minutes
        logger.info(f"Scheduled file indexer to run every {interval_minutes} minutes")
        last_run = time.time()

        while True:
            try:
                try:
                    command = await asyncio.wait_for(_indexer_queue.get(), timeout=30.0)
                    logger.debug(f"Received command: {command}")
                    if command == "scan":
                        logger.info("Manual indexer scan triggered")
                        await indexer_instance.start()
                        last_run = time.time()
                    elif command == "stop":
                        logger.info("Stopping indexer worker")
                        _shutdown_event.set()
                        break
                    elif isinstance(command, dict) and "incremental_changes" in command:
                        logger.info("File watcher detected changes, processing...")
                        await indexer_instance.handle_incremental_changes(
                            command["incremental_changes"]
                        )
                        last_run = time.time()
                    _indexer_queue.task_done()
                except asyncio.TimeoutError:
                    # This is expected - allows periodic checking for scheduled scans
                    pass

                if time.time() - last_run > interval_minutes * 60:
                    logger.debug(
                        f"Scheduled indexer scan triggered (interval: {interval_minutes} mins)"
                    )
                    await indexer_instance.start()
                    last_run = time.time()

                await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info("Indexer worker cancelled.")
                break
            except Exception as e:
                logger.exception(f"Error in indexer worker inner loop: {str(e)}")
                await asyncio.sleep(5)
    except Exception as e:
        logger.exception(f"Fatal error in indexer worker: {str(e)}")
        # Re-raise to ensure the task failure is visible
        raise


def _worth_indexing(kind: ChangeKind, path: str) -> bool:
    """Whether a reported change is one the library acts on.

    A directory event always is — a tree may have arrived whole, or taken
    tracks with it. A file event only matters for a format this module
    indexes, so tidying up a folder of artwork does not cost a database
    sweep.
    """
    if kind in (ChangeKind.DIR_ADDED, ChangeKind.DIR_REMOVED):
        return True
    return is_supported_audio_file(path)


async def _rearm_lost_roots(
    storage: FileStorage,
    watcher: ChangeWatcher,
    lost_roots: Dict[str, bool],
    db: AsyncIndexerDb,
) -> None:
    """Watch the roots that have come back, and rescan the ones that owe it."""
    candidates = [
        root for root in sorted(lost_roots) if not storage.should_defer_probe(root)
    ]
    statuses = await asyncio.gather(
        *(storage.probe_root(root) for root in candidates)
    )
    for root, status in zip(candidates, statuses):
        if not status.available:
            continue
        # Only a root that is actually armed leaves the lost set; one that is
        # back but still unwatchable is retried on the next pass instead of
        # being dropped and never watched again.
        if not await asyncio.to_thread(watcher.watch, root):
            logger.debug("Music folder is back but still unwatchable: %s", root)
            continue
        unmount_seen = lost_roots.pop(root)
        stored = await db.get_root_signature(root)
        if not rearm_needs_rescan(unmount_seen, status.identity, stored):
            logger.info(
                f"Music folder is back (same storage), watching it again: {root}"
            )
            continue
        logger.info(f"Music folder is back, watching it again and rescanning: {root}")
        await _indexer_queue.put(
            {"incremental_changes": {(ChangeKind.DIR_ADDED, root)}}
        )


async def _watch_roots(
    config: LocalFilesConfig,
    storage: FileStorage,
    watcher: ChangeWatcher,
    roots: List[str],
) -> None:
    """Feed one storage's change notifications into the indexer queue.

    Owns the policy around the watcher: which roots are armed, when a lost
    one is tried again, and which changes are worth waking the indexer for.
    The watcher itself only reports what the protocol told it.

    @note Closes the watcher on every way out, arming included: an armed
        watch nobody retires holds its descriptors until the process ends.
    """
    db = AsyncIndexerDb(config)
    # Roots without watches (never armed, unmounted, or deleted), re-armed by
    # the loop below once they come back. True when an unmount lost the root;
    # False when a rescan is owed unconditionally.
    lost_roots: Dict[str, bool] = {}
    next_rearm_check = 0.0

    try:
        # Probed together: an unreachable root costs the probe timeout, and
        # three of them in a row would delay watching the folders that are
        # there.
        statuses = await asyncio.gather(
            *(storage.probe_root(root) for root in roots)
        )
        for root, status in zip(roots, statuses):
            if not status.available:
                lost_roots[root] = False
                logger.warning(
                    f"Music folder is not available yet, deferring watches: "
                    f"{root} ({status.reason})"
                )
                continue
            # Reachable but unwatchable is its own case — inotify watches are
            # a finite per-user resource, and a large library can exhaust
            # them. The root stays in lost_roots so it is armed if they free
            # up.
            if not await asyncio.to_thread(watcher.watch, root):
                lost_roots[root] = False
                logger.warning(
                    f"Music folder cannot be watched for changes, leaving it "
                    f"to the periodic scan: {root}"
                )

        logger.info(
            f"Watching {len(roots) - len(lost_roots)} of {len(roots)} "
            f"{storage.scheme} music folder(s)"
        )

        while not _file_watcher_stop_event.is_set():
            try:
                result = await asyncio.to_thread(
                    watcher.poll, WATCH_POLL_INTERVAL_S
                )

                if lost_roots and time.monotonic() >= next_rearm_check:
                    next_rearm_check = time.monotonic() + REARM_CHECK_INTERVAL_S
                    await _rearm_lost_roots(storage, watcher, lost_roots, db)

                for root in result.unmounted:
                    lost_roots[root] = True
                    logger.warning(
                        f"Storage went away under {root}; "
                        "suspending watches until it returns"
                    )
                for root in result.vanished:
                    lost_roots[root] = False

                changes = {
                    change for change in result.changes if _worth_indexing(*change)
                }
                if changes:
                    logger.info(
                        f"File watcher detected {len(changes)} relevant events "
                        "(added/changed/removed)."
                    )
                    await _indexer_queue.put({"incremental_changes": changes})
                    logger.info("Changes added to indexer queue for processing.")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"Error reading change events: {e}")
                await asyncio.sleep(1)
    finally:
        watcher.close()


async def _watch_one_storage(
    config: LocalFilesConfig,
    storage: FileStorage,
    watcher: ChangeWatcher,
    roots: List[str],
) -> None:
    """One storage's watch loop, whose failure is its own.

    Logged where it happens and raised no further: the loops run together,
    and one giving up would otherwise leave its siblings polling with nobody
    left to close them.
    """
    try:
        await _watch_roots(config, storage, watcher, roots)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Watching %s music folders stopped", storage.scheme)


async def _file_watcher_worker(config: LocalFilesConfig):
    """Background worker for real-time monitoring of the music folders.

    Watches for content arriving (files written or moved in, directories
    created or moved in — a moved-in tree emits no per-file events, so it
    is scanned as a unit) and content leaving (files or directories deleted
    or moved out, which schedules the stale-track cleanup).

    Only some protocols can report changes at all. Roots on storage that
    cannot are left to the periodic scan, and when no root can be watched
    the worker returns rather than idling.
    """
    resolver = build_resolver(config)
    roots_by_storage: Dict[FileStorage, List[str]] = {}
    for root in library_roots(config, resolver):
        roots_by_storage.setdefault(resolver.for_path(root), []).append(root)

    watched: List[Tuple[FileStorage, ChangeWatcher]] = []
    try:
        for storage, roots in roots_by_storage.items():
            watcher = storage.watcher()
            if watcher is None:
                logger.info(
                    f"{storage.scheme} storage cannot report changes; "
                    f"{', '.join(roots)} rely on the periodic scan"
                )
                continue
            watched.append((storage, watcher))

        if not watched:
            logger.info(
                "No music folder can report changes; the periodic scan is "
                "what picks new files up"
            )
            return

        await asyncio.gather(
            *(
                _watch_one_storage(
                    config, storage, watcher, roots_by_storage[storage]
                )
                for storage, watcher in watched
            )
        )
    except asyncio.CancelledError:
        logger.info("File watcher worker cancelled.")
        raise
    finally:
        # Idempotent, so this also covers a loop that never started.
        for _, watcher in watched:
            watcher.close()
        logger.info("File watcher task exited")


def start_indexer(
    config: LocalFilesConfig, db_manager: AsyncIndexerDb
) -> Optional[asyncio.Task]:
    """Start the indexer process in a background task"""
    global _indexer_task
    if _indexer_task and not _indexer_task.done():
        logger.warning("Indexer task already running, not starting another")
        return _indexer_task

    artwork_path = Path(config.artwork_path).expanduser().resolve()
    db_path = Path(config.db_path).expanduser().resolve()

    db_dir = os.path.dirname(db_path)
    if db_dir:  # Only create directory if path is not empty
        os.makedirs(db_dir, exist_ok=True)
    os.makedirs(artwork_path, exist_ok=True)
    os.makedirs(
        os.path.join(artwork_path, "album"),
        exist_ok=True,
    )
    os.makedirs(
        os.path.join(artwork_path, "artist"),
        exist_ok=True,
    )

    _indexer_task = asyncio.create_task(_indexer_worker(config, db_manager))

    # Add done callback to log any unhandled exceptions
    def _on_indexer_task_done(task):
        if task.cancelled():
            logger.info("Indexer task was cancelled")
        elif task.exception():
            logger.exception(f"Indexer task failed with exception: {task.exception()}")
        else:
            logger.info("Indexer task completed normally")

    _indexer_task.add_done_callback(_on_indexer_task_done)
    logger.info("Started file indexer background task")
    return _indexer_task


async def stop_indexer() -> bool:
    """Stop the indexer task"""
    global _indexer_task, _indexer_queue
    if _indexer_task and not _indexer_task.done():
        logger.info("Sending stop command to indexer task")
        try:
            await _indexer_queue.put("stop")
            await asyncio.wait_for(_indexer_task, timeout=7.0)
            logger.info("Indexer task successfully stopped")
            return True
        except asyncio.TimeoutError:
            logger.warning("Indexer task did not stop in time, cancelling.")
            _indexer_task.cancel()
            try:
                await _indexer_task
            except asyncio.CancelledError:
                logger.info("Indexer task was cancelled.")
            return False
        except Exception as e:
            logger.exception(f"Error stopping indexer task: {e}")
            return False
    elif _indexer_task and _indexer_task.done():
        logger.info("Indexer task was already done.")
        return True
    logger.info("No active indexer task to stop.")
    return False


async def stop_file_watcher() -> bool:
    """Stop the file watcher task"""
    global _file_watcher_task, _file_watcher_stop_event
    if _file_watcher_task and not _file_watcher_task.done():
        logger.info("Sending stop signal to file watcher task")
        _file_watcher_stop_event.set()
        try:
            await asyncio.wait_for(_file_watcher_task, timeout=7.0)
            logger.info("File watcher task successfully stopped")
            return True
        except asyncio.TimeoutError:
            logger.warning("File watcher task did not stop in time, cancelling.")
            _file_watcher_task.cancel()
            try:
                await _file_watcher_task
            except asyncio.CancelledError:
                logger.info("File watcher task was cancelled.")
            return False
        except Exception as e:
            logger.exception(f"Error stopping file watcher task: {e}")
            return False
    elif _file_watcher_task and _file_watcher_task.done():
        logger.info("File watcher task was already done.")
        return True
    logger.info("No active file watcher task to stop.")
    return False


def start_file_watcher(config: LocalFilesConfig) -> Optional[asyncio.Task]:
    """Start the file watcher process in a background task if enabled"""
    global _file_watcher_task, _file_watcher_stop_event
    if not config.file_watch_enabled:
        logger.info("File watcher is disabled in config.")
        return None

    if _file_watcher_task and not _file_watcher_task.done():
        logger.warning("File watcher task already running, not starting another")
        return _file_watcher_task

    _file_watcher_stop_event.clear()
    _file_watcher_task = asyncio.create_task(_file_watcher_worker(config))

    # Add done callback to log any unhandled exceptions
    def _on_file_watcher_task_done(task):
        if task.cancelled():
            logger.info("File watcher task was cancelled")
        elif task.exception():
            logger.exception(
                f"File watcher task failed with exception: {task.exception()}"
            )
        else:
            logger.info("File watcher task completed normally")

    _file_watcher_task.add_done_callback(_on_file_watcher_task_done)
    logger.info("Started file watcher background task")
    return _file_watcher_task


# Process orchestration lives in librarian.py, which runs this indexer's
# worker loops and the enricher's in one event loop (Phase 2b). The
# start_indexer / start_file_watcher / stop_* helpers above are its API.
