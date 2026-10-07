"""Collections as a source you browse.

A collection is a playlist — the same entity every source already answers with
— so a client that can render a playlist can render one of these without
learning anything new. What marks it is :attr:`BrowseItem.can_edit`: the
server accepts writes for it, and no other source says so.

The item carries both payloads on purpose. As a playlist it is a row that
unrolls in place; as a catalog under the same id it is a page that declares
the fields it can be narrowed by. One record, two ways in.

Read only: this module never resolves audio. A collection's tracks keep the
ids of the sources that own them, and playback goes through those.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional, Tuple

from kalinka_plugin_sdk.datamodel import (
    BrowseItem,
    BrowseItemList,
    Catalog,
    CatalogRole,
    CardSize,
    EmptyList,
    EntityId,
    EntityType,
    Owner,
    Playlist,
    Preview,
    PreviewContentType,
    PreviewType,
    Track,
)
from kalinka_plugin_sdk.filters import (
    TEXT_FIELD,
    FilterKind,
    FilterOp,
    FilterQuery,
    FilterSpec,
    FilterValue,
    FilterValueList,
    UnsupportedFilter,
    or_unfiltered,
)
from kalinka_plugin_sdk.inputmodule import SearchType

from .store import (
    CollectionRow,
    CollectionStore,
    EntryFilter,
    EntryRow,
    FacetValue,
)

logger = logging.getLogger(__name__.split(".")[-1])

SOURCE_NAME = "collections"
DISPLAY_NAME = "Collections"

SHELF_TITLE = "Your collections"
SHELF_DESCRIPTION = "Your lists, mixed from any source"

ROOT_ENDPOINT = "root"
SHELF_ENDPOINT = "collections"

SOURCE_FIELD = "source"
GENRE_FIELD = "genre"

SHELF_FILTERS = [
    FilterSpec(id=TEXT_FIELD, kind=FilterKind.TEXT, label="Search your collections"),
]

COLLECTION_FILTERS = [
    FilterSpec(
        id=TEXT_FIELD, kind=FilterKind.TEXT, label="Search titles, artists and albums"
    ),
    FilterSpec(
        id=SOURCE_FIELD, kind=FilterKind.VALUES, label="Source", ops=[FilterOp.ANY]
    ),
    FilterSpec(
        id=GENRE_FIELD, kind=FilterKind.VALUES, label="Genre", ops=[FilterOp.ANY]
    ),
]


def collection_id(local_id: str) -> EntityId:
    return EntityId(id=local_id, type=EntityType.PLAYLIST, source=SOURCE_NAME)


def catalog_id(endpoint: str) -> EntityId:
    return EntityId(id=endpoint, type=EntityType.CATALOG, source=SOURCE_NAME)


class CollectionsSource:
    """The read side of collections, shaped as a source.

    ``source_title`` renders another source's name for the ``source`` facet:
    the store holds module names, and only the server knows what each is
    called. Defaults to the name itself.
    """

    def __init__(
        self,
        store: CollectionStore,
        source_title: Callable[[str], str] = lambda name: name,
    ):
        self._store = store
        self._source_title = source_title

    def module_name(self) -> str:
        return SOURCE_NAME

    def display_name(self) -> str:
        return DISPLAY_NAME

    async def browse(
        self,
        entity_id: EntityId,
        offset: int = 0,
        limit: int = 50,
        filter: Optional[FilterQuery] = None,
    ) -> BrowseItemList:
        query = or_unfiltered(filter)

        if entity_id.type == EntityType.PLAYLIST:
            return await self._browse_collection(entity_id.id, offset, limit, query)
        if entity_id.type == EntityType.CATALOG and entity_id.id == SHELF_ENDPOINT:
            return await self._browse_shelf(offset, limit, query)

        # What is left declares no filters, so a field sent to one is refused.
        query.reject_undeclared(())
        if entity_id.type == EntityType.CATALOG and entity_id.id == ROOT_ENDPOINT:
            return self._root(offset, limit)
        return EmptyList(offset, limit)

    async def get(self, entity_id: EntityId) -> BrowseItem:
        if entity_id.type == EntityType.CATALOG and entity_id.id == SHELF_ENDPOINT:
            return self._shelf_item()

        if entity_id.type == EntityType.PLAYLIST:
            row = await self._store.get_collection(entity_id.id)
            if row is not None:
                return (await self._collection_items([row]))[0]

        raise ValueError(f"No such collection: {entity_id.to_string}")

    async def search(
        self, type: SearchType, query: str, offset: int = 0, limit: int = 50
    ) -> BrowseItemList:
        """Collections whose name answers ``query``. Nothing else here has a
        name of its own: the tracks are the sources' own rows, found there."""
        if type != SearchType.playlist or not query.strip():
            return EmptyList(offset, limit)

        rows, total = await self._store.list_collections(offset, limit, query)
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=total,
            items=await self._collection_items(rows),
        )

    async def list_filter_values(
        self,
        catalog_id: EntityId,
        field: str,
        offset: int = 0,
        limit: int = 50,
        q: str = "",
    ) -> FilterValueList:
        if catalog_id.type != EntityType.PLAYLIST:
            raise UnsupportedFilter(field, "no such vocabulary here")

        if field == SOURCE_FIELD:
            values = [
                FacetValue(
                    id=value.id, name=self._source_title(value.name), count=value.count
                )
                for value in await self._store.sources_of(catalog_id.id)
            ]
        elif field == GENRE_FIELD:
            values = await self._store.genres_of(catalog_id.id)
        else:
            raise UnsupportedFilter(field, "no such vocabulary here")

        needle = q.casefold()
        matching = [value for value in values if needle in value.name.casefold()]
        return FilterValueList(
            offset=offset,
            limit=limit,
            total=len(matching),
            items=[
                FilterValue(id=value.id, name=value.name, count=value.count)
                for value in matching[offset : offset + limit]
            ],
        )

    async def playlist_user_list(
        self, offset: int = 0, limit: int = 25
    ) -> BrowseItemList:
        return await self._browse_shelf(offset, limit, FilterQuery({}))

    async def tracks_to_add(
        self, entity_id: EntityId, limit: int
    ) -> Optional[List[EntityId]]:
        return None

    def _root(self, offset: int, limit: int) -> BrowseItemList:
        items = [self._shelf_item()]
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=len(items),
            items=items[offset : offset + limit],
        )

    def _shelf_item(self) -> BrowseItem:
        id = catalog_id(SHELF_ENDPOINT)
        return BrowseItem(
            id=id,
            name=SHELF_TITLE,
            can_browse=True,
            can_add=False,
            # What is editable about the shelf is which collections exist —
            # the same claim one level up from a collection's own tracks.
            can_edit=True,
            catalog=Catalog(
                id=id,
                title=SHELF_TITLE,
                description=SHELF_DESCRIPTION,
                filters=SHELF_FILTERS,
                role=CatalogRole.LIBRARY,
                # A few rows and a way to the rest: the shelf is a listing,
                # not a rack of cards.
                preview_config=Preview(
                    type=PreviewType.TILE,
                    content_type=PreviewContentType.PLAYLIST,
                    icon="playlist",
                    items_count=3,
                    rows_count=3,
                    card_size=CardSize.SMALL,
                ),
            ),
        )

    async def _browse_shelf(
        self, offset: int, limit: int, filter: FilterQuery
    ) -> BrowseItemList:
        filter.reject_undeclared({spec.id for spec in SHELF_FILTERS})
        rows, total = await self._store.list_collections(offset, limit, filter.text())
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=total,
            items=await self._collection_items(rows),
        )

    async def _collection_items(self, rows: List[CollectionRow]) -> List[BrowseItem]:
        """Each row as an item, carrying the sources it draws on."""
        sources = await self._store.sources_by_collection([row.id for row in rows])
        return [_collection_item(row, sources.get(row.id, [])) for row in rows]

    async def _browse_collection(
        self, local_id: str, offset: int, limit: int, filter: FilterQuery
    ) -> BrowseItemList:
        listing = _listing(filter)
        if await self._store.get_collection(local_id) is None:
            raise ValueError(f"No such collection: {local_id}")

        rows, total = await self._store.list_entries(local_id, offset, limit, listing)
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=total,
            items=[item for item in map(_entry_item, rows) if item is not None],
        )


