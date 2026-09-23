"""``/server/logs/export``: one log export per server, as a single resource.

Every answer carries ``Cache-Control: no-store``, refusals included, and an
error is a stable ``code`` with a message fit to show the user — never an
exception's text or a local path.
"""

from typing import Any, Callable, Coroutine, Optional

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from .log_export_service import (
    ArchiveCheckout,
    ExportInProgress,
    ExportManager,
    ExportNotReady,
    ExportStatus,
    LogSourceUnavailable,
)

EXPORT_ROUTE = "/server/logs/export"

_NO_STORE = {"Cache-Control": "no-store"}


class ExportRequest(BaseModel):
    """The body of ``POST /server/logs/export``; an unknown field is refused."""

    model_config = ConfigDict(extra="forbid")

    lookback_seconds: StrictInt = Field(ge=60, le=604800)
    include_local_renderer: StrictBool = False


class _NoStoreRoute(APIRoute):
    """Marks every answer uncacheable, and gives a refused body the same
    ``code``/``message`` shape as every other refusal here."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def no_store(request: Request) -> Response:
            try:
                response = await handle(request)
            except RequestValidationError:
                response = _error(422, "invalid_request", "The request is not valid.")
            response.headers.update(_NO_STORE)
            return response

        return no_store


def _error(status: int, code: str, message: str, export: Optional[ExportStatus] = None) -> JSONResponse:
    body: dict = {"code": code, "message": message}
    if export is not None:
        body["export"] = export.to_json()
    return JSONResponse(status_code=status, content=body)


class _CheckedOutFileResponse(FileResponse):
    """FileResponse over a checked-out archive, released however the
    transfer ends — a client that hangs up included."""

    def __init__(self, checkout: ArchiveCheckout) -> None:
        super().__init__(
            checkout.path,
            media_type="application/zip",
            filename=checkout.filename,
            headers={"X-Content-Type-Options": "nosniff", **_NO_STORE},
        )
        self._checkout = checkout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._checkout.release()


def register_log_export_routes(app: FastAPI, manager: ExportManager) -> None:
    """Mount the export's routes on ``app``; before any catch-all mount."""
    router = APIRouter(route_class=_NoStoreRoute)

    @router.get(EXPORT_ROUTE)
    async def export_status():
        return JSONResponse(manager.status().to_json())

    @router.post(EXPORT_ROUTE)
    async def export_start(request: ExportRequest):
        try:
            status = await manager.start(
                request.lookback_seconds, request.include_local_renderer
            )
        except ExportInProgress as e:
            return _error(
                409, "export_in_progress", "Logs are already being collected.", e.status
            )
        except LogSourceUnavailable as e:
            if e.required:
                return _error(
                    503,
                    "log_source_unavailable",
                    "This server cannot read its logs; its log reader is not installed.",
                )
            return _error(
                422, "invalid_request", "This server has no renderer logs to include."
            )
        return JSONResponse(status_code=202, content=status.to_json())

    @router.delete(EXPORT_ROUTE, status_code=204)
    async def export_withdraw():
        await manager.withdraw()
        return Response(status_code=204)

    # HEAD alongside GET, as for content: a client may ask for the size alone.
    @router.api_route(f"{EXPORT_ROUTE}/download", methods=["GET", "HEAD"])
    async def export_download():
        try:
            checkout = manager.checkout()
        except ExportNotReady as e:
            return _error(409, "export_not_ready", "No log archive is ready.", e.status)
        return _CheckedOutFileResponse(checkout)

    app.include_router(router)
