"""The collections write API.

Reading a collection goes through the browse plane like any other source, so
nothing here answers a listing. What lives here is everything that *changes*
one — the half that has no place in a source's contract, because no plugin
implements it.

A write cannot degrade the way a listing does. With the file unavailable a
listing answers empty and the app shows an empty shelf; a write must say so,
or the app reports a collection nobody made.
"""

import logging
from typing import Callable, Dict, List, Tuple

from fastapi import FastAPI, HTTPException
from kalinka_plugin_sdk.datamodel import EntityId, EntityType, Track
from kalinka_plugin_sdk.inputmodule import InputModule
from pydantic import BaseModel, Field

from ..browse_source import BrowseSource
from ..queue_add import tracks_for
from .source import SOURCE_NAME, collection_id
from .store import CollectionChanged, CollectionStore, NewEntry

logger = logging.getLogger(__name__.split(".")[-1])

MAX_NAME = 200
MAX_DESCRIPTION = 2000


class NewCollection(BaseModel):
    """What making a collection takes: a name, and optionally a line about it."""

    name: str = Field(max_length=MAX_NAME)
    description: str = Field(default="", max_length=MAX_DESCRIPTION)


class CollectionEdit(BaseModel):
    """What renaming takes."""

    name: str = Field(max_length=MAX_NAME)


class EntriesWrite(BaseModel):
    """What a write to a collection's entries takes.

    The tracks are named by their ids — or by the ids of anything holding
    them, which the owning source expands. ``allow_duplicates`` says whether
    the collection may end up holding one of them twice; each row keeps its
    own entry id either way, so a write can still address one of two
    identical tracks.
    """

    items: List[str] = Field(min_length=1)
    allow_duplicates: bool = False


class EntriesEdit(BaseModel):
    """What a staged edit takes: the entries to drop, and the order the rest
    end up in — both by entry id.

    Between them they name every entry the collection held when the editor
    read it, which is what lets the server tell an edit of that collection
    from an edit of one that has moved on since.
    """

    remove: List[str] = Field(default_factory=list)
    order: List[str] = Field(default_factory=list)


class CollectionRef(BaseModel):
    """A collection the client can now go to: its browse id and its name.

    Deliberately not the whole item — the listing it appears in is refetched
    anyway, and its cover does not exist until something is in it.
    """

    id: str
    name: str


class EntriesAdded(BaseModel):
    """How an add went, so the client can say so without refetching."""

    added: int
    already_there: int


class EntriesReplaced(BaseModel):
    """How a replace went: what the collection holds now, and what it lost."""

    added: int
    dropped: int


class EntriesEdited(BaseModel):
    """How an edit went: what it dropped, and how many of the rest the user
    actually moved."""

    removed: int
    moved: int


def _required_name(raw: str) -> str:
    """The name a write asked for, refusing one that is only whitespace."""
    name = raw.strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail={"field": "name", "reason": "a collection needs a name"},
        )
    return name


def _local_id(entity_id: str) -> str:
    """The store's id behind a collection's browse id. An id belonging to
    anything else names nothing here, so it is a miss rather than a refusal."""
    try:
        parsed = EntityId.from_string(entity_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="No such collection")
    if parsed.source != SOURCE_NAME or parsed.type != EntityType.PLAYLIST:
        raise HTTPException(status_code=404, detail="No such collection")
    return parsed.id


def _genres_of(track: Track) -> Tuple[Tuple[str, str], ...]:
    """The genres a facet counts this track under, keyed by its folded name
    rather than by the source's own id: a collection is mixed, and two sources
    number the same genre differently."""
    by_key: Dict[str, str] = {}
    for genre in track.album.genres:
        name = genre.name.strip()
        if name:
            by_key.setdefault(name.casefold(), name)
    return tuple(by_key.items())


def _entry_of(track: Track) -> NewEntry:
    """A track as the row a collection keeps."""
    performer = track.performer or track.album.artist
    return NewEntry(
        entity_id=track.id.to_string,
        source=track.id.source,
        title=track.title,
        artist=performer.name if performer else "",
        album=track.album.title,
        duration=track.duration,
        # Membership is of the collection it joins; the read side fills it in.
        track_json=track.model_copy(
            update={"playlist_track_id": None}
        ).model_dump_json(),
        genres=_genres_of(track),
    )


