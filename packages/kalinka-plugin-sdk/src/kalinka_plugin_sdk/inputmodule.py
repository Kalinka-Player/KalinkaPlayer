import asyncio

from pydantic import BaseModel, PositiveInt, ConfigDict
from enum import Enum
from typing import (
    Awaitable,
    BinaryIO,
    Callable,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

from .datamodel import (
    BrowseItem,
    EntityId,
    Playlist,
    Track,
    BrowseItemList,
    FavoriteIds,
    EmptyList,
)
from .filters import FilterQuery, FilterValueList


class SourceUnavailableError(RuntimeError):
    """A track's backing storage is temporarily unreachable.

    Raised by :meth:`InputModule.get_content_info` or a track's
    ``source_retriever`` when the content exists but cannot be served right
    now — an unmounted network share, an offline backend. Distinct from a
    missing asset: callers should surface it as a transient condition (the
    server answers a content fetch with 503 rather than 404). The message is
    shown to users, so keep it presentable.
    """


class ModuleAsset(BaseModel):
    """
    Content the server fetches from the module and serves on its behalf.

    The module names an asset; the server owns the URL clients and renderers
    are given, and binds it to an address the fetcher can actually reach. A
    module must never build that URL itself — it cannot know which of the
    server's interfaces the fetcher is on.

    The module must be able to resolve ``asset_id`` through
    :meth:`InputModule.get_content_info`.

    Attributes:
        module (str): The owning module's :meth:`InputModule.module_name`
        asset_id (str): Module-minted id, opaque to the server and usable as a
                        single URL path segment
    """

    module: str
    asset_id: str


class DirectUrl(BaseModel):
    """
    An absolute URL the fetcher retrieves for itself, untouched by the server.

    For content already served somewhere reachable — a CDN, a public stream.
    The URL must be absolute and Range-capable, since a renderer seeks by
    asking for byte ranges.

    Attributes:
        url (str): The absolute streaming URL
    """

    url: str


class TrackSource(BaseModel):
    """
    Where a track's audio comes from, and in what format.

    Attributes:
        source (ModuleAsset | DirectUrl): Server-proxied asset, or an absolute URL
        format (str): The audio format/codec (e.g., "mp3", "flac", "aac")
    """

    source: ModuleAsset | DirectUrl
    format: str


class ContentInfo(BaseModel):
    """
    What the server needs to serve one asset of a module's content.

    The module says how the bytes can be reached, in one of two ways, and
    the server answers byte-range requests either way. ``local_path`` is
    preferred where it applies: the kernel serves the file and nothing is
    copied through this process. ``reader`` is for content only the module
    can reach — a share it speaks to itself, an object store — and needs
    ``size``, because a renderer reads the stream length out of
    ``Content-Range`` and cannot seek without it.

    Attributes:
        mime_type (str): Content-Type to serve the asset as
        local_path (Optional[str]): A file the server may read directly.
        reader (Optional[Callable[[], BinaryIO]]): Opens the asset for
            reading. Called once per request and given back a fresh,
            seekable, binary stream each time; the server closes it. Ignored
            when ``local_path`` is set.
        size (Optional[int]): Byte length. Required alongside ``reader``;
            the server measures a local file for itself.
        cacheable (bool): Whether the server may hold on to these bytes.
    """

    mime_type: str
    local_path: Optional[str] = None
    reader: Optional[Callable[[], BinaryIO]] = None
    size: Optional[int] = None
    cacheable: bool = False


class TrackInfo(BaseModel):
    """
    Complete track information including metadata and source retrieval.

    This class provides all the information needed to play a track, including
    a callable that can retrieve the actual audio source when needed.

    Attributes:
        id (EntityId): Unique identifier for the track
        source_retriever: Callable[[], Awaitable[TrackSource]]: Function that returns the track's audio source
        metadata (Optional[Track]): Track metadata (title, artist, album, etc.)
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: EntityId
    source_retriever: Callable[[], Awaitable[TrackSource]]
    metadata: Optional[Track]


class SearchType(str, Enum):
    """
    Enumeration of searchable content types within input modules.

    Values:
        album: Search for albums
        track: Search for individual tracks
        playlist: Search for playlists
        artist: Search for artists
    """

    album = "album"
    track = "track"
    playlist = "playlist"
    artist = "artist"


@runtime_checkable
class InputModule(Protocol):
    """
    Protocol for Kalinka audio input modules.

    This protocol defines the interface that all input modules must implement to provide
    audio content to the Kalinka player system. Input modules can represent various
    audio sources such as streaming services, local files, radio stations, etc.

    Each input module is responsible for:
    - Browsing and searching audio content
    - Managing user favorites
    - Handling playlists
    - Providing track metadata and playback sources
    - Declaring what its catalogs can be filtered by, and honouring it

    Latency contract: every call into this interface serves a real-time
    request, and the server enforces a hard per-call timeout (3 seconds) on
    its side — a call that overruns is cancelled and treated as failed, no
    matter what the module was doing. Implementations must return within
    that budget: configure backend HTTP timeouts to fail fast, don't retry
    requests that already timed out, and never run provisioning or other
    long work inline in a request path (start it in the background and
    return what is available).
    """

    def module_name(self) -> str:
        """
        Return the unique name identifier for this input module.

        Returns:
            str: A unique string identifier for this module (e.g., "spotify", "localfiles")
        """
        ...

    def display_name(self) -> str:
        """
        Return a short, human-friendly name for this source, shown in section
        headers (e.g. "My Library", "Jamendo"). Defaults to module_name().

        Returns:
            str: The display name for this source
        """
        return self.module_name()

    async def ai_search(self, query: str, offset: int = 0, limit: int = 50) -> BrowseItemList:
        """
        Search using natural language / AI / semantic interpretation.

        Unlike search(), no content type is specified — the module decides
        what kinds of results are relevant. Default returns an empty list.

        Args:
            query (str): A natural language query (e.g. "upbeat 90s rock for a road trip")
            offset (int): Pagination offset. Defaults to 0.
            limit (int): Max results to return. Defaults to 50.

        Returns:
            BrowseItemList: Matching items, possibly mixed types via .sections

        Must complete within the server's per-call timeout (see the
        class docstring's latency contract).
        """
        return EmptyList(offset, limit)

    async def search(
        self, type: SearchType, query: str, offset=0, limit=50
    ) -> BrowseItemList:
        """
        Search for content within this input module.

        Args:
            type (SearchType): The type of content to search for (album, track, playlist, artist)
            query (str): The search query string
            offset (int, optional): Number of results to skip for pagination. Defaults to 0.
            limit (int, optional): Maximum number of results to return. Defaults to 50.

        Returns:
            BrowseItemList: A list of matching items found by the search

        Must complete within the server's per-call timeout (see the
        class docstring's latency contract).
        """
        ...

    async def browse(
        self,
        entity_id: EntityId,
        offset: PositiveInt = 0,
        limit: PositiveInt = 50,
        filter: Optional[FilterQuery] = None,
    ) -> BrowseItemList:
        """
        Browse content within a specific entity (e.g., album contents, artist's albums).

        Args:
            entity_id (EntityId): The ID of the entity to browse into. Use "kalinka:<device_name>:catalog:root"
                                 to access the root level of the module.
            offset (PositiveInt, optional): Number of results to skip for pagination. Defaults to 0.
            limit (PositiveInt, optional): Maximum number of results to return. Defaults to 50.
            filter (FilterQuery, optional): Constraints to satisfy, addressed by
                the field ids this entity's Catalog declared in ``filters``.
                None is the unconstrained listing.

        Returns:
            BrowseItemList: A list of items contained within the specified entity,
                narrowed by ``filter``. ``total`` counts the filtered listing, so
                pagination stays meaningful under a filter.

        Raises:
            UnsupportedFilter: If ``filter`` carries a field this entity did not
                declare, or an operation this source cannot honour. Never drop a
                constraint instead — a listing that looks filtered but is not is
                worse than an error.

        Must complete within the server's per-call timeout (see the
        class docstring's latency contract).
        """
        ...

    async def get_track_info(self, track_ids: List[str]) -> List[TrackInfo]:
        """
        Retrieve detailed track information including playback sources.

        This method is called when the player needs to actually play tracks,
        providing both metadata and a callable to get the audio source.

        Args:
            track_ids (List[str]): List of track IDs to get information for

        Returns:
            List[TrackInfo]: List of track information objects containing
                     metadata and source retrievers for each requested track, suitable to insert into the play queue.

        Must complete within the server's per-call timeout (see the
        class docstring's latency contract).
        """
        ...

    async def get_content_info(self, asset_id: str) -> Optional[ContentInfo]:
        """
        Resolve an asset this module asked the server to serve for it.

        Called on every fetch of a :class:`ModuleAsset` this module named — so
        this is where access is granted or refused, not only where the source
        was first handed out. Return None for an id that is unknown, gone, or
        no longer permitted; the server answers 404 and never learns why.

        Modules that hand out only :class:`DirectUrl` need not implement this.

        Args:
            asset_id (str): The id from the ModuleAsset

        Returns:
            Optional[ContentInfo]: How to serve the asset, or None

        Must complete within the server's per-call timeout (see the
        class docstring's latency contract) — it answers metadata, while
        the bytes are served by the server afterwards.
        """
        return None

    async def list_favorite(
        self, type: SearchType, filter: str, offset: int = 0, limit: int = 50
    ) -> BrowseItemList:
        """
        List user's favorite items of a specific type.

        Args:
            type (SearchType): The type of favorites to list (album, track, playlist, artist)
            filter (str): Additional filter criteria for the favorites
            offset (int, optional): Number of results to skip for pagination. Defaults to 0.
            limit (int, optional): Maximum number of results to return. Defaults to 50.

        Returns:
            BrowseItemList: A list of the user's favorite items of the specified type
        """
        ...

    async def get_favorite_ids(self) -> FavoriteIds:
        """
        Retrieve all favorite item IDs for the current user.

        Returns:
            FavoriteIds: Object containing collections of favorite IDs grouped by type
        """
        ...

    async def add_to_favorite(self, id: str):
        """
        Add an item to the user's favorites.

        Args:
            id (str): The ID of the item to add to favorites
        """
        ...

    async def remove_from_favorite(self, id: str):
        """
        Remove an item from the user's favorites.

        Args:
            id (str): The ID of the item to remove from favorites
        """
        ...

    async def list_filter_values(
        self,
        catalog_id: EntityId,
        field: str,
        offset: int = 0,
        limit: int = 50,
        q: str = "",
    ) -> FilterValueList:
        """
        List the vocabulary of one VALUES field of one catalog.

        Called when a filter control is opened, not when a catalog is listed —
        a vocabulary may run to hundreds of entries, so it is fetched only when
        something is about to show it.

        Args:
            catalog_id (EntityId): The catalog whose field is being filled. A
                module with one vocabulary may ignore it.
            field (str): The field id, as declared in that catalog's ``filters``.
            offset (int, optional): Number of values to skip. Defaults to 0.
            limit (int, optional): Maximum number of values to return. Defaults to 50.
            q (str, optional): Narrows the vocabulary by label, for type-ahead
                over a large one. Defaults to no narrowing.

        Returns:
            FilterValueList: One page of the field's vocabulary

        Raises:
            UnsupportedFilter: If the catalog declares no such VALUES field.

        Must complete within the server's per-call timeout (see the
        class docstring's latency contract).
        """
        ...

    async def get(self, entity_id: EntityId) -> BrowseItem:
        """
        Get detailed information about a specific entity.

        Args:
            entity_id (EntityId): The ID of the entity to retrieve

        Returns:
            BrowseItem: Detailed information about the requested entity

        Must complete within the server's per-call timeout (see the
        class docstring's latency contract).
        """
        ...

    async def get_all(self, entity_ids: List[EntityId]) -> List[BrowseItem]:
        """Resolve many entities at once (SDK 1.3+).

        Default implementation: concurrent ``get()`` per id. Override when
        the backend supports batch lookup — e.g. one API request resolving
        N ids — to save per-id round-trips (the server resolves Related
        Artists through this).

        Ids that fail to resolve are omitted from the result, so callers
        must match returned items to requests by ``item.id``, never by
        position.

        Args:
            entity_ids (List[EntityId]): The IDs of the entities to retrieve

        Returns:
            List[BrowseItem]: The entities that resolved, in request order

        Must complete within the server's per-call timeout (see the
        class docstring's latency contract).
        """
        results = await asyncio.gather(
            *(self.get(eid) for eid in entity_ids), return_exceptions=True
        )
        return [r for r in results if isinstance(r, BrowseItem)]

    async def playlist_user_list(
        self, offset: int = 0, limit: int = 25
    ) -> BrowseItemList:
        """
        List user-created playlists.

        Args:
            offset (int, optional): Number of results to skip for pagination. Defaults to 0.
            limit (int, optional): Maximum number of results to return. Defaults to 25.

        Returns:
            BrowseItemList: A list of user-created playlists
        """
        ...

    async def playlist_create(self, name: str, description: str) -> Playlist:
        """
        Create a new playlist.

        Args:
            name (str): The name for the new playlist
            description (str): A description for the new playlist

        Returns:
            Playlist: The newly created playlist object
        """
        ...

    async def playlist_update(
        self, id: str, name: Optional[str], description: Optional[str]
    ) -> Playlist:
        """
        Update an existing playlist's metadata.

        Args:
            id (str): The ID of the playlist to update
            name (Optional[str]): New name for the playlist (None to keep unchanged)
            description (Optional[str]): New description for the playlist (None to keep unchanged)

        Returns:
            Playlist: The updated playlist object
        """
        ...

    async def playlist_delete(self, id: str):
        """
        Delete a playlist.

        Args:
            id (str): The ID of the playlist to delete
        """
        ...

    async def playlist_add_tracks(
        self, id: str, track_ids: List[str], allow_duplicates: bool
    ) -> Playlist:
        """
        Add tracks to an existing playlist.

        Args:
            id (str): The ID of the playlist to add tracks to
            track_ids (List[str]): List of track IDs to add to the playlist
            allow_duplicates (bool): Whether to allow duplicate tracks in the playlist

        Returns:
            Playlist: The updated playlist object with new tracks added
        """
        ...

    async def playlist_remove_tracks(
        self, id: str, playlist_track_ids: List[str]
    ) -> Playlist:
        """
        Remove tracks from an existing playlist.

        Args:
            id (str): The ID of the playlist to remove tracks from
            playlist_track_ids (List[str]): List of playlist track IDs to remove
                                          (these are playlist-specific IDs, not track IDs)

        Returns:
            Playlist: The updated playlist object with tracks removed
        """
        ...

    async def get_resource_path(self, id: str) -> str | None:
        """
        Get the file system path for a local resource.

        This method is primarily used by local file input modules to provide
        direct file system access to audio files.

        Args:
            id (str): The ID of the resource to get the path for

        Returns:
            str | None: The file system path to the resource, or None if not available
        """
        ...
