"""Serving a module's content on its behalf.

Every content link points at this server rather than at the module's own
backend, so a fetcher only ever has to reach the address it is already talking
to. :mod:`content_urls` mints those links.

A renderer seeks by asking for byte ranges and reads the stream size out of
``Content-Range``. A module's file goes to FileResponse, which answers those
for itself; content only the module can reach is streamed through
:mod:`ranged_content`, which answers them the same way.
"""

import asyncio
import logging
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.responses import Response

from kalinka_plugin_sdk.inputmodule import (
    ContentInfo,
    InputModule,
    SourceUnavailableError,
)

from . import ranged_content
from .content_urls import CONTENT_ROUTE

logger = logging.getLogger(__name__)

# A stat on a hung network mount never returns; the module's own checks are
# bounded, and this last one before FileResponse must be too.
_STAT_TIMEOUT_S = 3.0


def register_content_route(
    app: FastAPI, resolve_module: Callable[[str], InputModule]
) -> None:
    """Mount the content endpoint on `app`.

    ``resolve_module`` maps a module name to the input module, raising for one
    that is unknown or disabled.
    """

    # HEAD as well as GET: FastAPI does not answer it off a GET route the way a
    # mount does, and a client may ask for the size and type alone.
    # `:path` because an asset id is opaque and may hold a slash: the %2F the
    # link carries is decoded before the route is matched, and a single-segment
    # converter would not match what the module actually minted.
    @app.api_route(
        f"{CONTENT_ROUTE}/{{module_name}}/{{asset_id:path}}", methods=["GET", "HEAD"]
    )
    async def get_content(module_name: str, asset_id: str, request: Request):
        # An unmounted share answers 503, not 404: renderers retry 5xx but
        # treat 4xx as fatal.
        try:
            info = await resolve_module(module_name).get_content_info(asset_id)
        except SourceUnavailableError as e:
            raise HTTPException(
                status_code=503, detail=str(e), headers={"Retry-After": "2"}
            )
        if info is None:
            raise HTTPException(status_code=404, detail="Content not found")
        if not info.local_path:
            return await _serve_stream(info, module_name, asset_id, request)

        # A file that went away since the module named it is absent rather
        # than a server fault — FileResponse would raise on the missing stat.
        try:
            present = await asyncio.wait_for(
                asyncio.to_thread(Path(info.local_path).is_file), _STAT_TIMEOUT_S
            )
        except (asyncio.TimeoutError, TimeoutError):
            raise HTTPException(
                status_code=503,
                detail="Content storage did not respond",
                headers={"Retry-After": "2"},
            )
        if not present:
            raise HTTPException(status_code=404, detail="Content not found")

        return FileResponse(
            info.local_path,
            media_type=info.mime_type,
            headers={"Accept-Ranges": "bytes"},
        )


async def _serve_stream(
    info: ContentInfo, module_name: str, asset_id: str, request: Request
) -> Response:
    """Serve content the module alone can read, ranges included.

    A module that offers neither a file nor a readable stream — or a stream
    without a length, which a renderer cannot seek in — has nothing servable
    to give, so the asset reads as absent.
    """
    if info.reader is None or info.size is None:
        logger.warning(
            "%s named asset %s but gave neither a file nor a sized reader",
            module_name,
            asset_id,
        )
        raise HTTPException(status_code=404, detail="Content not found")

    try:
        span = ranged_content.resolve_range(
            request.headers.get("range"), info.size
        )
    except ranged_content.UnsatisfiableRange:
        return ranged_content.unsatisfiable_response(info.size)

    if request.method == "HEAD":
        return ranged_content.head_response(span, info.size, info.mime_type)

    try:
        reader = await ranged_content.open_reader(info.reader, _STAT_TIMEOUT_S)
    except (asyncio.TimeoutError, TimeoutError):
        raise HTTPException(
            status_code=503,
            detail="Content storage did not respond",
            headers={"Retry-After": "2"},
        )
    except FileNotFoundError:
        logger.warning(
            "Asset %s of %s went away between measuring and reading it",
            asset_id,
            module_name,
        )
        raise HTTPException(status_code=404, detail="Content not found")
    except OSError as e:
        # The asset was there a moment ago, so this is the storage failing,
        # not the asset missing. A renderer retries a 5xx and abandons the
        # track on a 4xx, and a dropped share session is worth retrying.
        logger.warning("Could not open asset %s of %s: %s", asset_id, module_name, e)
        raise HTTPException(
            status_code=503,
            detail="Content storage is not readable",
            headers={"Retry-After": "2"},
        )

    return ranged_content.stream_response(
        reader, span=span, size=info.size, mime_type=info.mime_type
    )