def register_collection_routes(
    app: FastAPI,
    store: CollectionStore,
    browse_source_for: Callable[[EntityId], BrowseSource],
    module_for: Callable[[str], InputModule],
) -> None:
    """Mount the collections write endpoints on `app`.

    Adding expands containers through the resolvers rather than through the
    store: what a client may add is whatever it may queue, so both go through
    the one helper that knows how to turn an id into tracks.
    """

    @app.post("/collections", status_code=201)
    async def create_collection(payload: NewCollection) -> CollectionRef:
        """Make an empty collection."""
        name = _required_name(payload.name)
        try:
            row = await store.create_collection(name, payload.description.strip())
        except RuntimeError as e:
            logger.error("Could not create collection: %s", e)
            raise HTTPException(status_code=503, detail="Collections unavailable")
        return CollectionRef(id=collection_id(row.id).to_string, name=row.name)

    @app.patch("/collections/{entity_id}")
    async def rename_collection(
        entity_id: str, payload: CollectionEdit
    ) -> CollectionRef:
        """Give a collection another name."""
        name = _required_name(payload.name)
        local_id = _local_id(entity_id)
        try:
            row = await store.rename_collection(local_id, name)
        except RuntimeError as e:
            logger.error("Could not rename %s: %s", entity_id, e)
            raise HTTPException(status_code=503, detail="Collections unavailable")
        if row is None:
            raise HTTPException(status_code=404, detail="No such collection")
        return CollectionRef(id=collection_id(row.id).to_string, name=row.name)

    @app.delete("/collections/{entity_id}", status_code=204)
    async def delete_collection(entity_id: str) -> None:
        """Remove a collection and everything in it."""
        local_id = _local_id(entity_id)
        try:
            gone = await store.delete_collection(local_id)
        except RuntimeError as e:
            logger.error("Could not delete %s: %s", entity_id, e)
            raise HTTPException(status_code=503, detail="Collections unavailable")
        if not gone:
            raise HTTPException(status_code=404, detail="No such collection")

    async def rows_for(items: List[str]) -> List[NewEntry]:
        """What ``items`` comes to, as rows to store."""
        try:
            tracks = await tracks_for(items, browse_source_for, module_for)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=f"Nothing to add: {e}")
        return [_entry_of(track) for track in tracks]

    @app.post("/collections/{entity_id}/entries")
    async def add_entries(entity_id: str, payload: EntriesWrite) -> EntriesAdded:
        """Put tracks at the end of a collection, expanding what holds them."""
        local_id = _local_id(entity_id)
        rows = await rows_for(payload.items)
        try:
            outcome = await store.add_entries(
                local_id, rows, allow_duplicates=payload.allow_duplicates
            )
        except RuntimeError as e:
            logger.error("Could not add to %s: %s", entity_id, e)
            raise HTTPException(status_code=503, detail="Collections unavailable")
        if outcome is None:
            raise HTTPException(status_code=404, detail="No such collection")
        return EntriesAdded(added=outcome.added, already_there=outcome.already_there)

    @app.patch("/collections/{entity_id}/entries")
    async def edit_entries(entity_id: str, payload: EntriesEdit) -> EntriesEdited:
        """Drop entries and lay the rest out, in one write."""
        local_id = _local_id(entity_id)
        try:
            outcome = await store.edit_entries(
                local_id, payload.remove, payload.order
            )
        except CollectionChanged as e:
            logger.info("Stale edit of %s: %s", entity_id, e)
            raise HTTPException(
                status_code=409, detail="This collection changed since you opened it"
            )
        except RuntimeError as e:
            logger.error("Could not edit %s: %s", entity_id, e)
            raise HTTPException(status_code=503, detail="Collections unavailable")
        if outcome is None:
            raise HTTPException(status_code=404, detail="No such collection")
        return EntriesEdited(removed=outcome.removed, moved=outcome.moved)

    @app.put("/collections/{entity_id}/entries")
    async def replace_entries(
        entity_id: str, payload: EntriesWrite
    ) -> EntriesReplaced:
        """Make a collection hold exactly these tracks, and nothing it held."""
        local_id = _local_id(entity_id)
        rows = await rows_for(payload.items)
        try:
            outcome = await store.replace_entries(
                local_id, rows, allow_duplicates=payload.allow_duplicates
            )
        except RuntimeError as e:
            logger.error("Could not replace %s: %s", entity_id, e)
            raise HTTPException(status_code=503, detail="Collections unavailable")
        if outcome is None:
            raise HTTPException(status_code=404, detail="No such collection")
        return EntriesReplaced(added=outcome.added, dropped=outcome.dropped)
