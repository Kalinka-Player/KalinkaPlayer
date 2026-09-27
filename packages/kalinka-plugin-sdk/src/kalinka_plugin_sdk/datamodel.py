from typing import Optional, List
from pydantic import (
    BaseModel,
    NonNegativeInt,
    model_serializer,
    field_validator,
    model_validator,
)
from enum import Enum

from .filters import FilterSpec


class EntityType(str, Enum):
    """
    Enumeration of all entity types supported in the Kalinka system.

    These types define the different kinds of content objects that can be
    referenced and manipulated within the music player ecosystem.
    """

    CATALOG = "catalog"
    ALBUM = "album"
    ARTIST = "artist"
    PLAYLIST = "playlist"
    TRACK = "track"
    LABEL = "label"
    GENRE = "genre"
    USER = "user"


class EntityId(BaseModel):
    """
    Universal identifier for all entities in the Kalinka system.

    EntityId provides a structured way to uniquely identify any content object
    across different input modules and sources. It combines the source module,
    entity type, and local ID into a globally unique identifier.

    The string representation follows the format:
    "kalinka:{source}:{type}:{id}"

    Examples:
        - "kalinka:streamingservice:track:4iV5W9uYEdYUVa79Axb7Rh"
        - "kalinka:localfiles:album:artist_album_2023"
        - "kalinka:musicservice:artist:123456"

    Attributes:
        id (str): The local identifier within the source module
        type (EntityType): The type of entity (track, album, artist, etc.)
        source (str): The name of the input module that provides this entity

    Features:
        - Automatic string serialization/deserialization
        - Hash and equality support for use in sets and dictionaries
        - Flexible construction from strings or component parts
        - Validation of format and structure
    """

    id: str
    type: EntityType
    source: str

    @model_validator(mode="before")
    @classmethod
    def validate_entity_id_model(cls, values):
        """
        Validates and converts string representations to EntityId objects.

        Handles cases where a full entity ID string is provided instead of
        a dictionary with separate fields.
        """
        # Handle the case where we receive a string instead of a dict
        if isinstance(values, str):
            return cls.from_string(values).__dict__
        return values

    @field_validator("id", "type", "source", mode="before")
    @classmethod
    def validate_from_string(cls, v, info):
        """
        Validates individual fields, parsing full entity ID strings when needed.

        This validator allows flexible construction where any field can receive
        a full entity ID string and extract the appropriate component.
        """
        # If we receive a string for any field and it looks like a full entity ID,
        # parse the entire string and return the appropriate field value
        if isinstance(v, str) and v.startswith("kalinka:") and ":" in v:
            # This is a full entity ID string, parse it
            parts = v.split(":")
            if len(parts) == 4 and parts[0] == "kalinka":
                _, source, type_str, id_ = parts
                # Return the value for the specific field being validated
                if info.field_name == "id":
                    return id_
                elif info.field_name == "type":
                    return EntityType(type_str)
                elif info.field_name == "source":
                    return source
        return v

    @property
    def to_string(self) -> str:
        """
        Convert the EntityId to its string representation.

        Returns:
            str: The full entity ID in format "kalinka:{source}:{type}:{id}"
        """
        return f"kalinka:{self.source}:{self.type.value}:{self.id}"

    @classmethod
    def from_string(cls, full_id: str) -> "EntityId":
        """
        Create an EntityId from its string representation.

        Args:
            full_id (str): Full entity ID string in format "kalinka:{source}:{type}:{id}"

        Returns:
            EntityId: A new EntityId object

        Raises:
            ValueError: If the string format is invalid
        """
        # Expected format: kalinka:{source}:{type}:{id}
        parts = full_id.split(":")
        if len(parts) != 4 or parts[0] != "kalinka":
            raise ValueError(f"Invalid full_id format: {full_id}")
        _, source, type_str, id_ = parts
        return cls(id=id_, type=EntityType(type_str), source=source)

    def __hash__(self):
        """Enable use of EntityId in sets and as dictionary keys."""
        return hash(self.to_string)

    def __eq__(self, other):
        """
        Compare EntityId with another EntityId or string.

        Args:
            other: Another EntityId object or string representation

        Returns:
            bool: True if the entities are the same
        """
        if isinstance(other, EntityId):
            return self.to_string == other.to_string
        elif isinstance(other, str):
            return self.to_string == other
        return False

    @model_serializer
    def ser_model(self) -> str:
        """Serialize EntityId as a string for JSON/API output."""
        return self.to_string


