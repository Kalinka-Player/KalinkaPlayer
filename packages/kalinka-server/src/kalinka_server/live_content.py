"""HTTP for explicit in-progress resources, without a fabricated length."""

import asyncio
import logging
import re

from fastapi import Request
from starlette.responses import Response, StreamingResponse
from starlette.requests import ClientDisconnect
from kalinka_plugin_sdk.live_content import LiveContent, LiveContentError, LiveReader
from . import ranged_content

logger = logging.getLogger(__name__)

#: Longest a live producer may stay silent before its response is abandoned.
IDLE_LIMIT_S = 30 * 60

_CHUNK_BYTES = 64 * 1024
_NO_STORE = {"Cache-Control": "no-store"}
# The renderer's first request is a bounded probe from byte zero.
_INITIAL_RANGE = re.compile(r"bytes=0-[0-9]*")


class LiveResponse(StreamingResponse):
    """A streaming response that owns a live reader.

    The reader is closed on every exit. The response also ends as soon as
    the client disconnects, even while the reader is still waiting for
    bytes and so never reaches a send that would notice the closed socket.
    """

    def __init__(self, reader: LiveReader, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reader = reader

    async def __call__(self, scope, receive, send):
        async def send_to_client(message):
            try:
                await send(message)
            except OSError as exc:
                # Only the socket's OSError is a disconnect; the reader's is a fault.
                raise ClientDisconnect() from exc

        streaming = asyncio.create_task(self.stream_response(send_to_client))
        disconnected = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            done, _ = await asyncio.wait(
                (streaming, disconnected), return_when=asyncio.FIRST_COMPLETED
            )
            if streaming in done:
                try:
                    await streaming
                except ClientDisconnect:
                    pass
            else:
                await disconnected
        finally:
            streaming.cancel()
            disconnected.cancel()
            try:
                await asyncio.gather(streaming, disconnected, return_exceptions=True)
            finally:
                await asyncio.shield(self.reader.aclose())
        if self.background is not None:
            await self.background()


async def serve(content: LiveContent, mime_type: str, request: Request) -> Response:
    size = content.size
    if size is None:
        headers = {**_NO_STORE, "Accept-Ranges": "none", "X-Kalinka-Live": "1"}
        requested = request.headers.get("range")
        # A nonzero reconnect must not silently replay the beginning.
        if requested is not None and not _INITIAL_RANGE.fullmatch(requested):
            return Response(status_code=409, headers=headers)
        status, start, end = 200, 0, None
    else:
        try:
            span = ranged_content.resolve_range(request.headers.get("range"), size)
        except ranged_content.UnsatisfiableRange:
            response = ranged_content.unsatisfiable_response(size)
            response.headers.update(_NO_STORE)
            return response
        status, headers = ranged_content.range_headers(span, size)
        headers.update(_NO_STORE)
        start, end = (span[0], span[1] + 1) if span is not None else (0, size)

    if request.method == "HEAD":
        response = Response(status_code=status, headers=headers, media_type=mime_type)
        if size is None:
            del response.headers["content-length"]
        return response
    try:
        reader = await asyncio.wait_for(content.open(start, end), IDLE_LIMIT_S)
    except LiveContentError as exc:
        return Response(status_code=exc.status, headers=_NO_STORE)
    except asyncio.TimeoutError:
        logger.warning("Live content had nothing to open for %d s", IDLE_LIMIT_S)
        return Response(status_code=504, headers=_NO_STORE)
    return LiveResponse(
        reader, _chunks(reader), status_code=status, media_type=mime_type,
        headers=headers,
    )


async def _chunks(reader: LiveReader):
    while True:
        try:
            data = await asyncio.wait_for(reader.read(_CHUNK_BYTES), IDLE_LIMIT_S)
        except asyncio.TimeoutError:
            logger.warning("Live content produced nothing for %d s; abandoning it", IDLE_LIMIT_S)
            raise
        if not data:
            return
        yield data
