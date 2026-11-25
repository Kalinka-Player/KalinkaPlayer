"""The favourites endpoints: what the listener has marked, source by source.

Only a source that reports ``favorites`` is asked. A read skips the others,
so naming a source that keeps none is an empty answer, as it is for one that
only browses. A change refuses them, since nothing would be kept.

A read is whole or it fails, naming the source that failed. A client fills
its hearts from it, and an answer that leaves one source out is not one it
can tell from that source having no favourites; asking a source at a time,
it loses only the hearts of the source that is down.

A change that was made is announced on the queue bus, so another client
showing the item need not ask again. A collection change is not, as nothing
shows a collection that is not open; a heart is on the screen wherever the
playing track is.
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable, List, Optional, TypeVar

from fastapi import FastAPI, HTTPException, Query
from pydantic import ValidationError

from kalinka_plugin_sdk.datamodel import (
    BrowseItemList,
    EmptyList,
    EntityId,
    EntityType,
    FavoriteIds,
)
from kalinka_plugin_sdk.events import FavoriteChangedEvent
from kalinka_plugin_sdk.filters import UnsupportedFilter
from kalinka_plugin_sdk.inputmodule import (
    InputModule,
    ModuleCapability,
    SearchType,
    SourceUnavailableError,
)

from .merge_utils import BrowseItemsSource, k_way_merge_browse_items
from .module_capabilities import capabilities_of
from .source_failed import SourceFailed

logger = logging.getLogger(__name__)

T = TypeVar("T")

FAVOURITE_KINDS = frozenset(EntityType(kind.value) for kind in SearchType)

# A ValueError or LookupError says why an item cannot be marked; these
# subclasses are a plugin failing to read its source's answer instead.
ACCIDENTS = (KeyError, IndexError, UnicodeError, json.JSONDecodeError, ValidationError)


def register_favorite_routes(
    app: FastAPI,
    resolve_modules: Callable[[Optional[str]], List[InputModule]],
    resolve_module: Callable[[EntityId], InputModule],
    parse_entity_id: Callable[[str], EntityId],
    publish: Callable[[FavoriteChangedEvent], None],
) -> None:
    """Mount the favourites endpoints on ``app``.

    ``resolve_modules`` turns a ``sources`` parameter into input modules,
    every enabled one when it is absent, and ``resolve_module`` finds the
    module an entity belongs to; both raise for what they cannot reach.
    ``parse_entity_id`` refuses an id that is not one. ``publish`` sends a
    change that was made to the queue bus.
    """

    @app.get("/favorite/list/{type}")
    async def list_favorite(
        type: SearchType,
        filter: str = "",
        sources: Optional[str] = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(10, ge=1),
    ) -> BrowseItemList:
        """One kind of favourite, most recently marked first."""
        keepers = _keepers(resolve_modules(sources))
        if not keepers:
            return EmptyList(offset, limit)
        try:
            if len(keepers) == 1:
                # Merging would read one source from its start to reach a
                # page deep in it, a plugin call at a time.
                return await _page(keepers[0], type, filter)(offset, limit)
            return await k_way_merge_browse_items(
                [_page(module, type, filter) for module in keepers],
                compared_value=lambda item: item.timestamp,
                offset=offset,
                limit=limit,
            )
        except UnsupportedFilter as e:
            raise HTTPException(
                status_code=422, detail={"field": e.field, "reason": e.reason}
            )
        except SourceFailed as e:
            raise _unavailable(e)

    @app.get("/favorite/ids")
    async def get_favorite_ids(
        sources: Optional[str] = None,
        source: Optional[str] = Query(None, deprecated=True),
    ) -> FavoriteIds:
        """Every favourite's id, by kind. ``source`` is the parameter's
        earlier name."""
        if sources is not None and source is not None:
            raise HTTPException(
                status_code=422, detail="Give `sources` or `source`, not both"
            )
        keepers = _keepers(resolve_modules(sources if source is None else source))
        try:
            answers = await asyncio.gather(
                *(_answer(module, module.get_favorite_ids()) for module in keepers)
            )
        except SourceFailed as e:
            raise _unavailable(e)
        return FavoriteIds(
            **{
                kind: [item for answer in answers for item in getattr(answer, kind)]
                for kind in FavoriteIds.model_fields
            }
        )

    @app.put("/favorite/add/{id}")
    async def add_favorite(id: str):
        await _change(id, True)
        return {"message": "Ok"}

    @app.delete("/favorite/remove/{id}")
    async def remove_favorite(id: str):
        await _change(id, False)
        return {"message": "Ok"}

    async def _change(id: str, favorite: bool) -> None:
        entity_id = parse_entity_id(id)
        if entity_id.type not in FAVOURITE_KINDS:
            raise HTTPException(
                status_code=422,
                detail={
                    "field": "id",
                    "reason": "Only tracks, albums, artists and playlists "
                    "can be favourites",
                },
            )
        module = resolve_module(entity_id)
        if not _keeps(module):
            raise HTTPException(
                status_code=409,
                detail={
                    "source": entity_id.source,
                    "reason": "This source keeps no favourites",
                },
            )
        change = module.add_to_favorite if favorite else module.remove_from_favorite
        try:
            await change(id)
        except ACCIDENTS as e:
            raise _unavailable(SourceFailed(entity_id.source, e))
        except ValueError as e:
            raise HTTPException(
                status_code=422, detail={"field": "id", "reason": str(e)}
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="No such item")
        except Exception as e:
            raise _unavailable(SourceFailed(entity_id.source, e))
        publish(FavoriteChangedEvent(id=entity_id, favorite=favorite))


def _keeps(module: InputModule) -> bool:
    return ModuleCapability.FAVORITES.value in capabilities_of(module)


def _keepers(modules: List[InputModule]) -> List[InputModule]:
    return [module for module in modules if _keeps(module)]


def _page(module: InputModule, type: SearchType, filter: str) -> BrowseItemsSource:
    async def page(offset: int, limit: int) -> BrowseItemList:
        return await _answer(
            module,
            module.list_favorite(type, filter, offset, limit),
            refusals=UnsupportedFilter,
        )

    return page


async def _answer(module: InputModule, call: Awaitable[T], *, refusals=()) -> T:
    """What ``call`` answers. A failure names the module; ``refusals`` are
    the request's own fault and pass as they are."""
    try:
        return await call
    except refusals:
        raise
    except Exception as e:
        raise SourceFailed(module.module_name(), e)


def _unavailable(failure: SourceFailed) -> HTTPException:
    """503 for a source that could not answer, its own words where it said
    why; 504 where it ran out of time, so a change may or may not be made."""
    cause = failure.cause
    logger.warning("favourites: source %s failed: %r", failure.source, cause)
    if isinstance(cause, TimeoutError):
        return HTTPException(
            status_code=504,
            detail={"source": failure.source, "reason": "Did not answer in time"},
        )
    reason = (
        str(cause)
        if isinstance(cause, SourceUnavailableError) and str(cause)
        else "Source unavailable"
    )
    return HTTPException(
        status_code=503, detail={"source": failure.source, "reason": reason}
    )
