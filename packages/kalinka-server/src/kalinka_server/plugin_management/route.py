"""Read-only plugin inventory. Missing trust is never reported as up to date."""

from typing import Callable

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .catalog import PublicCatalog
from .compatibility import CatalogCompatibility
from .inventory import PluginInventory

_HEADERS = {"Cache-Control": "no-store"}


def register_plugin_routes(
    app: FastAPI,
    inventory: PluginInventory,
    public_catalog: PublicCatalog | None = None,
    *,
    enabled: Callable[[], bool] = lambda: False,
    compatibility: CatalogCompatibility | None = None,
    renderers: Callable[[], tuple[dict, ...]] = lambda: (),
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
        return JSONResponse(result, headers=_HEADERS)

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
    def updates():
        return JSONResponse(
            headers=_HEADERS,
            content={
                "status": "unavailable",
                "reason": "signature_verification_not_configured",
                "updates": [],
                "automatic_updates_enabled": False,
            },
        )

    app.include_router(router)
