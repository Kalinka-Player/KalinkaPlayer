"""Plugin inventory and the install/uninstall requests the restart service carries out.

Missing trust is never reported as up to date; an accepted operation is a
restart, not a result.
"""

from typing import Callable

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .catalog import PublicCatalog
from .compatibility import CatalogCompatibility
from .inventory import PluginInventory
from .operations import PluginOperations, Refusal

_HEADERS = {"Cache-Control": "no-store"}


def register_plugin_routes(
    app: FastAPI,
    inventory: PluginInventory,
    public_catalog: PublicCatalog | None = None,
    *,
    enabled: Callable[[], bool] = lambda: False,
    compatibility: CatalogCompatibility | None = None,
    renderers: Callable[[], tuple[dict, ...]] = lambda: (),
    operations: PluginOperations | None = None,
) -> None:
    def require_enabled():
        if not enabled():
            raise HTTPException(
                403,
                detail={
                    "code": "plugin_catalog_disabled",
                    "message": "Enable Plugin catalog preview in General expert settings.",
                },
            )

    router = APIRouter(
        prefix="/server/plugins",
        tags=["plugins"],
        dependencies=[Depends(require_enabled)],
    )
    public_catalog = public_catalog if public_catalog is not None else PublicCatalog("")
    compatibility = (
        compatibility if compatibility is not None else CatalogCompatibility()
    )

    @router.get("")
    def installed_plugins():
        # A sync route runs metadata I/O in FastAPI's thread pool.
        result = inventory.snapshot()
        result["capabilities"]["enabled"] = True
        if operations is not None:
            result["capabilities"].update(operations.capabilities())
            result["last_operation"] = operations.last_result()
        return JSONResponse(result, headers=_HEADERS)

    if operations is not None:

        async def operate(action, plugin_id: str):
            try:
                body = await operations.request(action, plugin_id)
            except Refusal as refusal:
                raise HTTPException(
                    refusal.status,
                    detail={"code": refusal.code, "message": refusal.message},
                ) from None
            return JSONResponse(body, status_code=202, headers=_HEADERS)

        @router.post("/{plugin_id}/install", status_code=202)
        async def install_plugin(plugin_id: str):
            return await operate("install", plugin_id)

        @router.post("/{plugin_id}/update", status_code=202)
        async def update_plugin(plugin_id: str):
            return await operate("update", plugin_id)

        @router.post("/{plugin_id}/uninstall", status_code=202)
        async def uninstall_plugin(plugin_id: str):
            return await operate("uninstall", plugin_id)

    @router.get("/catalog")
    async def catalog():
        result = public_catalog.snapshot()
        available = result["status"] in {"available", "stale"}
        if available:
            result = await run_in_threadpool(
                compatibility.annotate, result, renderers()
            )
            require_enabled()
        else:
            result["code"] = "catalog_unavailable"
        return JSONResponse(
            status_code=200 if available else 503,
            headers=_HEADERS,
            content=result,
        )

    @router.get("/updates")
    async def updates():
        if operations is None:
            content = {
                "status": "unavailable",
                "reason": "signature_verification_not_configured",
                "updates": [],
                "automatic_updates_enabled": False,
            }
        else:
            content = await operations.updates()
        return JSONResponse(headers=_HEADERS, content=content)

    app.include_router(router)
