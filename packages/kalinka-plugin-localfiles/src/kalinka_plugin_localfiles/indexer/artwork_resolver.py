"""Gives every album the cover :mod:`artwork_choice` decides on, and every
track whose own embedded cover differs from its album's that picture.

Each album's decision is kept as the provenance of its ``image_url``: the
cover it should have, what it got, and what its folders held. A pass opens
nothing for an album whose tracks, folders and cached files are as they
were, so it can run over the whole library after every scan and every batch
of watched changes.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import (
    AbstractSet,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from PIL import Image

from ..clustering.signals import ART_SAME_MAX_HAMMING
from ..storage import DirEntry, FileStorage, StorageResolver
from ..utils.artwork_store import (
    ArtworkSave,
    artwork_file,
    artwork_lock,
    dhash_bytes,
    dhash_image,
    store_artwork_from_file,
    store_artwork_images,
)
from kalinka_plugin_sdk.media.folder_art import (
    EXACT,
    NAMED,
    SCAN,
    album_directories,
    find_folder_covers,
    is_cover_image_name,
)
from ..utils.name_utils import album_folder_for_path
from .artwork_choice import (
    EMBEDDED_ART,
    FOLDER_ART,
    NO_ART,
    AlbumFolders,
    Desired,
    EmbeddedChoice,
    FolderPick,
    Member,
    album_folders,
    decide,
    embedded_choices,
    hamming,
    track_variants,
)

logger = logging.getLogger("indexer")

#: How many of a folder's images an old cover is compared with.
_OLD_COVERS_COMPARED = 4

_LOCAL_SOURCES = (FOLDER_ART, EMBEDDED_ART)

#: Image entries per listed directory, as a full scan's walk saw them.
Listings = Mapping[str, Sequence[DirEntry]]

ReadCover = Callable[[FileStorage, str], Optional[bytes]]


class _Attempt(NamedTuple):
    source: str
    path: str
    reason: str
    tier: Optional[str] = None
    box: Optional[Tuple[float, float, float, float]] = None
    phash: Optional[str] = None
    picture_type: Optional[int] = None


class ArtworkResolver:
    """Decides and stores album covers from local art.

    @param db The indexer's database.
    @param storage Reads every configured root.
    @param artwork_path Where cover files are stored.
    @param library_roots The configured music folders; never an album's.
    @param read_cover Reads a file's embedded cover, raising OSError when
        the file cannot be read.
    """

    def __init__(
        self,
        db,
        storage: StorageResolver,
        artwork_path: Path,
        library_roots: Iterable[str],
        read_cover: ReadCover,
    ):
        self.db = db
        self.storage = storage
        self.artwork_path = Path(artwork_path)
        self.library_roots = list(library_roots)
        self.read_cover = read_cover
        self._decisions: Dict[str, Dict[str, Any]] = {}

    async def resolve(
        self,
        available_folders: List[str],
        listings: Optional[Listings] = None,
        dirty_dirs: AbstractSet[str] = frozenset(),
    ) -> int:
        """Bring every album's and track's cover in line with its decision,
        and say how many albums' changed.

        @param available_folders Roots that can be read now; an album under
            any other keeps the cover it has.
        @param listings A full scan's image entries per directory. Folders
            whose images differ from those last seen are searched again.
        @param dirty_dirs Directories a watched change touched, searched
            again without listings to compare against.
        """
        rows = await self.db.get_artwork_inputs()
        covers = await self.db.get_album_covers()
        origins = await self.db.get_cover_origins()
        self._decisions = dict(origins)
        cached = await asyncio.to_thread(self._cached_covers)
        owned = album_folders(
            ((row["album_id"], row["file_path"]) for row in rows), self.library_roots
        )
        track_paths = sorted(row["file_path"] for row in rows)
        members: Dict[str, List[Member]] = defaultdict(list)
        for row in rows:
            members[row["album_id"]].append(
                Member(
                    row["id"],
                    row["file_path"],
                    row["disc_number"],
                    row["track_number"],
                    row["art_phash"],
                    row["art_type"],
                )
            )
        children = _children(listings) if listings is not None else None

        def readable(track: Member) -> bool:
            return self.storage.root_of(track.path, available_folders) is not None

        # An album lives in one folder; loose tracks are anyone's.
        reachable = {
            album_id: (
                [t for t in tracks if readable(t)]
                if album_id == "unknown_album"
                else tracks if readable(tracks[0]) else []
            )
            for album_id, tracks in members.items()
        }
        reachable = {
            album_id: tracks for album_id, tracks in reachable.items() if tracks
        }
        changed = 0
        for album_id in sorted(reachable):
            if album_id == "unknown_album" or album_id not in covers:
                continue
            if await self._resolve_album(
                album_id,
                members[album_id],
                owned[album_id],
                origins.get(album_id),
                covers[album_id],
                album_id in cached,
                track_paths,
                listings,
                children,
                dirty_dirs,
            ):
                changed += 1
        await self._resolve_tracks(
            reachable, {row["id"]: row["image_url"] for row in rows}
        )
        return changed

    async def _resolve_tracks(
        self,
        members: Dict[str, List[Member]],
        current: Dict[str, Optional[str]],
    ) -> None:
        """Store each track's own cover where it differs from its album's,
        once per picture an album's tracks share, and point every other
        track back at its album's.

        The picture is stored under the first track carrying it, whose
        provenance says which picture that was, so it is read again only
        when that changes or the file goes missing.
        """
        albums = self._decisions
        stored = await self.db.get_track_cover_origins()
        cached = await asyncio.to_thread(self._cached_covers, "track")
        wanted: Dict[str, Optional[str]] = {}
        for album_id, tracks in members.items():
            cover = albums.get(album_id) or {}
            for track in tracks:
                wanted[track.track_id] = None
            for group in track_variants(
                tracks,
                cover.get("cover_dhash"),
                cover.get("source"),
                singles=album_id == "unknown_album",
            ):
                key = group[0].track_id
                record = stored.get(key) or {}
                tried = sorted(track.track_id for track in group)
                if record.get("phash") != group[0].phash or not (
                    (record.get("failed") and record.get("tracks") == tried)
                    or (not record.get("failed") and key in cached)
                ):
                    saved = await self._store_track_cover(key, group)
                    if saved is None:
                        for track in group:
                            wanted[track.track_id] = current.get(track.track_id)
                        continue
                    record = {"failed": not saved}
                if not record.get("failed"):
                    for track in group:
                        wanted[track.track_id] = f"{key}.jpg"
        await self.db.set_track_images(
            [
                (image_url, track_id)
                for track_id, image_url in wanted.items()
                if current.get(track_id) != image_url
            ]
        )

    async def _store_track_cover(self, key: str, group: List[Member]) -> Optional[bool]:
        """Store the picture ``group`` shares as track ``key``'s cover, from
        the first of its files that decodes. None when a file could not be
        read, which is tried again next pass."""
        phash = group[0].phash
        unread = False
        for track in group:
            storage = self.storage.for_path(track.path)
            try:
                data = await asyncio.to_thread(self.read_cover, storage, track.path)
            except OSError as e:
                logger.debug(f"Could not read embedded art from {track.path}: {e}")
                unread = True
                continue
            if data is None:
                continue
            outcome = await asyncio.to_thread(
                store_artwork_images, self.artwork_path, data, key, "track", track.path
            )
            if outcome is ArtworkSave.IO_FAILED:
                unread = True
            elif outcome is ArtworkSave.SAVED:
                await self.db.record_track_cover_origin(
                    key, {"phash": phash, "path": track.path}
                )
                return True
        if unread:
            return None
        # Remembered until the picture or the tracks carrying it change.
        await self.db.record_track_cover_origin(
            key,
            {
                "phash": phash,
                "failed": True,
                "tracks": sorted(track.track_id for track in group),
            },
        )
        return False

    async def _resolve_album(
        self,
        album_id: str,
        members: List[Member],
        owned: AlbumFolders,
        stored: Optional[Dict[str, Any]],
        cover: Tuple[Optional[str], int],
        cached: bool,
        track_paths: List[str],
        listings: Optional[Listings],
        children: Optional[Dict[str, List[str]]],
        dirty_dirs: AbstractSet[str],
    ) -> bool:
        storage = self.storage.for_path(members[0].path)
        folder = (stored or {}).get("folder")
        if not owned.roots:
            folder = None
        else:
            signature = (
                _signature(listings, children, owned) if listings is not None else None
            )
            if await self._must_search(
                storage, folder, owned, signature, listings, dirty_dirs
            ):
                try:
                    folder = await self._search(
                        _Listed(storage, listings) if listings else storage,
                        owned,
                        track_paths,
                        signature,
                    )
                except OSError as e:
                    # Unlistable is not empty: keep what was seen before.
                    logger.debug(f"Could not search {owned.roots[0]}: {e}")
                    if folder is None:
                        return False

        choices = embedded_choices(members)
        desired = decide(_pick(folder, "named"), choices, _pick(folder, "scan"))
        if _already_has(stored, desired, cover, cached):
            if folder != (stored or {}).get("folder"):
                await self._decide(
                    self.db.record_cover_origin, album_id, {**stored, "folder": folder}
                )
            return False
        return await self._apply(
            album_id,
            desired,
            choices,
            folder,
            stored,
            cover,
            cached,
            storage,
            members[0].path,
        )

    async def _must_search(
        self,
        storage: FileStorage,
        folder: Optional[Dict[str, Any]],
        owned: AlbumFolders,
        signature: Optional[List],
        listings: Optional[Listings],
        dirty_dirs: AbstractSet[str],
    ) -> bool:
        if folder is None or folder.get("roots") != owned.roots:
            return True
        if signature is not None and folder.get("sig") != signature:
            return True
        if any(
            directory in owned.roots or os.path.dirname(directory) in owned.folders
            for directory in dirty_dirs
        ):
            return True
        if listings is None:
            return False
        # A file replaced in place leaves a local listing unchanged; one on
        # a share carries its size and time, which the signature has.
        for kind in ("named", "scan"):
            pick = _pick(folder, kind)
            if (
                pick is None
                or pick.size is None
                or _listed_with_size(listings, pick.path)
            ):
                continue
            now = await asyncio.to_thread(_fingerprint, storage, pick.path)
            if now != (pick.size, pick.mtime_ns):
                return True
        return False

    async def _search(
        self,
        storage: FileStorage,
        owned: AlbumFolders,
        track_paths: List[str],
        signature: Optional[List],
    ) -> Dict[str, Any]:
        """Search the album's folders, and summarise what they hold."""

        def holds_tracks(directory: str) -> bool:
            prefix = directory.rstrip("/") + "/"
            at = bisect.bisect_left(track_paths, prefix)
            return at < len(track_paths) and track_paths[at].startswith(prefix)

        def search():
            directories = album_directories(
                storage, owned.folders, owned.discs, holds_tracks
            )
            found = find_folder_covers(storage, owned.roots[0], directories)
            candidates = [[c.path, c.box, c.tier] for c in found.covers]
            named = next((c for c in found.covers if c.tier in (EXACT, NAMED)), None)
            scan = next((c for c in found.covers if c.tier == SCAN), None)
            return {
                "roots": owned.roots,
                "sig": signature,
                "named": _pick_record(storage, named),
                "scan": _pick_record(storage, scan),
                "candidates": candidates,
            }

        return await asyncio.to_thread(search)

    async def _apply(
        self,
        album_id: str,
        desired: Desired,
        choices: List[EmbeddedChoice],
        folder: Optional[Dict[str, Any]],
        stored: Optional[Dict[str, Any]],
        cover: Tuple[Optional[str], int],
        cached: bool,
        storage: FileStorage,
        first_path: str,
    ) -> bool:
        base = {"desired": desired.key, "folder": folder}
        if desired.source == NO_ART:
            origin = {**base, "source": NO_ART, "reason": desired.reason}
            local = (stored or {}).get("source") in _LOCAL_SOURCES
            if not local and stored is None and _real(cover) and cached:
                local = await asyncio.to_thread(
                    self._shows_old_folder_cover, storage, album_id, first_path
                )
            if local:
                await self._decide(self.db.clear_local_album_cover, album_id, origin)
                logger.info(
                    "Album %s has no local cover any more; the online sources "
                    "may supply one",
                    album_id,
                )
                return True
            await self._decide(self.db.record_cover_origin, album_id, origin)
            return False

        if stored is None and desired.embedded is not None and _real(cover) and cached:
            # Indexed before decisions were kept: adopt a cover that already
            # is the one decided on, rather than storing it again.
            existing = await asyncio.to_thread(self._stored_dhash, album_id)
            if existing and hamming(existing, desired.embedded.phash) <= (
                ART_SAME_MAX_HAMMING
            ):
                await self._decide(
                    self.db.record_cover_origin,
                    album_id,
                    {
                        **base,
                        "source": EMBEDDED_ART,
                        "phash": desired.embedded.phash,
                        "picture_type": desired.embedded.picture_type,
                        "reason": desired.reason,
                        "cover_dhash": existing,
                    },
                )
                return False

        unread = False
        async with artwork_lock(album_id):
            for attempt in _attempts(folder, choices):
                outcome = await self._store(storage, attempt, album_id)
                if outcome is ArtworkSave.IO_FAILED:
                    unread = True
                    continue
                if outcome is ArtworkSave.SAVED:
                    cover_dhash = await asyncio.to_thread(self._stored_dhash, album_id)
                    await self._decide(
                        self.db.set_album_cover,
                        album_id,
                        {
                            **base,
                            "source": attempt.source,
                            "path": attempt.path,
                            "tier": attempt.tier,
                            "phash": attempt.phash,
                            "picture_type": attempt.picture_type,
                            "reason": attempt.reason,
                            "cover_dhash": cover_dhash,
                        },
                    )
                    logger.info(
                        "Cover for album %s: %s (%s)",
                        album_id,
                        attempt.path,
                        attempt.reason,
                    )
                    return True
        if unread:
            return False
        # Nothing would decode: remembered under the same decision, so the
        # files are not opened again until something changes. A cover still
        # on disk from before stays.
        source = (stored or {}).get("source", NO_ART) if cached else NO_ART
        await self._decide(
            self.db.record_cover_origin,
            album_id,
            {**(stored or {}), **base, "source": source},
        )
        return False

    async def _decide(self, write, album_id: str, origin: Dict[str, Any]) -> None:
        """Write an album's decision, remembered for the track pass after."""
        await write(album_id, origin)
        self._decisions[album_id] = origin

    async def _store(
        self, storage: FileStorage, attempt: _Attempt, album_id: str
    ) -> ArtworkSave:
        if attempt.source == FOLDER_ART:
            return await asyncio.to_thread(
                self._store_folder_image, storage, attempt, album_id
            )
        try:
            data = await asyncio.to_thread(self.read_cover, storage, attempt.path)
        except OSError as e:
            logger.debug(f"Could not read embedded art from {attempt.path}: {e}")
            return ArtworkSave.IO_FAILED
        if data is None:
            return ArtworkSave.UNDECODABLE
        return await asyncio.to_thread(
            store_artwork_images,
            self.artwork_path,
            data,
            album_id,
            "album",
            attempt.path,
        )

    def _store_folder_image(
        self, storage: FileStorage, attempt: _Attempt, album_id: str
    ) -> ArtworkSave:
        try:
            with storage.open(attempt.path) as image:
                return store_artwork_from_file(
                    self.artwork_path,
                    image,
                    album_id,
                    "album",
                    attempt.box,
                    origin=attempt.path,
                )
        except OSError as e:
            logger.debug(f"Could not read folder cover {attempt.path}: {e}")
            return ArtworkSave.IO_FAILED

    def _cached_covers(self, entity_type: str = "album") -> Set[str]:
        """The entities whose cover files are on disk, from one listing."""
        suffix = "_large.jpg"  # written last
        try:
            names = os.listdir(self.artwork_path / entity_type)
        except OSError:
            return set()
        return {name[: -len(suffix)] for name in names if name.endswith(suffix)}

    def _shows_old_folder_cover(
        self, storage: FileStorage, album_id: str, path: str
    ) -> bool:
        """Whether a cover stored before decisions were kept is one of the
        images the folder search then gave it: a folder shared with other
        albums once lent its cover.jpg to each."""
        stored = self._stored_dhash(album_id)
        if stored is None:
            return False
        found = find_folder_covers(storage, album_folder_for_path(path))
        for cover in found.covers[:_OLD_COVERS_COMPARED]:
            try:
                with storage.open(cover.path) as handle, Image.open(handle) as img:
                    img.draft("RGB", (64, 64))
                    if cover.box:
                        w, h = img.size
                        left, top, right, bottom = cover.box
                        img = img.crop((left * w, top * h, right * w, bottom * h))
                    if hamming(dhash_image(img), stored) <= ART_SAME_MAX_HAMMING:
                        return True
            except Exception:  # noqa: BLE001 - an unreadable image is no match
                continue
        return False

    def _stored_dhash(self, album_id: str) -> Optional[str]:
        try:
            data = artwork_file(
                self.artwork_path, "album", album_id, "small"
            ).read_bytes()
        except OSError:
            return None
        return dhash_bytes(data)


