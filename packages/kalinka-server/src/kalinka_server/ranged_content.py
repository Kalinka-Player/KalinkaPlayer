"""Answering byte-range requests for content only a module can read.

A file gets served by ``FileResponse``, which does its own range handling.
Content behind a module — a share it speaks to itself, an object store — has
to be streamed through this process instead, and a renderer will not play it
unless the same three guarantees hold: ``Accept-Ranges`` on every reply, a
``206`` carrying ``Content-Range``, and the total length after the slash in
it, which is where the renderer reads the stream size from.

The reader is blocking, so every touch of it goes to a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, BinaryIO, Callable, Optional

from starlette.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

#: Read size per hop to a worker thread. Large enough that a track is a few
#: hundred hops rather than thousands, small enough to stay off the heap.
_CHUNK_BYTES = 256 * 1024


class UnsatisfiableRange(Exception):
    """The request asked for bytes the asset does not have."""


def resolve_range(
    header: Optional[str], size: int
) -> Optional[tuple[int, int]]:
    """The inclusive byte span a ``Range`` header asks for, or None.

    None means the header is not one to honour — absent, in units this
    server does not speak, or asking for several spans at once — and the
    whole asset is the answer. A multipart reply is not something a renderer
    ever asks for, and ignoring an unhonourable range is what the HTTP
    specification calls for.

    @raise UnsatisfiableRange If the span lies outside the asset.
    """
    if not header:
        return None

    units, _, spec = header.partition("=")
    if units.strip().lower() != "bytes" or "," in spec:
        return None

    first, separator, last = spec.strip().partition("-")
    if not separator:
        return None

    if not first:
        # A suffix range: the last N bytes, clamped to the whole asset.
        if not last.isdecimal():
            return None
        requested = int(last)
        if requested == 0 or size <= 0:
            raise UnsatisfiableRange(header)
        return max(0, size - requested), size - 1

    if not first.isdecimal() or (last and not last.isdecimal()):
        return None
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        raise UnsatisfiableRange(header)
    return start, min(end, size - 1)


def unsatisfiable_response(size: int) -> Response:
    """The ``416`` a bad range earns, naming the length that would satisfy
    one."""
    return Response(
        status_code=416,
        headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
    )


def head_response(
    span: Optional[tuple[int, int]], size: int, mime_type: str
) -> Response:
    """What a GET would answer, without the body.

    Built from the same headers so a client cannot learn one thing from HEAD
    and another from GET.
    """
    status, headers = _headers(span, size)
    return Response(status_code=status, media_type=mime_type, headers=headers)


def stream_response(
    reader: BinaryIO,
    *,
    span: Optional[tuple[int, int]],
    size: int,
    mime_type: str,
) -> StreamingResponse:
    """Stream ``reader``, either whole or over the span that was asked for.

    @param reader An open, seekable stream. Closed when the response ends,
        including when the client goes away mid-track.
    @param span The honoured range, or None to serve the whole asset.
    """
    status, headers = _headers(span, size)
    start, end = span if span is not None else (0, size - 1)
    return _ClosingStreamingResponse(
        reader,
        _chunks(reader, start, end - start + 1),
        status_code=status,
        media_type=mime_type,
        headers=headers,
    )


class _ClosingStreamingResponse(StreamingResponse):
    """A streaming response that owns the stream it was built from.

    The obvious place to close it is a ``finally`` inside the body generator,
    and that does not work: a client going away mid-track cancels the
    consumer, leaving the generator suspended at its ``yield`` until the loop
    finalizes async generators at shutdown. Closing here instead runs on
    every exit, and runs synchronously, so cancellation cannot interrupt it
    and strand a handle on the storage.
    """

    def __init__(self, reader: BinaryIO, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._reader = reader

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            _close(self._reader)


def _headers(
    span: Optional[tuple[int, int]], size: int
) -> tuple[int, dict[str, str]]:
    """Status and headers for one answer. ``Accept-Ranges`` goes on every
    one of them: without it a renderer refuses to seek at all."""
    if span is None:
        return 200, {"Accept-Ranges": "bytes", "Content-Length": str(size)}
    start, end = span
    return 206, {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Range": f"bytes {start}-{end}/{size}",
    }


async def _chunks(reader: BinaryIO, start: int, length: int) -> AsyncIterator[bytes]:
    """Reads the span. The stream's lifetime belongs to the response, which
    is the only place that sees every way one can end."""
    if start:
        await asyncio.to_thread(reader.seek, start)
    remaining = length
    while remaining > 0:
        chunk = await asyncio.to_thread(reader.read, min(_CHUNK_BYTES, remaining))
        if not chunk:
            # The asset shrank under us. Stopping short is all that is left;
            # the length already went out in the headers.
            logger.warning("Content ended %d bytes early", remaining)
            break
        remaining -= len(chunk)
        yield chunk


def _close(reader: BinaryIO) -> None:
    try:
        reader.close()
    except OSError as e:
        logger.debug("Closing the content stream failed: %s", e)


async def open_reader(
    open_stream: Callable[[], BinaryIO], timeout_s: float
) -> BinaryIO:
    """Open the asset off the event loop, bounded.

    A stream nobody is left to receive is closed rather than leaked: the
    worker thread stays blocked on storage that stopped answering, and the
    open can still succeed after the waiter is gone.

    @note The cleanup catches ``BaseException`` because the waiter is lost to
        cancellation as often as to the timeout — on shutdown, or when a
        renderer abandons a range request to seek — and ``CancelledError`` is
        not an ``Exception``. Attaching the callback also retrieves the
        exception of an open that failed on its own, which is otherwise
        reported as never retrieved.

    @raise TimeoutError If the storage did not answer in time, which the
        caller reports as transient rather than as a missing asset.
    """
    opening = asyncio.ensure_future(asyncio.to_thread(open_stream))
    try:
        return await asyncio.wait_for(asyncio.shield(opening), timeout_s)
    except BaseException:
        opening.add_done_callback(_close_if_opened)
        raise


def _close_if_opened(opening: "asyncio.Future[BinaryIO]") -> None:
    if not opening.cancelled() and opening.exception() is None:
        _close(opening.result())