class PreviewType(str, Enum):
    """
    The enum defines different types of preview layouts for displaying content in a user interface.
    Each type specifies a unique way to present items, such as images, text, or carousels.
    """

    # Card with image above, title and subtitle below (default card layout)
    IMAGE_TEXT = "image"
    # Card with only text (title centered inside the card)
    TEXT_ONLY = "text"
    # Horizontal carousel of up to 5 items (used for root catalog only)
    CAROUSEL = "carousel"
    # Tile layout: image on the left, title and subtitle on the right
    TILE = "tile"
    # Numbered tile layout: order number, title, and subtitle on the right
    TILE_NUMBERED = "tile_numbered"
    # Self-contained card wrapping a vertical list of items (e.g. the AI
    # suggestions track list): the section renders its own bordered surface
    # with a header. Plain (non-card) sections render header + rows inline.
    CARD = "card"
    # No preview items (section displays only an image or text)
    NONE = "none"


class PreviewContentType(str, Enum):
    """
    What a preview section's items are.

    Consumers use it to size cards, pick placeholders while the items load,
    and to tell which kind a shelf stands for. Leave it unset where a listing
    mixes kinds rather than naming one of them.
    """

    ALBUM = "album"
    ARTIST = "artist"
    PLAYLIST = "playlist"
    TRACK = "track"
    CATALOG = "catalog"


class CardSize(str, Enum):
    SMALL = "small"
    LARGE = "large"


class CatalogRole(str, Enum):
    """
    UI hint describing a catalog's role on discovery/home surfaces.

    Advisory only — frontends may combine the role with other signals or
    fall back to a generic shelf rendering when the role is unset or
    unrecognized. The enum lets plugins express intent ("this is a small
    editorial pick" / "this is a navigation index" / "do not surface on
    home") without the frontend hardcoding catalog IDs.
    """

    # Small curated set with editorial weight; UI may use larger tiles.
    FEATURED = "featured"
    # Generic catalog of discoverable content (albums/playlists/etc.).
    DISCOVERY = "discovery"
    # User's own content on this backend (library shelves).
    LIBRARY = "library"
    # Catalog whose children are themselves catalogs; UI may auto-descend
    # one level and surface grandchildren as shelves.
    INDEX = "index"
    # Too broad or otherwise unsuited to home; reachable via drill-down only.
    HIDE_ON_HOME = "hide_on_home"


class CoverImage(BaseModel):
    """
    Container for cover art images in different sizes.

    Provides URLs for the same image in multiple resolutions to support
    different UI contexts and device capabilities.

    Attributes:
        small (Optional[str]): Small resolution image URL (e.g., for list items)
        thumbnail (Optional[str]): Thumbnail resolution image URL
        large (Optional[str]): High resolution image URL (e.g., for full-screen display)
    """

    small: Optional[str] = ""
    thumbnail: Optional[str] = ""
    large: Optional[str] = ""


class Artist(BaseModel):
    """
    Represents a musical artist or performer.

    Attributes:
        id (EntityId): Unique identifier for the artist
        name (str): Artist's display name
        image (Optional[CoverImage]): Artist's profile/promotional images
        album_count (Optional[int]): Number of albums by this artist
    """

    id: EntityId
    name: str
    image: Optional[CoverImage] = None
    album_count: Optional[int] = None


class Label(BaseModel):
    """
    A music label or record company.

    Attributes:
        id (EntityId): Unique identifier for the label
        name (str): Label's display name
    """

    id: EntityId
    name: str