def _listing(filter: FilterQuery) -> EntryFilter:
    """A browse's filter in the store's terms, refusing what a collection
    never offered."""
    filter.reject_undeclared({spec.id for spec in COLLECTION_FILTERS})
    return EntryFilter(
        text=filter.text(),
        sources=_any_of(filter, SOURCE_FIELD),
        genres=_any_of(filter, GENRE_FIELD),
    )


def _any_of(filter: FilterQuery, field: str) -> Tuple[str, ...]:
    selector = filter.values(field)
    if selector is None:
        return ()
    if selector.all or selector.none:
        raise UnsupportedFilter(field, "only `any` is supported")
    return tuple(dict.fromkeys(selector.any))


def _collection_item(row: CollectionRow, sources: List[str]) -> BrowseItem:
    id = collection_id(row.id)
    return BrowseItem(
        id=id,
        name=row.name,
        can_browse=True,
        can_add=True,
        can_edit=True,
        subname=_track_count(row.track_count),
        timestamp=row.updated_at,
        playlist=Playlist(
            id=id,
            name=row.name,
            description=row.description,
            track_count=row.track_count,
            duration=row.duration,
            owner=Owner(
                name="You",
                id=EntityId(id="you", type=EntityType.USER, source=SOURCE_NAME),
            ),
        ),
        catalog=Catalog(
            id=id,
            title=row.name,
            description=row.description,
            filters=COLLECTION_FILTERS,
            sources=sources,
            preview_config=Preview(
                type=PreviewType.TILE,
                content_type=PreviewContentType.TRACK,
                items_count=15,
                rows_count=1,
                card_size=CardSize.SMALL,
            ),
        ),
    )


def _entry_item(entry: EntryRow) -> Optional[BrowseItem]:
    """The entry as the row it was when it was added, or None when the
    snapshot can no longer be read — one unreadable row must not cost the
    whole collection."""
    try:
        track = Track.model_validate_json(entry.track_json)
    except ValueError as e:
        logger.warning("Unreadable snapshot for entry %s: %s", entry.entry_id, e)
        return None

    track.playlist_track_id = entry.entry_id
    return BrowseItem(
        id=track.id,
        name=track.title,
        can_browse=False,
        can_add=True,
        subname=track.performer.name if track.performer else None,
        track=track,
    )


def _track_count(count: int) -> str:
    if count == 0:
        return "Empty"
    return "1 track" if count == 1 else f"{count} tracks"