def _real(cover: Tuple[Optional[str], int]) -> bool:
    image_url, generated = cover
    return bool(image_url) and not generated


def _already_has(
    stored: Optional[Dict[str, Any]],
    desired: Desired,
    cover: Tuple[Optional[str], int],
    cached: bool,
) -> bool:
    """Whether the album's cover is the one decided on, or the decision is
    one whose files would not decode last time either."""
    if stored is None or stored.get("desired") != desired.key:
        return False
    if stored.get("source") in _LOCAL_SOURCES:
        return _real(cover) and cached
    return True


def _attempts(
    folder: Optional[Dict[str, Any]], choices: List[EmbeddedChoice]
) -> List[_Attempt]:
    """What to store, best first, falling back past files that will not
    decode: the folder images named as covers, the embedded pictures, then
    the folder images chosen by their look — the order :func:`decide`
    prefers them in."""
    candidates = (folder or {}).get("candidates") or []
    attempts = [
        _Attempt(FOLDER_ART, path, "album folder cover", tier, _box(box))
        for path, box, tier in candidates
        if tier in (EXACT, NAMED)
    ]
    for choice in choices:
        attempts.extend(
            _Attempt(
                EMBEDDED_ART,
                member.path,
                choice.reason,
                phash=choice.phash,
                picture_type=member.art_type,
            )
            for member in choice.members
        )
    attempts.extend(
        _Attempt(FOLDER_ART, path, "album folder scan", tier, _box(box))
        for path, box, tier in candidates
        if tier == SCAN
    )
    return attempts


