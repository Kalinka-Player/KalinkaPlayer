"""The Drives source: mounted volumes, browsed and played from their files."""

from __future__ import annotations

import os
from typing import Callable, FrozenSet, List, Optional, Sequence

from kalinka_plugin_sdk.datamodel import (
    Album,
    Artist,
    BrowseItem,
    BrowseItemList,
    CardSize,
    Catalog,
    EmptyList,
    EntityId,
    EntityType,
    FavoriteIds,
    Playlist,
    Preview,
    PreviewType,
    Track,
)
from kalinka_plugin_sdk.filters import FilterQuery, FilterValueList, UnsupportedFilter, or_unfiltered
from kalinka_plugin_sdk.inputmodule import (
    ContentInfo,
    InputModule,
    ModuleAsset,
    ModuleCapability,
    SearchType,
    SourceUnavailableError,
    TrackInfo,
    TrackSource,
)
from kalinka_plugin_sdk.media.formats import is_supported_audio_file, media_type_of
from kalinka_plugin_sdk.mounts import covers
from kalinka_plugin_sdk.media.tags import TagSummary, title_from_filename
from kalinka_plugin_sdk.storage import StorageLocation, VolumeDescriptor, VolumeKind, VolumeState

from ..browse_source import RegisteredSource
from ..storage.describe import icon_of
from ..storage.manager import StorageManager
from . import fsio, ids
from .artwork import EMBEDDED, FOLDER, DriveArtwork, cover_urls
from .fsio import DriveIO
from .listing import (
    FileEntry,
    FolderCounts,
    ListingCache,
    TagCache,
    count_folder,
    entry_of,
    folder_subname,
    shown,
    walk_tracks,
)

DISPLAY_NAME = "Drives"
ICON = "storage_outlined"

#: How long a page waits on tags before showing file names for the rest.
PAGE_TAG_BUDGET_S = 2.0
#: How long resolving tracks for the queue waits on tags.
QUEUE_TAG_BUDGET_S = 6.0
#: The most entries one page lists, whatever it asks for.
MAX_PAGE = 200


def _folder_preview() -> Preview:
    return Preview(type=PreviewType.FOLDER, icon="folder", items_count=3, rows_count=1, card_size=CardSize.SMALL)


def _catalog(item_id: EntityId, title: str, **fields) -> Catalog:
    """A page of a drive: what it lists changes as files are copied to the
    drive, and it is on storage."""
    return Catalog(
        id=item_id, title=title, preview_config=_folder_preview(), live=True, on_storage=True, **fields
    )


