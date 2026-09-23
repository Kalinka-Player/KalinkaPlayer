import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Tuple

import httpx
from pydantic import PositiveInt

from kalinka_plugin_sdk.datamodel import (
    Album,
    Artist,
    BrowseItem,
    BrowseItemList,
    CardSize,
    Catalog,
    CatalogRole,
    CoverImage,
    EmptyList,
    EntityId,
    EntityType,
    FavoriteIds,
    Genre,
    Owner,
    Playlist,
    Preview,
    PreviewContentType,
    PreviewType,
    Track,
)
from kalinka_plugin_sdk.filters import (
    or_unfiltered,
    TEXT_FIELD,
    TYPE_FIELD,
    FilterKind,
    FilterOp,
    FilterQuery,
    FilterSpec,
    FilterValue,
    FilterValueList,
    UnsupportedFilter,
)
from kalinka_plugin_sdk.inputmodule import (
    DirectUrl,
    InputModule,
    SearchType,
    TrackInfo,
    TrackSource,
)

from .config_model import JamendoConfig
from .mood_search import JamendoMoodIndex

logger = logging.getLogger(__name__.split(".")[-1])

SOURCE = "jamendo"
BASE_URL = "https://api.jamendo.com/v3.0/"


# Jamendo caps page size at 200.
MAX_LIMIT = 200

# Jamendo's unbounded ``order=releasedate_desc`` sort scans the whole album
# catalog and takes ~5s server-side — past this client's per-attempt timeout,
# so the New Releases shelf would time out and show nothing. Bounding the query
# to a recent date window makes Jamendo sort a tiny slice instead (~0.15s). The
# window is wide enough that offset pagination never runs dry: a 6-month window
# holds well over MAX_LIMIT albums.
NEW_RELEASES_WINDOW_DAYS = 180

# Jamendo publishes no tag list, so this is the vocabulary we offer: the genres
# its own tracks actually carry, by frequency over a sample of the popular and
# recent catalog. Slugs are what the API matches; the labels are ours.
GENRES = [
    ("pop", "Pop"),
    ("electronic", "Electronic"),
    ("rock", "Rock"),
    ("dance", "Dance"),
    ("indie", "Indie"),
    ("ambient", "Ambient"),
    ("folk", "Folk"),
    ("synthpop", "Synth-pop"),
    ("hiphop", "Hip-hop"),
    ("electronica", "Electronica"),
    ("filmscore", "Film score"),
    ("singersongwriter", "Singer-songwriter"),
    ("rnb", "R&B"),
    ("electropop", "Electropop"),
    ("poprock", "Pop rock"),
    ("synthwave", "Synthwave"),
    ("soul", "Soul"),
    ("funk", "Funk"),
    ("chillout", "Chillout"),
    ("newage", "New age"),
    ("lofi", "Lo-fi"),
    ("rap", "Rap"),
    ("jazz", "Jazz"),
    ("classical", "Classical"),
    ("world", "World"),
    ("country", "Country"),
    ("indierock", "Indie rock"),
    ("house", "House"),
    ("drumnbass", "Drum & bass"),
    ("rocknroll", "Rock & roll"),
    ("dreampop", "Dream pop"),
    ("hardrock", "Hard rock"),
    ("experimental", "Experimental"),
    ("indiepop", "Indie pop"),
    ("easylistening", "Easy listening"),
    ("punk", "Punk"),
    ("disco", "Disco"),
    ("metal", "Metal"),
    ("reggae", "Reggae"),
    ("blues", "Blues"),
]

_GENRE_LABELS = dict(GENRES)


def _genres_of(track: dict) -> List[Genre]:
    """The genres a track's musicinfo names, labelled from the vocabulary the
    plugin offers; a slug outside it is shown as it came."""
    tags = (track.get("musicinfo") or {}).get("tags") or {}
    return [
        Genre(id=genre_id(slug), name=_GENRE_LABELS.get(slug, slug.capitalize()))
        for slug in tags.get("genres") or []
        if slug
    ]


# ``tags`` is honoured by /tracks/ alone — /albums/, /artists/ and /playlists/
# drop it and say so in headers.warnings — and multiple tags intersect rather
# than union, which is what ops says.
GENRE_FILTER = FilterSpec(
    id="genre",
    kind=FilterKind.VALUES,
    label="Genre",
    ops=[FilterOp.ALL],
)

TYPE_FILTER = FilterSpec(
    id=TYPE_FIELD,
    kind=FilterKind.VALUES,
    label="Type",
    ops=[FilterOp.ANY],
)


def _text_filter(what: str) -> FilterSpec:
    """``namesearch`` matches the entity's own name and nothing else, so the
    label says which name it is."""
    return FilterSpec(id=TEXT_FIELD, kind=FilterKind.TEXT, label=f"Search {what}")


@dataclass(frozen=True)
class _PopularSection:
    """One kind's shelf within Popular.

    Popular and its sections are two addresses for one listing: browsing
    Popular under ``type: [kind]`` and browsing the section answer the same
    rows. The section exists so a consumer can preview each kind without
    knowing how to write that constraint.

    Attributes:
        kind: The ``type`` value that narrows Popular to this section, and the
            :class:`SearchType` the rows are.
        endpoint: The catalog slug the section is browsable under.
        path: Jamendo API path the rows come from.
        order: Jamendo sort this kind is popular by.
    """

    kind: str
    endpoint: str
    path: str
    order: str
    title: str
    content_type: PreviewContentType
    filters: List[FilterSpec]


POPULAR_ENDPOINT = "popular"

