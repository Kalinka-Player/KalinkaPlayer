"""``/server/storage/devices`` and ``/server/storage/shares``: what Drives &
shares lists, and its actions."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .devices import DeviceChanges, DeviceError, NewShare, SaveChanges, StorageDevices, nfs_id
from .routes import ROUTE


class ProbeRequest(BaseModel):
    host: str


def saving_through(apply_changes: Callable[[Dict[str, Any]], Awaitable[Any]]) -> SaveChanges:
    """``apply_changes``, as ``PUT /server/config`` commits, answering why it
    refused a change instead of the response it refused it with."""

    async def save(changes: Dict[str, Any]) -> Optional[str]:
        result = await apply_changes(changes)
        if isinstance(result, JSONResponse):
            return json.loads(result.body).get("detail") or "The change was refused"
        return None

    return save


def register_device_routes(app: FastAPI, devices: StorageDevices) -> None:
    async def _carried_out(action: Awaitable[Any]) -> Any:
        try:
            return await action
        except DeviceError as e:
            raise HTTPException(status_code=e.status, detail=e.message) from e

    @app.get(f"{ROUTE}/devices")
    async def list_devices():
        listing = await asyncio.to_thread(devices.listing)
        return listing.model_dump(mode="json")

    @app.post(f"{ROUTE}/devices/{{device_id}}/mount")
    async def mount(device_id: str, changes: Optional[DeviceChanges] = None):
        result = await _carried_out(devices.mount(device_id, changes))
        return result.model_dump(mode="json")

    @app.post(f"{ROUTE}/devices/{{device_id}}/unmount")
    async def unmount(device_id: str):
        result = await _carried_out(devices.unmount(device_id))
        return result.model_dump(mode="json")

    @app.put(f"{ROUTE}/devices/{{device_id}}")
    async def update(device_id: str, changes: DeviceChanges):
        await _carried_out(devices.update(device_id, changes))
        return {"message": "Ok"}

    @app.delete(f"{ROUTE}/devices/{{device_id}}/failure")
    async def dismiss(device_id: str):
        await _carried_out(devices.dismiss(device_id))
        return {"message": "Ok"}

    @app.delete(f"{ROUTE}/devices/{{device_id}}")
    async def forget(device_id: str):
        await _carried_out(devices.forget(device_id))
        return {"message": "Ok"}

    @app.post(f"{ROUTE}/shares/probe")
    async def probe(request: ProbeRequest):
        answer = await _carried_out(asyncio.to_thread(devices.probe, request.host))
        return {
            "host": answer.host,
            "reachable": answer.reachable,
            "error": answer.error,
            "exports": [
                {
                    "id": nfs_id(export.host, export.path),
                    "host": export.host,
                    "export": export.path,
                    "server": export.server or None,
                    "clients": list(export.clients),
                }
                for export in answer.exports
            ],
        }

    @app.post(f"{ROUTE}/shares/scan")
    async def rescan():
        devices.rescan()
        return {"message": "Ok"}

    @app.post(f"{ROUTE}/shares")
    async def add_share(share: NewShare):
        result = await _carried_out(devices.add_share(share))
        return result.model_dump(mode="json")