class Genre(BaseModel):
    """
    A music genre classification.

    Attributes:
        id (EntityId): Unique identifier for the genre
        name (str): Genre name (e.g., "Rock", "Jazz", "Electronic")
    """

    id: EntityId
    name: str


class Album(BaseModel):
    """
    Represents a music album or collection of tracks.

    Attributes:
        id (EntityId): Unique identifier for the album
        title (str): Album title
        duration (Optional[int]): Total album duration in seconds
        track_count (Optional[int]): Number of tracks in the album
        image (Optional[CoverImage]): Album cover art in different sizes
        label (Optional[Label]): Record label that released the album
        genres (List[Genre]): The genres it is filed under, in the order the
            source names them; empty when it names none
        artist (Optional[Artist]): Primary artist/performer
    """

    id: EntityId
    title: str
    duration: Optional[int] = None
    track_count: Optional[int] = None
    image: Optional[CoverImage] = None
    label: Optional[Label] = None
    genres: List[Genre] = []
    artist: Optional[Artist] = None


class Track(BaseModel):
    """
    Represents an individual music track.

    Attributes:
        id (EntityId): Unique identifier for the track
        title (str): Track title
        duration (int): Track duration in seconds
        performer (Optional[Artist]): Track performer (may differ from album artist)
        album (Album): Album containing this track
        replaygain_peak (Optional[float]): ReplayGain peak value for audio normalization
        replaygain_gain (Optional[float]): ReplayGain gain value for audio normalization
        playlist_track_id (Optional[str]): ID specific to playlist membership
        unavailable (bool): True when the track's source could not be retrieved and playback skipped it
    """

    id: EntityId
    title: str
    # Duration in seconds
    duration: int
    performer: Optional[Artist] = None
    album: Album
    replaygain_peak: Optional[float] = None
    replaygain_gain: Optional[float] = None
    playlist_track_id: Optional[str] = None
    # True when the track's source could not be retrieved and playback
    # skipped it. Surfaced to clients so they can flag the track in the queue.
    unavailable: bool = False


class Owner(BaseModel):
    """
    Represents the owner/creator of a playlist.

    Attributes:
        name (str): Owner's display name
        id (EntityId): Unique identifier for the owner
    """

    name: str
    id: EntityId


class Playlist(BaseModel):
    """
    A playlist created by a user or imported from an external source.

    Attributes:
        id (EntityId): Unique identifier for the playlist
        name (str): Playlist name
        owner (Owner): User who created/owns the playlist
        image (Optional[CoverImage]): Playlist cover art
        description (Optional[str]): Playlist description
        track_count (int): Number of tracks in the playlist
        duration (Optional[int]): Total playing time in seconds, where the
            source knows it without loading the tracks
    """

    id: EntityId
    name: str
    owner: Owner
    image: Optional[CoverImage] = None
    description: Optional[str]
    track_count: int
    duration: Optional[int] = None


class Preview(BaseModel):
    """
    Configuration for preview section in the catalog view.

    Defines how content should be displayed in preview sections of the UI,
    including layout, sizing, and presentation hints.

    Attributes:
        items_count (Optional[int]): Maximum number of items to show in preview
        type (PreviewType): Layout type for the preview section
        content_type (Optional[PreviewContentType]): Hint about content type for UI styling
        icon (Optional[str]): Semantic icon id for the section header (e.g.
            "best_match", "ai_suggestions", "album", "artist"). The UI maps
            this to a concrete icon; presentation is owned by the backend.
        rows_count (Optional[int]): Number of rows to display
        aspect_ratio (Optional[float]): Preferred aspect ratio for items
        card_size (Optional[CardSize]): Size preference for cards/items
    """

    # Maximum number of items to be shown in the preview section.
    # This is a UI configuration value, not the actual count of items available.
    items_count: Optional[int] = None
    type: PreviewType
    content_type: Optional[PreviewContentType] = None
    icon: Optional[str] = None
    rows_count: Optional[int] = None
    aspect_ratio: Optional[float] = None
    card_size: Optional[CardSize] = None


