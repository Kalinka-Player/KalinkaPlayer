"""``/server/storage``: the volumes, the server's folders, and handing a
location to a library or taking it back."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kalinka_plugin_sdk.datamodel import EntityId
from kalinka_plugin_sdk.inputmodule import SourceUnavailableError
from kalinka_plugin_sdk.storage import LocationStatus, SourcesChange, StorageLocation

from .manager import LIBRARY_TIMEOUT_S, Adopter, StorageManager

ROUTE = "/server/storage"


class LibraryRequest(BaseModel):
    entity_id: str
    module: Optional[str] = None


def register_storage_routes(
    app: FastAPI,
    storage: StorageManager,
    location_of: Callable[[EntityId], Awaitable[Optional[StorageLocation]]],
    current_value: Callable[[str], Any],
    apply_changes: Callable[[Dict[str, Any]], Awaitable[Any]],
    list_folders: Callable[[Optional[str], int, int], Awaitable[BaseModel]],
) -> None:
    """Mount the storage routes.

    @param location_of Where the page an entity id names is on storage, or
        None for one that is not a folder of a volume.
    @param current_value The saved value at a dotted config path.
    @param apply_changes Commits config changes as ``PUT /server/config``
        does, answering what it answers.
    @param list_folders The folders in a path, or the places they can be in
        for None, a page at a time.
    """

    @app.get(f"{ROUTE}/volumes")
    async def list_volumes():
        return {"volumes": [v.model_dump(mode="json") for v in storage.volumes()]}

    @app.get(f"{ROUTE}/folders")
    async def folders(path: Optional[str] = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1)):
        try:
            found = await list_folders(path, offset, limit)
        except LookupError as e:
            raise HTTPException(status_code=404, detail="Not a folder on this server's storage") from e
        except SourceUnavailableError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        return found.model_dump(mode="json")

    @app.post(f"{ROUTE}/volumes/{{volume_id}}/eject", status_code=202)
    async def eject(volume_id: str):
        if storage.volume(volume_id) is None:
            raise HTTPException(status_code=404, detail="No such volume")
        if not storage.eject(volume_id):
            raise HTTPException(status_code=409, detail="This volume cannot be ejected")
        await storage.refresh()
        return {"message": "Ok"}

    @app.post(f"{ROUTE}/volumes/{{volume_id}}/mount", status_code=202)
    async def mount(volume_id: str):
        if not storage.mount(volume_id):
            raise HTTPException(status_code=404, detail="No configured mount with that id")
        await storage.refresh()
        return {"message": "Ok"}

    # One change to a library's sources at a time: each is made to the list
    # as saved, which another change in between would leave behind.
    changing = asyncio.Lock()

    async def _located(entity_id: str) -> StorageLocation:
        try:
            parsed = EntityId.from_string(entity_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid entity id: {e}") from e
        location = await location_of(parsed)
        if location is None:
            raise HTTPException(status_code=404, detail="Not a folder on a drive")
        return location

    @app.get(f"{ROUTE}/location")
    async def location(entity_id: str):
        found = await _located(entity_id)
        answer = await storage.location_status(found)
        library = None
        if answer is not None:
            adopter, status = answer
            library = {"module": adopter.module, "title": adopter.title, **status.model_dump(mode="json")}
        return {
            "volume_id": found.volume_id or None,
            "subpath": found.subpath,
            "label": found.label,
            "path": found.path,
            "library": library,
        }

    async def _library_of(request: LibraryRequest) -> tuple[StorageLocation, Adopter, LocationStatus]:
        found = await _located(request.entity_id)
        answer = await storage.location_status(found)
        if answer is None or (request.module and answer[0].module != request.module):
            raise HTTPException(status_code=422, detail="No library can keep this folder")
        return found, *answer

    async def _change(adopter: Adopter, ask: Callable[[], Awaitable[Optional[SourcesChange]]]) -> Optional[SourcesChange]:
        try:
            return await asyncio.wait_for(ask(), LIBRARY_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise HTTPException(status_code=504, detail=f"{adopter.title} did not answer") from e

    def _unchanged(adopter: Adopter):
        return {"module": adopter.module, "issues": [], "restart_required": False}

    async def _commit(adopter: Adopter, status: LocationStatus, change: SourcesChange):
        path = f"input_modules.{adopter.module}.{change.collection}"
        records = list(current_value(path) or [])
        if change.add is not None:
            records.append(change.add)
        else:
            kept = [record for record in records if record.get("id") != change.remove]
            if len(kept) == len(records):
                return _unchanged(adopter)
            records = kept
        result = await apply_changes({path: records})
        if isinstance(result, JSONResponse):
            return result
        await storage.refresh()
        return {
            "module": adopter.module,
            "issues": result.get("issues", []),
            "restart_required": status.restart_required,
        }

    @app.post(f"{ROUTE}/adopt")
    async def adopt(request: LibraryRequest):
        async with changing:
            found, adopter, status = await _library_of(request)
            if status.holds:
                return _unchanged(adopter)
            change = await _change(adopter, lambda: adopter.library.adopt_location(found))
            if change is None or change.add is None:
                raise HTTPException(status_code=422, detail=f"{adopter.title} cannot keep this folder")
            return await _commit(adopter, status, change)

    @app.post(f"{ROUTE}/release")
    async def release(request: LibraryRequest):
        async with changing:
            found, adopter, status = await _library_of(request)
            if not status.holds:
                return _unchanged(adopter)
            if not status.removable:
                raise HTTPException(
                    status_code=409,
                    detail=f"This folder is in {adopter.title} as part of a folder above it",
                )
            change = await _change(adopter, lambda: adopter.library.release_location(found))
            # Gone since it was asked about: there is nothing left to take out.
            if change is None or change.remove is None:
                return _unchanged(adopter)
            return await _commit(adopter, status, change)