POPULAR_SECTIONS = (
    _PopularSection(
        kind=SearchType.track.value,
        endpoint="popular-tracks",
        path="tracks",
        order="popularity_month",
        title="Tracks",
        content_type=PreviewContentType.TRACK,
        filters=[_text_filter("track names"), GENRE_FILTER],
    ),
    _PopularSection(
        kind=SearchType.album.value,
        endpoint="popular-albums",
        path="albums",
        order="popularity_month",
        title="Albums",
        content_type=PreviewContentType.ALBUM,
        filters=[_text_filter("album names")],
    ),
    _PopularSection(
        kind=SearchType.artist.value,
        endpoint="popular-artists",
        path="artists",
        # Artists have no monthly ranking on this API.
        order="popularity_total",
        title="Artists",
        content_type=PreviewContentType.ARTIST,
        filters=[_text_filter("artist names")],
    ),
)

SECTION_BY_KIND = {section.kind: section for section in POPULAR_SECTIONS}
SECTION_BY_ENDPOINT = {section.endpoint: section for section in POPULAR_SECTIONS}

POPULAR_FILTERS = [
    _text_filter("track, album and artist names"),
    TYPE_FILTER,
    GENRE_FILTER,
]

SHELF_FILTERS = {
    POPULAR_ENDPOINT: POPULAR_FILTERS,
    **{section.endpoint: section.filters for section in POPULAR_SECTIONS},
    "new-releases": [_text_filter("album names")],
    "featured-playlists": [_text_filter("playlist names")],
}


def _narrowing_params(filter: FilterQuery, declared: List[FilterSpec]) -> dict:
    """The filter as Jamendo query parameters, refusing what this listing never
    offered.

    ``type`` carries no parameter of its own — it chooses the listing rather
    than narrowing one, and :func:`_popular_sections` is what reads it.
    """
    declared_ids = {spec.id for spec in declared}
    filter.reject_undeclared(declared_ids)

    params: dict = {}
    text = filter.text() if TEXT_FIELD in declared_ids else ""
    if text:
        params["namesearch"] = text

    genres = (
        filter.values(GENRE_FILTER.id) if GENRE_FILTER.id in declared_ids else None
    )
    if genres:
        if genres.any or genres.none:
            raise UnsupportedFilter(
                GENRE_FILTER.id, "this source can only require every genre at once"
            )
        params["tags"] = " ".join(genres.all)
    return params


def _popular_sections(filter: FilterQuery) -> Tuple[_PopularSection, ...]:
    """The kinds a Popular listing holds, given its ``type`` constraint.

    An unknown value is refused rather than matched by nothing: ``type`` has a
    closed vocabulary, so a value outside it is a caller's mistake, and a
    listing that silently held nothing would hide it.
    """
    selector = filter.values(TYPE_FIELD)
    if selector is None:
        return POPULAR_SECTIONS
    if selector.all or selector.none:
        raise UnsupportedFilter(TYPE_FIELD, "only `any` is supported")
    unknown = [value for value in selector.any if value not in SECTION_BY_KIND]
    if unknown:
        raise UnsupportedFilter(
            TYPE_FIELD, f"unknown value(s): {', '.join(unknown)}"
        )
    # Deduplicated: a kind named twice would otherwise interleave with itself.
    return tuple(SECTION_BY_KIND[kind] for kind in dict.fromkeys(selector.any))


FORMAT_CODE = {
    "MP3 (VBR ~V0)": "mp32",
    "MP3 (96 kbps)": "mp31",
    "OGG Vorbis": "ogg",
    "FLAC": "flac",
}
FORMAT_MIME = {
    "mp31": "audio/mpeg",
    "mp32": "audio/mpeg",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
}


def artist_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.ARTIST, source=SOURCE)


def album_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.ALBUM, source=SOURCE)


def track_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.TRACK, source=SOURCE)


def genre_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.GENRE, source=SOURCE)


def playlist_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.PLAYLIST, source=SOURCE)


def user_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.USER, source=SOURCE)


def catalog_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.CATALOG, source=SOURCE)


# Retried: failures that are discovered in milliseconds and are genuinely
# transient. Timeouts are deliberately NOT here — a timeout has already
# consumed the full per-attempt budget, and an upstream that just hung is
# very likely to hang again, so retrying turns one slow request into
# several (3s became 7.5s worst-case). Timeouts propagate immediately.
_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,          # refused / unreachable / DNS
    httpx.ReadError,             # connection reset mid-response
    httpx.RemoteProtocolError,   # server dropped a keep-alive connection
    httpx.ProxyError,
)

# Pause before retrying a success-with-0-results response (see request()).
# Short: the flaky empties return in ~0.15s, so the total detour is ~0.5s.
_EMPTY_RETRY_DELAY_S = 0.2


class RetryTransport(httpx.AsyncHTTPTransport):
    def __init__(self, read_retries=2, **kwargs):
        super().__init__(**kwargs)
        # read_retries is the total number of attempts (1 initial + N-1 retries).
        # Kept low so a request fails fast when the server has no internet —
        # otherwise the playqueue's resolution slot stays occupied far too long.
        self.read_retries = read_retries
        self.backoff_factor = 0.5

    async def handle_async_request(self, request) -> httpx.Response:
        attempt = 1
        while True:
            try:
                response = await super().handle_async_request(request)
            except _RETRYABLE_EXCEPTIONS as exc:
                if attempt >= self.read_retries:
                    raise
                logger.warning("Retry %d due to %r", attempt, exc)
                await asyncio.sleep(self.backoff_factor * attempt)
                attempt += 1
                continue
            if 500 <= response.status_code < 600 and attempt < self.read_retries:
                # Release the connection before retrying; abandoning the
                # response leaks the connection back-pressure under repeated
                # 5xx and can eventually stall further requests.
                await response.aclose()
                logger.warning(
                    "Retry %d due to status code=%d", attempt, response.status_code
                )
                await asyncio.sleep(self.backoff_factor * attempt)
                attempt += 1
                continue
            return response