class Catalog(BaseModel):
    """
    Representation of a music catalog or browsable section.

    Catalogs are top-level containers that organize content into browsable
    sections like "New Releases", "Genres", "My Library", etc.

    Attributes:
        id (EntityId): Unique identifier for the catalog
        title (str): Display title for the catalog section
        image (Optional[CoverImage]): Representative image for the catalog
        filters (List[FilterSpec]): The fields this catalog can be filtered by.
            Empty means it offers no filtering. A field absent here is one a
            caller must not send: the module answers such a query with
            :class:`~kalinka_plugin_sdk.filters.UnsupportedFilter` rather than
            listing unfiltered.
        description (Optional[str]): Description of the catalog content
        preview_config (Optional[Preview]): Configuration for preview display
        role (Optional[CatalogRole]): Hint for how home/discovery surfaces
            should treat this catalog. Advisory; UI may ignore.
        sources (List[str]): Input-module source name(s) whose entities this
            catalog represents. A single name for a per-source section, several
            for a server-assembled cross-source one (e.g. Related Albums rolled
            up from multiple sources). Empty when not applicable. This is how a
            consumer attributes a catalog to its source(s) when the catalog's
            own ``id.source`` cannot (server-assembled sections use
            ``source="server"`` and so can't encode the origin in the id).
    """

    id: EntityId
    title: str
    image: Optional[CoverImage] = None
    filters: List[FilterSpec] = []
    description: Optional[str] = ""
    preview_config: Optional[Preview] = None
    role: Optional[CatalogRole] = None
    sources: List[str] = []


class MatchTier(str, Enum):
    """
    How closely a search hit's name answers the query, best first.

    Assigned by the server's name ranking, so a consumer holding hits from
    several sources can merge them by tier and then by
    :attr:`NameMatch.score` without ranking anything itself.

    Values:
        EXACT: The name, once case, diacritics and punctuation are set aside.
        EQUIVALENT: The name once a leading article and an edition suffix
            ("Deluxe Edition", "2009 Remaster") are set aside too.
        CLOSE: A near miss on the whole name — a typo away.
        PARTIAL: The query is a prefix of the name or contained in it, or
            the name is contained in the query.
        CONTEXTUAL: Matched not on the name but on its context — an album's
            or track's artist, or a track's album.
        WEAK: Returned by the source, but its name explains little of the
            query.
    """

    EXACT = "exact"
    EQUIVALENT = "equivalent"
    CLOSE = "close"
    PARTIAL = "partial"
    CONTEXTUAL = "contextual"
    WEAK = "weak"


class NameMatch(BaseModel):
    """
    Why a search hit stands where it does in its listing.

    Attributes:
        tier (MatchTier): The band the hit ranks in; every hit of a better
            tier outranks it.
        score (float): Whole-string similarity to the query, 0..100, ordering
            hits within a tier. Not comparable across tiers.
    """

    tier: MatchTier
    score: float


