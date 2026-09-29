"""HTTP for explicit in-progress resources, without a fabricated length."""

import asyncio
import re

from fastapi import Request
from starlette.responses import Response, StreamingResponse
from starlette.requests import ClientDisconnect
from kalinka_plugin_sdk.live_content import LiveContent, LiveContentError
from . import ranged_content


class LiveResponse(StreamingResponse):
    def __init__(self, reader, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reader = reader

    async def __call__(self, scope, receive, send):
        async def send_to_client(message):
            try:
                await send(message)
            except OSError as exc:
                # A closed socket is expected; an OSError from reader.read
                # remains a source failure and must still propagate.
                raise ClientDisconnect() from exc

        # Always watch disconnects, including ASGI 2.4: an unfinished reader
        # may be waiting for audio and never reach send() to notice the socket.
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
    headers = {"Cache-Control": "no-store", "Accept-Ranges": "none"}
    start, end, status = 0, size, 200
    if size is None:
        headers["X-Kalinka-Live"] = "1"
        # The renderer probes with a bounded first chunk (e.g. bytes=0-383999).
        # Ignore either form of initial range and send the whole live response.
        # A nonzero reconnect must not silently replay the beginning.
        requested_range = request.headers.get("range")
        if requested_range is not None and not re.fullmatch(r"bytes=0-[0-9]*", requested_range):
            return Response(status_code=409, headers=headers)
    else:
        headers.update({"Accept-Ranges": "bytes", "Content-Length": str(size)})
        try:
            span = ranged_content.resolve_range(request.headers.get("range"), size)
        except ranged_content.UnsatisfiableRange:
            response = ranged_content.unsatisfiable_response(size)
            response.headers["Cache-Control"] = "no-store"
            return response
        if span is not None:
            start, last = span
            end, status = last + 1, 206
            headers["Content-Range"] = f"bytes {start}-{last}/{size}"
            headers["Content-Length"] = str(end - start)
    if request.method == "HEAD":
        response = Response(status_code=status, headers=headers, media_type=mime_type)
        if size is None:
            del response.headers["content-length"]
        return response
    try:
        reader = await content.open(start, end)
    except LiveContentError as exc:
        return Response(status_code=exc.status, headers={"Cache-Control": "no-store"})

    async def chunks():
        while data := await reader.read(64 * 1024):
            yield data

    return LiveResponse(reader, chunks(), status_code=status, media_type=mime_type,
                        headers=headers)