class JamendoClient:
    """Thin async wrapper over the Jamendo v3.0 REST API.

    Only a ``client_id`` is needed for search/browse/playback. Favourites and
    playlist mutation would require an OAuth2 user flow and are out of scope.
    """

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.base = BASE_URL
        self.session = httpx.AsyncClient(
            # 2 attempts (1 initial + 1 retry), 3s per attempt. RetryTransport
            # already retries connect failures, so httpx-level retries=0 avoids
            # multiplying the worst-case wait.
            transport=RetryTransport(read_retries=2, retries=0),
            timeout=3,
        )
        self.session.headers.update(
            {
                "User-Agent": "kalinka-plugin-jamendo",
            }
        )

    async def aclose(self) -> None:
        await self.session.aclose()

    async def request(self, path: str, params: dict) -> list:
        """GET an endpoint and return its ``results`` array.

        Jamendo wraps every response in ``{"headers": {...}, "results": [...]}``.
        A failed call still returns HTTP 200 with ``headers.status == "failed"``;
        we log and return an empty list so the UI degrades to "no results"
        rather than throwing.

        A *successful* response with an empty ``results`` array gets one
        retry: Jamendo's API intermittently serves ``status: success`` with
        zero results for queries that plainly have them (measured ~50% of
        requests during a 2026-07 episode; the empties return in ~0.15s vs
        ~0.45s for real payloads — a bad cache node, not a timeout). A
        genuinely empty result just repeats one fast round-trip.
        """
        results = await self._request_once(path, params)
        if results is None or results:
            return results or []

        await asyncio.sleep(_EMPTY_RETRY_DELAY_S)
        retried = await self._request_once(path, params)
        if retried:
            logger.info(
                "Jamendo %s: empty success healed by retry (params=%s)",
                path, params,
            )
            return retried
        if retried is not None:
            # Twice empty with success status: either genuinely no data, or
            # the upstream flake outlasted the retry. Params tell which.
            logger.info(
                "Jamendo %s: empty success twice (params=%s)", path, params
            )
        return []

    async def _request_once(self, path: str, params: dict) -> Optional[list]:
        """One GET attempt. Error paths log and return None (never retried
        here — the transport already retries what is safe to retry); a
        successful call returns its ``results`` list, possibly empty."""
        merged = {"client_id": self.client_id, "format": "json", **params}
        started = time.monotonic()
        response = await self.session.get(self.base + path, params=merged)
        elapsed_ms = (time.monotonic() - started) * 1000

        if not response.is_success:
            logger.warning(
                "Jamendo %s failed: HTTP %s (%.0fms)",
                path, response.status_code, elapsed_ms,
            )
            return None

        try:
            rjson = response.json()
        except ValueError as exc:
            # Truncated body, HTML error page, etc. Degrade to "no results"
            # rather than breaking browse/search.
            logger.warning("Jamendo %s returned non-JSON body: %s", path, exc)
            return None
        headers = rjson.get("headers", {})
        if headers.get("status") != "success":
            logger.warning(
                "Jamendo %s error: %s",
                path,
                headers.get("error_message") or headers,  # log-safe: Jamendo's JSON status block, not HTTP headers
            )
            return None

        # Empty-success is not logged here — request() logs the retried
        # outcome once, so a legitimately empty search doesn't warn twice.
        return rjson.get("results", [])

    async def resolve_audio_url(self, track_id: str, audioformat: str) -> str:
        """Resolve a track's playable audio URL via the /tracks/file/ endpoint.

        This is the documented audio-delivery endpoint: it 302-redirects to the
        actual storage URL (tokenised, range-capable). Crucially it resolves a
        track by id alone and works for tracks the /tracks/ metadata index
        doesn't return (old or freshly published). We read the redirect target
        rather than following it, since the native player streams the storage
        URL directly but does not follow redirects itself.

        ``action=stream`` is what makes the target playable in a browser: the
        download form it redirects to otherwise serves the audio as
        ``text/html``, which an HTML ``<audio>`` element refuses.
        """
        params = {
            "client_id": self.client_id,
            "id": track_id,
            "audioformat": audioformat,
            "action": "stream",
        }
        try:
            response = await self.session.get(
                self.base + "tracks/file/", params=params
            )
        except httpx.HTTPError as exc:
            logger.warning("Jamendo tracks/file failed for %s: %s", track_id, exc)
            return ""

        location = response.headers.get("location")
        if location:
            return location
        # No redirect: either the body already is the audio (use the request
        # URL) or it's an error page we can't use.
        if response.is_success:
            return str(response.request.url)
        logger.warning(
            "Jamendo tracks/file for %s returned HTTP %s with no redirect",
            track_id,
            response.status_code,
        )
        return ""


async def get_client(config: JamendoConfig) -> JamendoClient:
    if not config.client_id:
        logger.warning(
            "Jamendo client_id is not configured — requests will return empty."
        )
    return JamendoClient(config.client_id)