class BrowseItem(BaseModel):
    """
    A universal container for any item that can be displayed in the Kalinka UI.

    BrowseItem is the primary data structure used throughout the Kalinka system
    for representing content that users can browse, search, and interact with.
    It provides a flexible container that can represent any type of musical content
    while maintaining a consistent interface for the UI.

    Key Concepts:
    - **Polymorphic Design**: A single BrowseItem can represent different types
      of content (tracks, albums, artists, playlists, catalogs) by populating
      the appropriate nested object fields.

    - **Browsable Content**: Items with `can_browse=True` can be "opened" to
      reveal their contents (e.g., browsing into an album shows its tracks).

    - **Addable Content**: Items with `can_add=True` can be added to playlists
      or queues directly.

    - **Hierarchical Structure**: Items can contain sections with related content,
      enabling rich browsing experiences.

    Usage Patterns:
    - For a track: Populate `track` field, set `can_add=True`
    - For an album: Populate `album` field, set `can_browse=True` to show tracks
    - For a catalog: Populate `catalog` field, set `can_browse=True`
    - For mixed content: Use `name`/`subname` with appropriate nested objects

    Attributes:
        id (EntityId): Unique identifier for this item
        name (str): Primary display name (title, artist name, etc.)
        url (Optional[str]): Direct URL for web-based content
        can_browse (bool): Whether this item can be browsed into (has children)
        can_add (bool): Whether this item can be added to playlists/queues
        can_edit (bool): Whether the server accepts writes that change what
            this item holds — a hint that lets a client offer editing without
            knowing which source owns it. Advisory: the write API is the
            authority and refuses an id it does not own, whatever a client
            believed.
        subname (Optional[str]): Secondary display text (subtitle, artist, etc.)
        album (Optional[Album]): Album data if this represents an album
        artist (Optional[Artist]): Artist data if this represents an artist
        playlist (Optional[Playlist]): Playlist data if this represents a playlist
        catalog (Optional[Catalog]): Catalog data if this represents a catalog section
        track (Optional[Track]): Track data if this represents a track
        timestamp (NonNegativeInt): Used for sorting/merging items by time
        match (Optional[NameMatch]): How the item answers the query it was
            searched for, set by the server's name ranking and absent
            everywhere else.
        sections (Optional[List[BrowseItem]]): Related content shown alongside
            this item — "Similar albums", or the shelves a catalog is made of.
            A section carrying a ``catalog`` is a pointer: browse its id for
            the items, which is what lets a consumer show one shelf per kind
            and fill each lazily. A section carrying anything else is an item
            to render inline. Browsing the owning catalog itself answers
            items, never these sections.

    Examples:
        # Track item
        BrowseItem(
            id=track_id,
            name="Song Title",
            subname="Artist Name",
            can_add=True,
            track=Track(...)
        )

        # Album item
        BrowseItem(
            id=album_id,
            name="Album Title",
            subname="Artist Name",
            can_browse=True,
            album=Album(...)
        )

        # Catalog section with related content
        BrowseItem(
            id=catalog_id,
            name="New Releases",
            can_browse=True,
            catalog=Catalog(...),
            sections=[
                BrowseItem(name="Similar Artists", ...),
                BrowseItem(name="Recommended Albums", ...)
            ]
        )
    """

    id: EntityId
    name: str
    url: Optional[str] = None
    can_browse: bool = False
    can_add: bool = False
    can_edit: bool = False
    subname: Optional[str] = None
    album: Optional[Album] = None
    artist: Optional[Artist] = None
    playlist: Optional[Playlist] = None
    catalog: Optional[Catalog] = None
    track: Optional[Track] = None

    # Used for merging multiple items into a single view
    timestamp: NonNegativeInt = 0

    match: Optional[NameMatch] = None
    sections: Optional[List["BrowseItem"]] = None


class BrowseItemList(BaseModel):
    """
    Paginated list of browse items with metadata.

    Standard container for returning lists of content with pagination support.
    Used throughout the API for browse, search, and listing operations.

    Attributes:
        offset (int): Starting position of this page in the full result set
        limit (int): Maximum number of items requested for this page
        total (int): Total number of items available across all pages
        items (List[BrowseItem]): The actual items for this page
    """

    offset: int
    limit: int
    total: int
    items: List[BrowseItem]


def EmptyList(offset, limit) -> BrowseItemList:
    """
    Create an empty BrowseItemList with the specified pagination parameters.

    Args:
        offset (int): The offset for the empty list
        limit (int): The limit for the empty list

    Returns:
        BrowseItemList: An empty list with total=0 and no items
    """
    return BrowseItemList(offset=offset, limit=limit, total=0, items=[])


