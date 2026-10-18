"""Browsing an entity, and filling in the controls that narrow it.

The filter document's field ids belong to the module that declared them, so
neither route interprets one: they check its shape and hand it over. A module
that was not offered a field refuses it, and that refusal surfaces as 422 —
never as a listing that looks filtered but is not.
"""

import logging
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import ValidationError

from kalinka_plugin_sdk.datamodel import BrowseItemList, EntityId, EntityType
from kalinka_plugin_sdk.filters import FilterQuery, FilterValueList, UnsupportedFilter

from .browse_source import BrowseSource

logger = logging.getLogger(__name__)


def _parse_filter(raw: Optional[str]) -> FilterQuery:
    """The ``filter`` parameter as a document. Absent is unconstrained; the
    shape is checked here so a module only ever sees a well-formed one.

    A shape error is reported as the field it is in, the same detail an
    unsupported field produces — one error shape for the caller, and none of
    the selector union's internals."""
    if not raw:
        return FilterQuery({})
    try:
        return FilterQuery.model_validate_json(raw)
    except ValidationError as e:
        first = e.errors(include_url=False, include_context=False)[0]
        location = first.get("loc") or ()
        if location:
            field, reason = str(location[0]), "not a valid selector"
        else:
            field, reason = "filter", str(first.get("msg", "invalid filter"))
        raise HTTPException(
            status_code=422, detail={"field": field, "reason": reason}
        )


def register_browse_routes(
    app: FastAPI,
    resolve_module: Callable[[EntityId], BrowseSource],
    parse_entity_id: Callable[[str], EntityId],
    list_roots: Callable[[int, int], BrowseItemList],
    decorate: Callable[[BrowseItemList], None] = lambda result: None,
) -> None:
    """Mount the browsing endpoints on `app`.

    ``resolve_module`` maps an entity id to its input module, raising for one
    that is unknown or disabled; ``list_roots`` pages the sources' root
    catalogs; ``decorate`` is the server's own pass over a result (catalog card
    art) before it is serialised.
    """

    @app.get("/browse")
    def browse_root(offset: int = Query(0, ge=0), limit: int = Query(10, ge=0)):
        """Browse the root catalog."""
        return list_roots(offset, limit).model_dump(exclude_unset=True)

    @app.get("/browse/{id}")
    async def browse_entity(
        id: str,
        offset: int = 0,
        limit: int = 10,
        filter: Optional[str] = Query(None),
    ):
        """Browse an entity by its ID, narrowed by an optional filter."""
        entity_id = parse_entity_id(id)
        query = _parse_filter(filter)

        try:
            input_module = resolve_module(entity_id)
            result = await input_module.browse(
                entity_id, offset=offset, limit=limit, filter=query
            )
            decorate(result)
            return result.model_dump(exclude_unset=True)
        except HTTPException:
            raise
        except UnsupportedFilter as e:
            raise HTTPException(
                status_code=422, detail={"field": e.field, "reason": e.reason}
            )
        except Exception as e:
            # repr, not str: httpx timeout exceptions stringify to "".
            logger.error(f"Error browsing entity {id}: {e!r}")
            raise HTTPException(
                status_code=500, detail=f"Internal server error: {str(e)}"
            )

    @app.get("/browse/{id}/filter/{field}/values")
    async def list_filter_values(
        id: str,
        field: str,
        offset: int = 0,
        limit: int = 50,
        q: str = "",
    ) -> FilterValueList:
        """List one catalog filter field's vocabulary.

        Separate from the catalog itself because a vocabulary can be long and
        is wanted only when a control is about to show it.
        """
        entity_id = parse_entity_id(id)
        if entity_id.type is not EntityType.CATALOG:
            raise HTTPException(
                status_code=422, detail={"field": "id", "reason": "not a catalog"}
            )

        try:
            input_module = resolve_module(entity_id)
            return await input_module.list_filter_values(
                entity_id, field, offset=offset, limit=limit, q=q
            )
        except HTTPException:
            raise
        except UnsupportedFilter as e:
            raise HTTPException(
                status_code=422, detail={"field": e.field, "reason": e.reason}
            )
        except Exception as e:
            logger.error(f"Error listing {field} values for {id}: {e!r}")
            raise HTTPException(
                status_code=500, detail=f"Internal server error: {str(e)}"
            )