def _resize(url: Optional[str], width: int) -> Optional[str]:
    """Return ``url`` requesting a specific image width.

    Jamendo image URLs carry a ``width=`` query param we can rewrite to get
    different sizes from the single URL the API returns.
    """
    if not url:
        return None
    if "width=" in url:
        return re.sub(r"width=\d+", f"width={width}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}width={width}"


def _cover(url: Optional[str]) -> Optional[CoverImage]:
    if not url:
        return None
    return CoverImage(
        thumbnail=_resize(url, 100),
        small=_resize(url, 200),
        large=_resize(url, 600),
    )


def _estimated_total(offset: int, limit: int, count: int) -> int:
    """Estimate a result total.

    Jamendo never reports a grand total — only the size of the current page.
    Report ``offset + count``, and when the page came back full assume there's
    at least one more page so clients keep paginating.
    """
    total = offset + count
    if limit and count >= limit:
        total += limit
    return total


def _album_tracks_section(aid: str) -> BrowseItem:
    return BrowseItem(
        id=album_id(aid),
        name="Tracks",
        can_browse=True,
        can_add=True,
        catalog=Catalog(
            id=album_id(aid),
            title="Tracks",
            preview_config=Preview(
                type=PreviewType.TILE_NUMBERED,
                content_type=PreviewContentType.TRACK,
                items_count=10,
                rows_count=1,
                aspect_ratio=1.0,
                card_size=CardSize.SMALL,
            ),
        ),
    )


def _artist_albums_section(aid: str) -> BrowseItem:
    return BrowseItem(
        id=artist_id(aid),
        name="Albums",
        can_browse=True,
        can_add=False,
        catalog=Catalog(
            id=artist_id(aid),
            title="Albums",
            preview_config=Preview(
                type=PreviewType.TILE,
                content_type=PreviewContentType.ALBUM,
                items_count=10,
                rows_count=1,
                aspect_ratio=1.0,
                card_size=CardSize.SMALL,
            ),
        ),
    )


def _playlist_tracks_section(pid: str) -> BrowseItem:
    return BrowseItem(
        id=playlist_id(pid),
        name="Tracks",
        can_browse=True,
        can_add=True,
        catalog=Catalog(
            id=playlist_id(pid),
            title="Tracks",
            preview_config=Preview(
                type=PreviewType.TILE,
                content_type=PreviewContentType.TRACK,
                items_count=15,
                rows_count=1,
                aspect_ratio=1.0,
                card_size=CardSize.SMALL,
            ),
        ),
    )


def _kind_values(offset: int, limit: int, q: str) -> FilterValueList:
    """The kinds Popular holds, in the order its sections appear."""
    wanted = q.strip().casefold()
    values = [
        FilterValue(id=section.kind, name=section.title)
        for section in POPULAR_SECTIONS
        if wanted in section.title.casefold()
    ]
    return FilterValueList(
        offset=offset,
        limit=limit,
        total=len(values),
        items=values[offset : offset + limit],
    )


def _genre_values(offset: int, limit: int, q: str) -> FilterValueList:
    """The genre vocabulary, narrowed by label or slug. Jamendo cannot say how
    many items a tag covers, so no counts."""
    needle = q.casefold()
    matching = [
        (slug, label)
        for slug, label in GENRES
        if not needle or needle in label.casefold() or needle in slug
    ]
    return FilterValueList(
        offset=offset,
        limit=limit,
        total=len(matching),
        items=[
            FilterValue(id=slug, name=label)
            for slug, label in matching[offset : offset + limit]
        ],
    )


def _catalog_card(
    endpoint: str,
    title: str,
    *,
    preview: Preview,
    description: str = "",
    role: CatalogRole = CatalogRole.DISCOVERY,
    sections: Optional[List[BrowseItem]] = None,
) -> BrowseItem:
    """A catalog as the card that points at it, filtered by whatever that
    endpoint declared."""
    id = catalog_id(endpoint)
    return BrowseItem(
        id=id,
        name=title,
        subname=description or None,
        can_browse=True,
        can_add=False,
        catalog=Catalog(
            id=id,
            title=title,
            description=description,
            filters=SHELF_FILTERS.get(endpoint, []),
            preview_config=preview,
            role=role,
        ),
        sections=sections,
    )


def _popular_section_card(section: _PopularSection) -> BrowseItem:
    return _catalog_card(
        section.endpoint,
        section.title,
        preview=Preview(
            type=PreviewType.IMAGE_TEXT,
            content_type=section.content_type,
            icon=section.kind,
            # Few enough that the next shelf stays on screen.
            items_count=3,
            rows_count=1,
            card_size=CardSize.SMALL,
        ),
    )


class JamendoInputModule(InputModule):
    def __init__(
        self,
        config: JamendoConfig,
        client: JamendoClient,
        mood_index: Optional[JamendoMoodIndex] = None,
    ):
        self.client = client
        # None disables ai_search, which then returns nothing.
        self._mood_index = mood_index
        # config.audio_format is the enum *value* (use_enum_values=True).
        self.audio_format = FORMAT_CODE.get(config.audio_format, "mp32")
        self.audio_mime = FORMAT_MIME[self.audio_format]
        # The /tracks/ metadata index returns nothing for many valid tracks (old
        # or freshly published), so tracks are cached as they are listed and
        # get_track_info reads metadata from here before asking /tracks/.
        self._track_cache: "OrderedDict[str, Track]" = OrderedDict()
        self._cache_max = 5000
        # Related Artists resolves the same popular artists across searches;
        # serving them from here skips the /artists round-trip.
        self._artist_cache: "OrderedDict[str, BrowseItem]" = OrderedDict()
        self._artist_cache_max = 1000
        logger.info("Jamendo audio format: %s", self.audio_format)

    def _cache_track(self, metadata: Track) -> None:
        cache = self._track_cache
        cache[metadata.id.id] = metadata
        cache.move_to_end(metadata.id.id)
        while len(cache) > self._cache_max:
            cache.popitem(last=False)

    def _cache_artist(self, item: BrowseItem) -> None:
        cache = self._artist_cache
        cache[item.id.id] = item
        cache.move_to_end(item.id.id)
        while len(cache) > self._artist_cache_max:
            cache.popitem(last=False)

    def module_name(self) -> str:
        return "Jamendo"

    def display_name(self) -> str:
        """Human-friendly source name for section headers."""
        return "Jamendo"

    async def search(
        self, type: SearchType, query: str, offset=0, limit=50
    ) -> BrowseItemList:
        limit = min(limit, MAX_LIMIT)
        endpoint = type.value + "s"
        params = {"namesearch": query, "offset": offset, "limit": limit}
        if type == SearchType.track:
            params["audioformat"] = self.audio_format
            params["include"] = "musicinfo"
        results = await self.client.request(endpoint, params)
        items = self._items_of_kind(type.value, results)

        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=_estimated_total(offset, limit, len(items)),
            items=items,
        )

    async def ai_search(
        self, query: str, offset: int = 0, limit: int = 50
    ) -> BrowseItemList:
        """Natural-language mood/genre search over JamendoMaxCaps embeddings.

        Returns a single AI-suggestions catalog card ("DISCOVER ON JAMENDO")
        of tracks ranked by semantic proximity to the query — the plugin owns
        this card's presentation. Independent of search(): no name matching, no
        albums/artists. Empty when the mood index is unavailable.
        """
        limit = min(limit, MAX_LIMIT)
        if self._mood_index is None or not query.strip():
            return EmptyList(offset, limit)

        hits = await self._mood_index.search(query, offset + limit)
        page = hits[offset : offset + limit]
        if not page:
            return EmptyList(offset, limit)

        # Some ids won't resolve; keep whatever does, in mood order.
        ids = [str(tid) for tid, _ in page]
        raw = await self.client.request(
            "tracks",
            {
                "id": " ".join(ids),
                "audioformat": self.audio_format,
                "include": "musicinfo",
                "limit": len(ids),
            },
        )
        by_id = {str(t.get("id")): t for t in raw}
        ordered = [by_id[i] for i in ids if i in by_id]
        tracks = self._tracks_to_browse_items(ordered)
        if not tracks:
            return EmptyList(offset, limit)

        cat = catalog_id("ai_search:tracks")
        card = BrowseItem(
            id=cat,
            name="DISCOVER ON JAMENDO",
            subname="Open music matching this vibe",
            can_browse=False,
            can_add=False,
            catalog=Catalog(
                id=cat,
                title="DISCOVER ON JAMENDO",
                sources=[cat.source],
                preview_config=Preview(
                    type=PreviewType.CARD,
                    content_type=PreviewContentType.TRACK,
                    icon="ai_suggestions",
                    items_count=len(tracks),
                ),
            ),
            sections=tracks,
        )
        return BrowseItemList(offset=offset, limit=limit, total=1, items=[card])

    async def browse(
        self,
        entity_id: EntityId,
        offset: PositiveInt = 0,
        limit: PositiveInt = 50,
        filter: Optional[FilterQuery] = None,
    ) -> BrowseItemList:
        limit = min(limit, MAX_LIMIT)
        filter = or_unfiltered(filter)
        if entity_id.type == EntityType.CATALOG:
            return await self._browse_catalog(entity_id.id, offset, limit, filter)

        # These listings declare no filters, so a field sent to one is refused.
        filter.reject_undeclared(())
        if entity_id.type == EntityType.ALBUM:
            return await self._browse_album(entity_id.id, offset, limit)
        elif entity_id.type == EntityType.ARTIST:
            return await self._browse_artist(entity_id.id, offset, limit)
        elif entity_id.type == EntityType.PLAYLIST:
            return await self._browse_playlist(entity_id.id, offset, limit)
        return EmptyList(offset, limit)

    async def _browse_album(
        self, id: str, offset: int, limit: int
    ) -> BrowseItemList:
        results = await self.client.request(
            "albums/tracks",
            {
                "id": id,
                "track_type": "albumtrack",
                "audioformat": self.audio_format,
            },
        )
        if not results:
            return EmptyList(offset, limit)
        tracks = results[0].get("tracks", [])
        page = tracks[offset : offset + limit]
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=len(tracks),
            items=self._tracks_to_browse_items(page, album_meta=results[0]),
        )

    async def _browse_artist(
        self, id: str, offset: int, limit: int
    ) -> BrowseItemList:
        # Use /albums/?artist_id= rather than /artists/albums/: the latter's
        # nested albums carry only a placeholder cover URL (no representative
        # trackid, which Jamendo album art is keyed on), so cards showed a
        # generic icon. /albums/ returns the real trackid-bearing cover, plus
        # artist_name and native offset/limit pagination over albums.
        albums = await self.client.request(
            "albums",
            {"artist_id": id, "offset": offset, "limit": limit},
        )
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=_estimated_total(offset, limit, len(albums)),
            items=self._albums_to_browse_items(albums),
        )

    async def _browse_playlist(
        self, id: str, offset: int, limit: int
    ) -> BrowseItemList:
        results = await self.client.request(
            "playlists/tracks",
            {"id": id, "audioformat": self.audio_format},
        )
        if not results:
            return EmptyList(offset, limit)
        tracks = results[0].get("tracks", [])
        page = tracks[offset : offset + limit]
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=len(tracks),
            items=self._tracks_to_browse_items(page),
        )

    async def _browse_catalog(
        self,
        endpoint: str,
        offset: int,
        limit: int,
        filter: FilterQuery,
    ) -> BrowseItemList:
        if endpoint == POPULAR_ENDPOINT:
            return await self._browse_popular(offset, limit, filter)

        # Narrowed before anything is listed, so an undeclared field is
        # refused rather than dropped; the shelf keeps its own order either way.
        narrowing = _narrowing_params(filter, SHELF_FILTERS.get(endpoint, []))

        if endpoint == "root":
            return self._root_catalog(offset, limit)

        section = SECTION_BY_ENDPOINT.get(endpoint)
        if section is not None:
            items = await self._section_items(section, offset, limit, narrowing)
        elif endpoint == "new-releases":
            today = date.today()
            since = today - timedelta(days=NEW_RELEASES_WINDOW_DAYS)
            results = await self.client.request(
                "albums",
                {
                    "order": "releasedate_desc",
                    "offset": offset,
                    "limit": limit,
                    "datebetween": f"{since.isoformat()}_{today.isoformat()}",
                    **narrowing,
                },
            )
            items = self._albums_to_browse_items(results)
        elif endpoint == "featured-playlists":
            results = await self.client.request(
                "playlists",
                {
                    "order": "creationdate_desc",
                    "offset": offset,
                    "limit": limit,
                    **narrowing,
                },
            )
            items = self._playlists_to_browse_items(results)
        else:
            return EmptyList(offset, limit)

        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=_estimated_total(offset, limit, len(items)),
            items=items,
        )

    async def _browse_popular(
        self, offset: int, limit: int, filter: FilterQuery
    ) -> BrowseItemList:
        sections = _popular_sections(filter)
        narrowing = _narrowing_params(filter, POPULAR_FILTERS)
        if "tags" in narrowing and any(
            section.kind != SearchType.track.value for section in sections
        ):
            raise UnsupportedFilter(
                GENRE_FILTER.id, "only tracks carry genres on this source"
            )

        if len(sections) == 1:
            items = await self._section_items(sections[0], offset, limit, narrowing)
            more = bool(limit) and len(items) >= limit
        else:
            items, more = await self._interleaved_items(
                sections, offset, limit, narrowing
            )

        # Not _estimated_total: a page left short by one exhausted section
        # still has the others behind it, and a total that stopped here would
        # take their remaining rows with it.
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=offset + len(items) + (limit if more else 0),
            items=items,
        )

    async def _section_items(
        self,
        section: _PopularSection,
        offset: int,
        limit: int,
        narrowing: dict,
    ) -> List[BrowseItem]:
        if limit <= 0:
            return []
        params = {
            "order": section.order,
            "offset": offset,
            "limit": limit,
            **narrowing,
        }
        if section.kind == SearchType.track.value:
            params["audioformat"] = self.audio_format
        results = await self.client.request(section.path, params)
        return self._items_of_kind(section.kind, results)

    def _items_of_kind(self, kind: str, results) -> List[BrowseItem]:
        """Jamendo rows of one kind as browse items."""
        return {
            SearchType.track.value: self._tracks_to_browse_items,
            SearchType.album.value: self._albums_to_browse_items,
            SearchType.artist.value: self._artists_to_browse_items,
            SearchType.playlist.value: self._playlists_to_browse_items,
        }[kind](results)

    async def _interleaved_items(
        self,
        sections: Tuple[_PopularSection, ...],
        offset: int,
        limit: int,
        narrowing: dict,
    ) -> Tuple[List[BrowseItem], bool]:
        """Popular with no kind chosen: the sections round-robined into one
        listing, which is what a consumer that cannot show shelves gets.

        Position ``i`` always holds section ``i % n``'s row ``i // n``, so
        paging neither repeats nor skips a row when one section runs short.

        Also answers whether any section filled its span, since an exhausted
        one leaves gaps and the page alone can no longer say.
        """
        n = len(sections)
        end = offset + limit
        spans = []
        for index in range(n):
            first = offset + (index - offset) % n
            spans.append((first // n, len(range(first, end, n))))

        pages = await asyncio.gather(
            *(
                self._section_items(section, start, count, narrowing)
                for section, (start, count) in zip(sections, spans)
            )
        )

        items = []
        for position in range(offset, end):
            start, _ = spans[position % n]
            rows = pages[position % n]
            row = position // n - start
            if row < len(rows):
                items.append(rows[row])

        more = any(
            count and len(rows) >= count for rows, (_, count) in zip(pages, spans)
        )
        return items, more

    def _root_catalog(self, offset: int, limit: int) -> BrowseItemList:
        all_items = [
            _catalog_card(
                POPULAR_ENDPOINT,
                "Popular",
                description="Most played on Jamendo this month",
                preview=Preview(
                    type=PreviewType.TILE,
                    icon="popular",
                    items_count=20,
                    rows_count=2,
                    aspect_ratio=1.0,
                ),
                sections=[
                    _popular_section_card(section) for section in POPULAR_SECTIONS
                ],
            ),
            _catalog_card(
                "new-releases",
                "New Releases",
                description="Fresh albums, just added",
                preview=Preview(
                    type=PreviewType.IMAGE_TEXT,
                    content_type=PreviewContentType.ALBUM,
                    icon="new_releases",
                    items_count=20,
                    rows_count=2,
                    aspect_ratio=1.0,
                ),
            ),
            _catalog_card(
                "featured-playlists",
                "Featured Playlists",
                description="Hand-picked collections",
                preview=Preview(
                    type=PreviewType.IMAGE_TEXT,
                    content_type=PreviewContentType.PLAYLIST,
                    icon="playlist",
                    items_count=20,
                    rows_count=2,
                    aspect_ratio=1.0,
                ),
                role=CatalogRole.HIDE_ON_HOME,
            ),
        ]
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=len(all_items),
            items=all_items[offset : offset + limit],
        )

    async def get_track_info(self, track_ids: List[str]) -> List[TrackInfo]:
        if not track_ids:
            return []

        missing = [tid for tid in track_ids if tid not in self._track_cache]
        fetched: dict[str, Track] = {}
        for i in range(0, len(missing), MAX_LIMIT):
            chunk = missing[i : i + MAX_LIMIT]
            results = await self.client.request(
                "tracks", {"id": " ".join(chunk), "limit": len(chunk)}
            )
            for track in results:
                meta = self._track_metadata(track)
                fetched[meta.id.id] = meta

        # A track with no metadata still plays: /tracks/file/ resolves it by id.
        infos: List[TrackInfo] = []
        without_metadata: List[str] = []
        for tid in track_ids:
            if tid in self._track_cache:
                metadata = self._track_cache[tid]
            elif tid in fetched:
                metadata = fetched[tid]
            else:
                metadata = self._placeholder_metadata(tid)
                without_metadata.append(tid)
            infos.append(self._make_track_info(tid, metadata))

        if without_metadata:
            logger.warning(
                "Jamendo get_track_info: no metadata for %d/%d track(s) "
                "(not in cache or the tracks index); they remain playable via "
                "the file endpoint: %s",
                len(without_metadata),
                len(track_ids),
                without_metadata,
            )
        logger.info(
            "Jamendo get_track_info: returning %d/%d track(s)",
            len(infos),
            len(track_ids),
        )
        return infos

    def _make_track_info(self, tid: str, metadata: Track) -> TrackInfo:
        """Build a TrackInfo whose link resolves via /tracks/file/ at play time."""

        async def source_retriever() -> TrackSource:
            url = await self.client.resolve_audio_url(tid, self.audio_format)
            if not url:
                # Raise rather than return an empty URL: the server treats any
                # non-exception return as playable, so it would try to stream
                # "". Raising lets it mark the track unavailable and skip it.
                raise RuntimeError(
                    f"Could not resolve audio URL for Jamendo track {tid}"
                )
            return TrackSource(source=DirectUrl(url=url), format=self.audio_mime)

        return TrackInfo(
            id=track_id(tid),
            source_retriever=source_retriever,
            metadata=metadata,
        )

    def _placeholder_metadata(self, tid: str) -> Track:
        return Track(
            id=track_id(tid),
            title="",
            duration=0,
            album=Album(id=album_id(""), title=""),
        )

    async def get(self, entity_id: EntityId) -> BrowseItem:
        if entity_id.type == EntityType.TRACK:
            results = await self.client.request(
                "tracks",
                {"id": entity_id.id, "audioformat": self.audio_format},
            )
            items = self._tracks_to_browse_items(results)
        elif entity_id.type == EntityType.ALBUM:
            results = await self.client.request("albums", {"id": entity_id.id})
            items = self._albums_to_browse_items(results)
        elif entity_id.type == EntityType.ARTIST:
            results = await self.client.request("artists", {"id": entity_id.id})
            items = self._artists_to_browse_items(results)
        elif entity_id.type == EntityType.PLAYLIST:
            results = await self.client.request("playlists", {"id": entity_id.id})
            items = self._playlists_to_browse_items(results)
        else:
            raise ValueError(f"Unsupported EntityId type: {entity_id.type.name}")

        if not items:
            raise ValueError(f"Entity not found: {entity_id.to_string}")
        return items[0]

    async def get_all(self, entity_ids: List[EntityId]) -> List[BrowseItem]:
        """Batch resolve (SDK 1.3). Artists resolve in ONE ``/artists`` call
        — the ``id`` param accepts a space-separated list — with an LRU cache
        in front, so Related Artists costs at most one round-trip per search
        instead of one per artist. Other types fall back to per-id get()."""
        artist_ids = [e.id for e in entity_ids if e.type == EntityType.ARTIST]
        missing = [i for i in artist_ids if i not in self._artist_cache]
        if missing:
            try:
                results = await self.client.request(
                    "artists", {"id": " ".join(missing)}
                )
            except Exception as e:
                logger.warning("artist batch resolve failed: %s", e)
                results = []
            for item in self._artists_to_browse_items(results):
                self._cache_artist(item)
        out: List[BrowseItem] = []
        for eid in entity_ids:
            if eid.type == EntityType.ARTIST:
                item = self._artist_cache.get(eid.id)
                if item is not None:
                    self._artist_cache.move_to_end(eid.id)
                    out.append(item)
                continue
            try:
                out.append(await self.get(eid))
            except Exception:
                continue
        return out

    def _track_metadata(self, track, album_meta: Optional[dict] = None) -> Track:
        album_meta = album_meta or {}
        aid = str(track.get("album_id") or album_meta.get("id") or "")
        album_name = track.get("album_name") or album_meta.get("name") or ""
        image = (
            track.get("album_image")
            or track.get("image")
            or album_meta.get("image")
        )
        # /albums/tracks nests tracks under the album and does not repeat the
        # artist on each track (it lives on the album object), so fall back to
        # album_meta. Without this, album tracks reach the playqueue with an
        # empty artist even though browse shows the album-level artist.
        performer = Artist(
            id=artist_id(
                str(track.get("artist_id") or album_meta.get("artist_id") or "")
            ),
            name=track.get("artist_name") or album_meta.get("artist_name") or "",
        )
        return Track(
            id=track_id(str(track["id"])),
            title=track.get("name", ""),
            duration=int(track.get("duration", 0) or 0),
            performer=performer,
            album=Album(
                id=album_id(aid),
                title=album_name,
                artist=performer,
                image=_cover(image),
                genres=_genres_of(track),
            ),
        )

    def _tracks_to_browse_items(
        self, tracks, album_meta: Optional[dict] = None
    ) -> List[BrowseItem]:
        result = []
        for track in tracks:
            meta = self._track_metadata(track, album_meta)
            # Lets get_track_info label ids the /tracks/ index can't resolve.
            self._cache_track(meta)
            result.append(
                BrowseItem(
                    id=meta.id,
                    name=meta.title,
                    subname=meta.performer.name if meta.performer else None,
                    can_browse=False,
                    can_add=True,
                    track=meta,
                )
            )
        return result

    def _albums_to_browse_items(self, albums) -> List[BrowseItem]:
        result = []
        for album in albums:
            aid = str(album["id"])
            artist = Artist(
                id=artist_id(str(album.get("artist_id", ""))),
                name=album.get("artist_name", ""),
            )
            result.append(
                BrowseItem(
                    id=album_id(aid),
                    name=album.get("name", ""),
                    subname=artist.name,
                    can_browse=True,
                    can_add=True,
                    album=Album(
                        id=album_id(aid),
                        title=album.get("name", ""),
                        artist=artist,
                        image=_cover(album.get("image")),
                    ),
                    sections=[_album_tracks_section(aid)],
                )
            )
        return result

    def _artists_to_browse_items(self, artists) -> List[BrowseItem]:
        result = []
        for artist in artists:
            aid = str(artist["id"])
            result.append(
                BrowseItem(
                    id=artist_id(aid),
                    name=artist.get("name", ""),
                    subname=None,
                    can_browse=True,
                    can_add=False,
                    artist=Artist(
                        id=artist_id(aid),
                        name=artist.get("name", ""),
                        image=_cover(artist.get("image")),
                    ),
                    sections=[_artist_albums_section(aid)],
                )
            )
        return result

    def _playlists_to_browse_items(self, playlists) -> List[BrowseItem]:
        result = []
        for playlist in playlists:
            pid = str(playlist["id"])
            tracks = playlist.get("tracks", [])
            owner = Owner(
                name=playlist.get("user_name", ""),
                id=user_id(str(playlist.get("user_id", ""))),
            )
            result.append(
                BrowseItem(
                    id=playlist_id(pid),
                    name=playlist.get("name", ""),
                    subname=owner.name,
                    can_browse=True,
                    can_add=True,
                    playlist=Playlist(
                        id=playlist_id(pid),
                        name=playlist.get("name", ""),
                        owner=owner,
                        description=None,
                        track_count=len(tracks),
                    ),
                    sections=[_playlist_tracks_section(pid)],
                )
            )
        return result

    async def list_favorite(
        self, type: SearchType, filter: str, offset: int = 0, limit: int = 50
    ) -> BrowseItemList:
        return EmptyList(offset, limit)

    async def get_favorite_ids(self) -> FavoriteIds:
        return FavoriteIds()

    async def add_to_favorite(self, id: str):
        logger.warning("Favorites are not supported in the Jamendo input module")

    async def remove_from_favorite(self, id: str):
        logger.warning("Favorites are not supported in the Jamendo input module")

    async def list_filter_values(
        self,
        catalog_id: EntityId,
        field: str,
        offset: int = 0,
        limit: int = 50,
        q: str = "",
    ) -> FilterValueList:
        declared = {spec.id for spec in SHELF_FILTERS.get(catalog_id.id, [])}
        if field == TYPE_FIELD and field in declared:
            return _kind_values(offset, limit, q)
        if field == GENRE_FILTER.id and field in declared:
            return _genre_values(offset, limit, q)
        raise UnsupportedFilter(field, "no such vocabulary here")

    async def playlist_user_list(
        self, offset: int = 0, limit: int = 25
    ) -> BrowseItemList:
        return EmptyList(offset, limit)

    async def playlist_create(self, name: str, description: str) -> Playlist:
        raise NotImplementedError(
            "Playlist management is not supported in the Jamendo input module"
        )

    async def playlist_update(
        self, id: str, name: Optional[str], description: Optional[str]
    ) -> Playlist:
        raise NotImplementedError(
            "Playlist management is not supported in the Jamendo input module"
        )

    async def playlist_delete(self, id: str):
        raise NotImplementedError(
            "Playlist management is not supported in the Jamendo input module"
        )

    async def playlist_add_tracks(
        self, id: str, track_ids: List[str], allow_duplicates: bool
    ) -> Playlist:
        raise NotImplementedError(
            "Playlist management is not supported in the Jamendo input module"
        )

    async def playlist_remove_tracks(
        self, id: str, playlist_track_ids: List[str]
    ) -> Playlist:
        raise NotImplementedError(
            "Playlist management is not supported in the Jamendo input module"
        )

    async def get_resource_path(self, id: str) -> str | None:
        return None