class FavoriteIds(BaseModel):
    """
    Collection of user's favorite item IDs organized by content type.

    Provides efficient access to user's favorites for quick lookup
    without requiring full metadata retrieval.

    Attributes:
        albums (List[EntityId]): List of favorite album IDs
        artists (List[EntityId]): List of favorite artist IDs
        tracks (List[EntityId]): List of favorite track IDs
        playlists (List[EntityId]): List of favorite playlist IDs
    """

    albums: List[EntityId] = []
    artists: List[EntityId] = []
    tracks: List[EntityId] = []
    playlists: List[EntityId] = []


class VolumeBackend(str, Enum):
    """
    Where a device applies its volume, and so whether it touches the samples.

    Only SOFTWARE does, and only below unity. UNKNOWN is an unanswered
    question, not a denial: deciding the samples are untouched takes NONE,
    HARDWARE, or SOFTWARE at its maximum. Testing ``!= SOFTWARE`` instead reads
    a device that cannot say as one that has said no.
    """

    UNKNOWN = "unknown"
    NONE = "none"
    HARDWARE = "hardware"
    SOFTWARE = "software"


class DeviceVolume(BaseModel):
    """
    Volume control information for audio devices.

    Attributes:
        max_volume (int): Maximum volume level supported by the device
        current_volume (int): Current volume level
        volume_gain (int): Additional gain/boost applied
        supported (bool): Whether volume control is supported on this device
    """

    max_volume: int = 0
    current_volume: int = 0
    volume_gain: int = 0
    supported: bool = True
    backend: VolumeBackend = VolumeBackend.UNKNOWN


class DeviceAccess(str, Enum):
    """
    How a renderer holds its output device.

    SHARED is not proof that anything alters the samples, only that nothing
    rules it out — the most a renderer can say about a path it shares.
    """

    UNKNOWN = "unknown"
    EXCLUSIVE = "exclusive"
    SHARED = "shared"


class OutputInfo(BaseModel):
    """
    What the output device runs at, against the stream that was decoded for it.

    Attributes:
        sample_rate (int): Device sample rate in Hz
        bits_per_sample (int): Significant bits the device carries, not the
            width of the container they are shifted into
        channels (int): Device channel count
        access (DeviceAccess): How the device is held
        lossless_path (bool): Whether the decoded samples reach the device
            unaltered. Says nothing about the volume: that is reported with the
            device it is applied by, because only there is it current.
    """

    sample_rate: int = 0
    bits_per_sample: int = 0
    channels: int = 0
    access: DeviceAccess = DeviceAccess.UNKNOWN
    lossless_path: bool = False


class AudioInfo(BaseModel):
    """
    Technical information about the currently playing audio stream.

    Attributes:
        sample_rate (int): Audio sample rate in Hz (e.g., 44100, 48000)
        bits_per_sample (int): Bit depth (e.g., 16, 24)
        channels (int): Number of audio channels (1=mono, 2=stereo)
        duration_ms (int): Track duration in milliseconds
        output (Optional[OutputInfo]): The device the stream is played out of,
            absent when the renderer has none open or cannot describe it
    """

    sample_rate: int
    bits_per_sample: int
    channels: int
    duration_ms: int
    output: Optional[OutputInfo] = None


class PlaybackMode(BaseModel):
    """
    Playback behavior configuration.

    Attributes:
        shuffle (bool): Whether tracks are played in random order
        repeat_single (bool): Whether to repeat the current track
        repeat_all (bool): Whether to repeat the entire playlist/queue
    """

    shuffle: bool
    repeat_single: bool
    repeat_all: bool


class PlayerStateEnum(str, Enum):
    """
    Enumeration of possible player states.

    Defines the various states the audio player can be in, such as playing,
    paused, stopped, buffering, or encountering an error.
    """

    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    BUFFERING = "BUFFERING"
    ERROR = "ERROR"