class _Listed:
    """A storage whose directory listings are the ones the scan just took,
    so searching an album's folders costs no second round trip each."""

    def __init__(self, storage: FileStorage, listings: Listings):
        self._storage = storage
        self._listings = listings

    def listdir(self, path: str) -> List[DirEntry]:
        entries = self._listings.get(path)
        return list(entries) if entries is not None else self._storage.listdir(path)

    def __getattr__(self, name: str):
        return getattr(self._storage, name)


def _listed_with_size(listings: Listings, path: str) -> bool:
    name = os.path.basename(path)
    return any(
        entry.name == name and entry.size is not None
        for entry in listings.get(os.path.dirname(path), ())
    )


def _box(box) -> Optional[Tuple[float, float, float, float]]:
    return tuple(box) if box else None


def _fingerprint(storage: FileStorage, path: str) -> Optional[Tuple[int, int]]:
    try:
        stat = storage.stat(path)
    except OSError:
        return None
    return stat.size, stat.mtime_ns


def _pick_record(storage: FileStorage, cover) -> Optional[List]:
    """A chosen folder image as kept in the album's provenance, with the
    file's size and mtime so a replacement is noticed."""
    if cover is None:
        return None
    fingerprint = _fingerprint(storage, cover.path) or (None, None)
    return [cover.path, cover.box, cover.tier, *fingerprint]


def _pick(folder: Optional[Dict[str, Any]], kind: str) -> Optional[FolderPick]:
    record = (folder or {}).get(kind)
    if not record:
        return None
    path, box, tier, size, mtime_ns = record
    return FolderPick(path, _box(box), tier, size, mtime_ns)


def _children(listings: Listings) -> Dict[str, List[str]]:
    children: Dict[str, List[str]] = defaultdict(list)
    for directory in listings:
        children[os.path.dirname(directory)].append(directory)
    return children


def _signature(
    listings: Listings, children: Dict[str, List[str]], owned: AlbumFolders
) -> List:
    """The images a full scan saw where the album's cover may be: in its
    folders, the folders under them, and its disc folders."""
    directories = set(owned.discs)
    for folder in owned.folders:
        directories.add(folder)
        directories.update(children.get(folder, ()))
    return [
        [
            directory,
            sorted(
                [entry.name, entry.size, entry.mtime_ns]
                for entry in listings.get(directory, ())
                if not entry.is_dir and is_cover_image_name(entry.name)
            ),
        ]
        for directory in sorted(directories)
    ]