class DrivesModule(InputModule):
    """Every mounted volume as folders, and their files as playable tracks.

    Holds no state of its own beyond caches: the volumes are the storage
    manager's, and a track is its path. Nothing here searches, keeps
    favourites or edits playlists; those belong to a library.

    @param enabled Whether Drives shows as a source.
    @param io The threads files are read on, shared with whatever else
        reads the drives; Drives' own when not given.
    """

    def __init__(
        self,
        storage: StorageManager,
        cache_dir: str,
        enabled: Callable[[], bool] = lambda: True,
        io: Optional[DriveIO] = None,
    ):
        self._storage = storage
        self._enabled = enabled
        self._io = io or DriveIO()
        self._tags = TagCache()
        self._listings = ListingCache()
        self._artwork = DriveArtwork(os.path.join(cache_dir, "drives-art"), self._allowed)

    def module_name(self) -> str:
        return ids.SOURCE

    def display_name(self) -> str:
        return DISPLAY_NAME

    def capabilities(self) -> FrozenSet[ModuleCapability]:
        if self._enabled() and self._drives():
            return frozenset({ModuleCapability.CATALOGS})
        return frozenset()

    def count(self) -> Optional[int]:
        """How many drives Drives lists, for a client to show on its icon."""
        drives = len(self._drives()) if self._enabled() else 0
        return drives or None

    def registered_source(self) -> RegisteredSource:
        """Drives as the browse root lists it."""
        return RegisteredSource(
            name=self.module_name(),
            title=self.display_name(),
            source=self,
            builtin=True,
            preview=_folder_preview(),
            icon=ICON,
            live=True,
            on_storage=True,
            count=self.count,
        )

    def close(self) -> None:
        self._tags.shutdown()

    async def _counts(self, paths: Sequence[str]) -> list[Optional[FolderCounts]]:
        """Each folder's counts, or None for one not counted within
        COUNT_BUDGET_S: a slow drive lists its folders without them."""
        return await self._io.each_within(count_folder, paths, fsio.COUNT_BUDGET_S)

    def _drives(self) -> list[VolumeDescriptor]:
        return [
            v for v in self._storage.volumes()
            if v.kind != VolumeKind.INTERNAL and v.state == VolumeState.MOUNTED
        ]

    def _mounted(self) -> list[VolumeDescriptor]:
        return [v for v in self._storage.volumes() if v.state == VolumeState.MOUNTED]

    def _allowed(self, real: str) -> bool:
        """Whether ``real``, a resolved path, lies on a mounted volume."""
        return any(covers(v.mount_point, real) for v in self._mounted())

    async def _resolved(self, path: Optional[str]) -> str:
        if path is None:
            raise LookupError("not a path on a drive")
        real = await self._io.run(os.path.realpath, path)
        if not self._allowed(real):
            raise SourceUnavailableError(self._absent_reason(path))
        return real

    def _absent_reason(self, path: str) -> str:
        for volume in self._storage.volumes():
            if covers(volume.mount_point, path):
                return f"{volume.label} is not connected"
        return "The drive this is on is not connected"

    async def browse(
        self,
        entity_id: EntityId,
        offset: int = 0,
        limit: int = 50,
        filter: Optional[FilterQuery] = None,
    ) -> BrowseItemList:
        or_unfiltered(filter).reject_undeclared(())
        if entity_id.type == EntityType.CATALOG:
            endpoint = entity_id.id
            if endpoint == ids.ROOT:
                return await self._browse_root(offset, limit)
            if endpoint.startswith(ids.VOLUME_PREFIX):
                volume = self._storage.volume(endpoint[len(ids.VOLUME_PREFIX):])
                if volume is None or volume.state != VolumeState.MOUNTED:
                    return EmptyList(offset, limit)
                return await self._browse_folder(volume.mount_point, offset, limit)
            if endpoint.startswith(ids.FOLDER_PREFIX):
                path = await self._resolved(ids.decode_path(endpoint[len(ids.FOLDER_PREFIX):]))
                return await self._browse_folder(path, offset, limit)
        if entity_id.type == EntityType.ALBUM:
            path = await self._resolved(ids.decode_path(entity_id.id))
            return await self._browse_folder(path, offset, limit, tracks_only=True)
        return EmptyList(offset, limit)

    async def _browse_root(self, offset: int, limit: int) -> BrowseItemList:
        if not self._enabled():
            return EmptyList(offset, limit)
        drives = self._drives()
        if len(drives) == 1:
            return await self._browse_folder(drives[0].mount_point, offset, limit)
        return self._page([self._volume_item(v) for v in drives], offset, limit)

    @staticmethod
    def _page(items: list[BrowseItem], offset: int, limit: int) -> BrowseItemList:
        return BrowseItemList(offset=offset, limit=limit, total=len(items), items=items[offset : offset + limit])

    def _volume_item(self, volume: VolumeDescriptor) -> BrowseItem:
        item_id = ids.volume_id(volume.id)
        return BrowseItem(
            id=item_id,
            name=volume.label,
            can_browse=True,
            can_add=True,
            subname=volume.summary,
            icon=icon_of(volume),
            catalog=_catalog(item_id, volume.label, description=volume.summary),
        )

    async def _browse_folder(
        self, path: str, offset: int, limit: int, tracks_only: bool = False
    ) -> BrowseItemList:
        limit = min(limit, MAX_PAGE)
        try:
            contents = await self._io.run(self._listings.list, path)
        except OSError as e:
            raise SourceUnavailableError(f"{shown(os.path.basename(path) or path)} cannot be read") from e
        folders = [] if tracks_only else contents.folders
        page_folders = folders[offset : offset + limit]
        track_offset = max(0, offset - len(folders))
        page_tracks = contents.tracks[track_offset : track_offset + limit - len(page_folders)]

        counts = await self._counts([p for _, p in page_folders])
        folder_items = [
            await self._folder_item(name, folder, count)
            for (name, folder), count in zip(page_folders, counts)
        ]
        tags = await self._io.run(
            self._tags.read_all, page_tracks, PAGE_TAG_BUDGET_S, timeout=PAGE_TAG_BUDGET_S + fsio.FS_TIMEOUT_S
        )
        track_items = [
            self._track_item(entry, tags.get(entry.path), contents.has_images) for entry in page_tracks
        ]
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=len(folders) + len(contents.tracks),
            items=folder_items + track_items,
        )

    async def _folder_item(self, name: str, path: str, counts: Optional[FolderCounts]) -> BrowseItem:
        item_id = ids.folder_id(path)
        image = cover_urls(FOLDER, path) if counts is not None and (counts.has_images or counts.tracks) else None
        name = shown(name)
        return BrowseItem(
            id=item_id,
            name=name,
            can_browse=True,
            can_add=counts is None or bool(counts.folders or counts.tracks),
            subname=folder_subname(counts, await self._storage.library_holding(path)),
            icon="folder",
            catalog=_catalog(item_id, name, image=image),
        )

    def _track(self, entry: FileEntry, summary: Optional[TagSummary], folder_has_images: bool) -> Track:
        folder = os.path.dirname(entry.path)
        summary = summary or TagSummary()
        if summary.has_cover:
            image = cover_urls(EMBEDDED, entry.path)
        elif folder_has_images:
            image = cover_urls(FOLDER, folder)
        else:
            image = None
        album_artist = summary.album_artist or summary.artist
        album = Album(
            id=ids.album_id(folder),
            title=summary.album or shown(os.path.basename(folder) or folder),
            year=summary.year,
            image=image,
            artist=Artist(id=ids.artist_id(album_artist), name=album_artist) if album_artist else None,
        )
        return Track(
            id=ids.track_id(entry.path),
            title=summary.title or shown(title_from_filename(entry.path)),
            duration=int(summary.duration_s or 0),
            track_number=summary.track_number,
            disc_number=summary.disc_number,
            performer=Artist(id=ids.artist_id(summary.artist), name=summary.artist) if summary.artist else None,
            album=album,
        )

    def _track_item(self, entry: FileEntry, summary: Optional[TagSummary], folder_has_images: bool) -> BrowseItem:
        track = self._track(entry, summary, folder_has_images)
        return BrowseItem(
            id=track.id,
            name=track.title,
            can_browse=False,
            can_add=True,
            subname=track.performer.name if track.performer else None,
            track=track,
            icon="track",
        )

    async def get(self, entity_id: EntityId) -> BrowseItem:
        items = await self.get_all([entity_id])
        if not items:
            raise LookupError(entity_id.to_string)
        return items[0]

    async def get_all(self, entity_ids: List[EntityId]) -> List[BrowseItem]:
        paths: list[str] = []
        others: dict[str, BrowseItem] = {}
        for entity_id in entity_ids:
            if entity_id.type == EntityType.TRACK:
                path = ids.decode_path(entity_id.id)
                if path is not None:
                    paths.append(path)
            else:
                item = await self._container_item(entity_id)
                if item is not None:
                    others[entity_id.to_string] = item
        tracks: list[FileEntry] = await self._io.run(
            self._entries, paths, timeout=max(fsio.FS_TIMEOUT_S, len(paths) / 100)
        ) if paths else []
        tags = await self._io.run(
            self._tags.read_all, tracks, QUEUE_TAG_BUDGET_S, timeout=QUEUE_TAG_BUDGET_S + fsio.FS_TIMEOUT_S
        )
        folders = list(dict.fromkeys(os.path.dirname(entry.path) for entry in tracks))
        images = dict(zip(folders, await self._counts(folders)))
        by_path = {}
        for entry in tracks:
            counts = images.get(os.path.dirname(entry.path))
            by_path[ids.track_id(entry.path).to_string] = self._track_item(
                entry, tags.get(entry.path), counts is not None and counts.has_images
            )
        resolved = {**others, **by_path}
        return [resolved[e.to_string] for e in entity_ids if e.to_string in resolved]

    def _entries(self, paths: Sequence[str]) -> list[FileEntry]:
        """The files of ``paths`` that are on a volume, as they are now.

        @note Blocks on the drive; runs on Drives' threads.
        """
        found = []
        for path in paths:
            if self._allowed(os.path.realpath(path)):
                entry = entry_of(path)
                if entry is not None:
                    found.append(entry)
        return found

    async def _container_item(self, entity_id: EntityId) -> Optional[BrowseItem]:
        if entity_id.type == EntityType.CATALOG:
            endpoint = entity_id.id
            if endpoint.startswith(ids.VOLUME_PREFIX):
                volume = self._storage.volume(endpoint[len(ids.VOLUME_PREFIX):])
                return self._volume_item(volume) if volume is not None else None
            if endpoint.startswith(ids.FOLDER_PREFIX):
                path = ids.decode_path(endpoint[len(ids.FOLDER_PREFIX):])
                if path is None or not self._allowed(await self._io.run(os.path.realpath, path)):
                    return None
                (counts,) = await self._counts([path])
                return await self._folder_item(os.path.basename(path) or path, path, counts)
        return None

    async def tracks_to_add(self, entity_id: EntityId, limit: int) -> Optional[List[EntityId]]:
        path = self._container_path(entity_id)
        if path is None:
            return None
        real = await self._resolved(path)
        try:
            if entity_id.type == EntityType.ALBUM:
                contents = await self._io.run(self._listings.list, real, timeout=3 * fsio.FS_TIMEOUT_S)
                paths = [t.path for t in contents.tracks][:limit]
            else:
                paths = await self._io.run(walk_tracks, real, limit, timeout=6 * fsio.FS_TIMEOUT_S)
        except OSError as e:
            raise SourceUnavailableError(f"{shown(os.path.basename(real) or real)} cannot be read") from e
        return [ids.track_id(p) for p in paths]

    def _container_path(self, entity_id: EntityId) -> Optional[str]:
        if entity_id.type == EntityType.ALBUM:
            return ids.decode_path(entity_id.id)
        if entity_id.type != EntityType.CATALOG:
            return None
        endpoint = entity_id.id
        if endpoint == ids.ROOT:
            drives = self._drives()
            return drives[0].mount_point if len(drives) == 1 else None
        if endpoint.startswith(ids.VOLUME_PREFIX):
            volume = self._storage.volume(endpoint[len(ids.VOLUME_PREFIX):])
            return volume.mount_point if volume is not None else None
        if endpoint.startswith(ids.FOLDER_PREFIX):
            return ids.decode_path(endpoint[len(ids.FOLDER_PREFIX):])
        return None

    async def storage_location(self, entity_id: EntityId) -> Optional[StorageLocation]:
        """Where a folder, volume or album page of this source is on storage."""
        path = self._container_path(entity_id)
        if path is None:
            return None
        real = await self._io.run(os.path.realpath, path)
        if not self._allowed(real):
            return None
        return self._storage.location_of(real) or StorageLocation(volume_id="", path=real)

    async def _playable(self, track: str) -> Optional[str]:
        """The file a track plays from, or None when it is not there."""
        path = ids.decode_path(track)
        if path is None or not is_supported_audio_file(path):
            return None
        real = await self._resolved(path)
        return real if await self._io.run(os.path.isfile, real) else None

    async def get_track_source(self, track_id: str) -> TrackSource:
        real = await self._playable(track_id)
        if real is None:
            raise LookupError(track_id)
        entry = await self._io.run(entry_of, real)
        hit, summary = self._tags.cached((real, entry.size, entry.mtime_ns)) if entry else (False, None)
        return TrackSource(
            source=ModuleAsset(module=ids.SOURCE, asset_id=track_id),
            format=media_type_of(real) or "application/octet-stream",
            audio_codec=summary.codec if hit and summary else None,
        )

    async def get_content_info(self, asset_id: str) -> Optional[ContentInfo]:
        real = await self._playable(asset_id)
        if real is None:
            return None
        return ContentInfo(
            mime_type=media_type_of(real) or "application/octet-stream",
            local_path=real,
            cacheable=True,
        )

    async def get_resource_path(self, id: str) -> str | None:
        if not id.startswith(f"{ids.SOURCE}/"):
            return None
        try:
            return await self._io.run(self._artwork.path_of, id, timeout=3 * fsio.FS_TIMEOUT_S)
        except SourceUnavailableError:
            return None

    async def search(self, type: SearchType, query: str, offset=0, limit=50) -> BrowseItemList:
        return EmptyList(offset, limit)

    async def ai_search(self, query: str, offset: int = 0, limit: int = 50) -> BrowseItemList:
        return EmptyList(offset, limit)

    async def get_track_info(self, track_ids: List[str]) -> List[TrackInfo]:
        return []

    async def list_favorite(
        self, type: SearchType, filter: str, offset: int = 0, limit: int = 50
    ) -> BrowseItemList:
        return EmptyList(offset, limit)

    async def get_favorite_ids(self) -> FavoriteIds:
        return FavoriteIds()

    async def add_to_favorite(self, id: str):
        raise LookupError("Drives keeps no favourites")

    async def remove_from_favorite(self, id: str):
        raise LookupError("Drives keeps no favourites")

    async def list_filter_values(
        self, catalog_id: EntityId, field: str, offset: int = 0, limit: int = 50, q: str = ""
    ) -> FilterValueList:
        raise UnsupportedFilter(f"Drives lists no values for {field}")

    async def playlist_user_list(self, offset: int = 0, limit: int = 25) -> BrowseItemList:
        return EmptyList(offset, limit)

    async def playlist_create(self, name: str, description: str) -> Playlist:
        raise LookupError("Drives keeps no playlists")

    async def playlist_update(self, id: str, name: Optional[str], description: Optional[str]) -> Playlist:
        raise LookupError("Drives keeps no playlists")

    async def playlist_delete(self, id: str):
        raise LookupError("Drives keeps no playlists")

    async def playlist_add_tracks(self, id: str, track_ids: List[str], allow_duplicates: bool) -> Playlist:
        raise LookupError("Drives keeps no playlists")

    async def playlist_remove_tracks(self, id: str, playlist_track_ids: List[str]) -> Playlist:
        raise LookupError("Drives keeps no playlists")