class PlaybackState(BaseModel):
    """
    Complete state information for the audio player.

    Represents the current status of the player including what's playing,
    playback position, and technical details about the audio stream.

    Attributes:
        state (Optional[str]): Current player state (playing, paused, stopped, etc.)
        current_track (Optional[Track]): Currently loaded/playing track
        index (Optional[int]): Position in the current playlist/queue
        position (Optional[int]): Playback position in seconds
        message (Optional[str]): Status message or error information
        audio_info (Optional[AudioInfo]): Technical details about the audio stream
        mime_type (Optional[str]): MIME type of the audio stream
        stream_url (Optional[str]): Where the renderer is fetching the current
            stream from, as it was handed over. Absent while nothing is loaded.
        timestamp_ns (NonNegativeInt): Timestamp when this state was captured
    """

    state: Optional[PlayerStateEnum] = None
    current_track: Optional[Track] = None
    index: Optional[int] = None
    position: Optional[int] = None
    message: Optional[str] = None
    audio_info: Optional[AudioInfo] = None
    mime_type: Optional[str] = None
    stream_url: Optional[str] = None
    timestamp_ns: NonNegativeInt = 0


class PlaybackControlMode(str, Enum):
    QUEUE = "queue"
    EXCLUSIVE = "exclusive"


class PlaybackControl(BaseModel):
    """Who drives the output.

    Under QUEUE, Kalinka's play queue plays. Under EXCLUSIVE, an input plugin
    holds the renderer to play outside the queue, as a Connect receiver does:
    the queue keeps its contents but plays nothing, and the playback state
    clients are shown is the plugin's.

    Attributes:
        mode (PlaybackControlMode): QUEUE or EXCLUSIVE
        plugin_id (Optional[str]): The plugin holding the output; EXCLUSIVE only
        title (Optional[str]): What clients call its playback ("Qobuz
            Connect"); EXCLUSIVE only
    """

    mode: PlaybackControlMode = PlaybackControlMode.QUEUE
    plugin_id: Optional[str] = None
    title: Optional[str] = None

    @model_validator(mode="after")
    def _names_the_plugin_only_when_exclusive(self) -> "PlaybackControl":
        if self.mode is PlaybackControlMode.EXCLUSIVE:
            if not self.plugin_id or not self.title:
                raise ValueError("exclusive control names the plugin and its title")
        elif self.plugin_id is not None or self.title is not None:
            raise ValueError("the queue's control names no plugin")
        return self

    @classmethod
    def queue(cls) -> "PlaybackControl":
        return cls()

    @classmethod
    def exclusive(cls, plugin_id: str, title: str) -> "PlaybackControl":
        return cls(mode=PlaybackControlMode.EXCLUSIVE, plugin_id=plugin_id, title=title)

    @property
    def is_exclusive(self) -> bool:
        return self.mode is PlaybackControlMode.EXCLUSIVE


class DeviceState(BaseModel):
    """
    State information for an audio output device.

    Attributes:
        name (str): Human-readable device name
        volume (DeviceVolume): Volume control information for the device
        capabilities (List[str]): List of supported device capabilities
    """

    name: str
    volume: DeviceVolume
    power_on: bool
    capabilities: List[str] = []


class TrackList(BaseModel):
    """
    Paginated list of tracks.

    Used for playlist contents, album tracks, and other track collections.

    Attributes:
        offset (int): Starting position in the full track list
        limit (int): Maximum number of tracks in this response
        total (int): Total number of tracks available
        items (List[Track]): The track items for this page
    """

    offset: int
    limit: int
    total: int
    items: List[Track]


class LastUpdate(BaseModel):
    """
    Timestamp tracking for user's favorite content changes.

    Used to efficiently synchronize favorites between client and server
    by tracking when each category was last modified.

    Attributes:
        favorite_tracks_ts (int): Unix timestamp of last favorite tracks update
        favorite_albums_ts (int): Unix timestamp of last favorite albums update
        favorite_artists_ts (int): Unix timestamp of last favorite artists update
        favorite_playlists_ts (int): Unix timestamp of last favorite playlists update
    """

    favorite_tracks_ts: int = 0
    favorite_albums_ts: int = 0
    favorite_artists_ts: int = 0
    favorite_playlists_ts: int = 0
