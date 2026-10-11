"""A bounded, file-backed LiveContent for audio still being produced (SDK 3.11)."""

import asyncio
import os
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO, Optional

from .live_content import LiveContentError

_FILE_BYTES = 1024 * 1024


async def _disk_call(function, *args):
    # A cancelled caller must not close the descriptor under a running worker.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return task.result()


class LiveCaptureError(RuntimeError):
    """The capture can take no more bytes; the message is fit to show."""


class LiveCapture:
    """
    @brief One stream a plugin produces while the renderer plays it.

    The producer appends bytes and finishes or retires the capture; the
    server opens readers on it through the LiveContent protocol. Bytes live
    in anonymous temporary files. By default they stay until the capture is
    retired and its last reader closed, so a reader never sees them vanish
    under it; a capture that keeps no delivered bytes lets them go as its
    readers pass them.

    Only the first open of an unfinished capture may read, from byte 0: the
    stream is sequential, and a reconnect must be served a new capture.

    @note Not thread-safe: use from the event loop that created it.
    """

    def __init__(
        self,
        directory: Optional[Path] = None,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        max_readers: int = 2,
        read_timeout_s: float = 1800,
        keep_delivered: bool = True,
    ):
        """
        @param directory Where the temporary files go; the system's default when None.
        @param max_bytes What the capture may hold before append() fails it.
        @param read_timeout_s How long a read waits for bytes before it fails.
        @param keep_delivered False for a stream with no end in sight, such as
            a receiver's: bytes go once read, and max_bytes then bounds only
            how far the producer runs ahead of the reader.
        """
        self.id = uuid.uuid4().hex
        self.max_bytes = max_bytes
        self.max_readers = max_readers
        self.read_timeout_s = read_timeout_s
        self.keep_delivered = keep_delivered
        self.available = 0
        # The furthest byte any reader has been handed.
        self.delivered = 0
        self.complete = False
        self.failure: Optional[str] = None
        self.retired = False
        self._cancelled = False
        self._opened = False
        self._closed = False
        self._readers: set[LiveCaptureReader] = set()
        self._directory = directory
        # Byte n lives in file n // _FILE_BYTES; files before _first_file are gone.
        self._files: dict[int, BinaryIO] = {}
        self._first_file = 0
        self._condition = asyncio.Condition()

    @property
    def size(self) -> Optional[int]:
        return self.available if self.complete else None

    @property
    def readers(self) -> int:
        return len(self._readers)

    @property
    def closed(self) -> bool:
        """Retired with every reader gone: its bytes are released."""
        return self._closed

    @property
    def _held(self) -> int:
        return self.available - (0 if self.keep_delivered else self.delivered)

    async def append(self, data: bytes) -> None:
        """Add bytes for readers to take; raises LiveCaptureError once over the limit."""
        async with self._condition:
            if self.retired or self._cancelled or self.failure or self.complete:
                raise LiveCaptureError("The stream is no longer being written")
            if self._held + len(data) > self.max_bytes:
                self._fail("The stream outgrew its buffer")
            try:
                await self._write(memoryview(data))
            except OSError:
                self._fail("The stream's buffer could not be written")
            self._condition.notify_all()

    async def _write(self, data: memoryview) -> None:
        while data:
            index, offset = divmod(self.available, _FILE_BYTES)
            part = data[: _FILE_BYTES - offset]
            file = self._files.get(index)
            if file is None:
                file = self._files[index] = tempfile.TemporaryFile(dir=self._directory)
            if await _disk_call(os.pwrite, file.fileno(), part, offset) != len(part):
                raise OSError("short write")
            self.available += len(part)
            data = data[len(part) :]

    def _fail(self, failure: str):
        self.failure = failure
        self._condition.notify_all()
        raise LiveCaptureError(failure)

    async def finish(self) -> None:
        """No more bytes will come: readers reach the end instead of waiting."""
        async with self._condition:
            self.complete = True
            self._condition.notify_all()

    async def retire(
        self, failure: Optional[str] = None, *, wait_for_disconnect: bool = False
    ) -> None:
        """Stop serving the capture; its file goes once the last reader closes.

        @param failure Why, when it failed; readers then fail with it.
        @param wait_for_disconnect Let open readers take the bytes already
            captured, then reach the end if the capture finished or wait for
            their client to hang up if not, rather than failing them: the
            renderer then reports no error for a stream the plugin chose to end.
        """
        async with self._condition:
            self.retired = True
            self._cancelled |= not wait_for_disconnect or failure is not None
            self.failure = failure or self.failure
            self._condition.notify_all()
            self._close_if_unused()

    async def wait_closed(self) -> None:
        """Until the file is gone: retired, and every reader closed."""
        async with self._condition:
            await self._condition.wait_for(lambda: self.closed)

    async def open(self, start: int, end: Optional[int]) -> "LiveCaptureReader":
        async with self._condition:
            if self.retired or self.failure:
                raise LiveContentError(410, "The stream has ended")
            if len(self._readers) >= self.max_readers:
                raise LiveContentError(429, "Too many readers of the stream")
            if not self.complete and (start != 0 or self._opened):
                raise LiveContentError(
                    409, "The stream plays once, from its start; start a new one"
                )
            if start < self._first_file * _FILE_BYTES or start > self.available:
                raise LiveContentError(409, "Those bytes are not available")
            reader = LiveCaptureReader(self, start, end)
            self._readers.add(reader)
            self._opened = True
            self._condition.notify_all()
            return reader

    def _let_go_of_read_bytes(self):
        if self.keep_delivered or not self._readers:
            return
        last_needed = min(r.position for r in self._readers) // _FILE_BYTES
        while self._first_file < last_needed:
            file = self._files.pop(self._first_file, None)
            if file is not None:
                file.close()
            self._first_file += 1

    def _close_if_unused(self):
        if self.retired and not self._readers:
            for file in self._files.values():
                file.close()
            self._files.clear()
            self._closed = True


class LiveCaptureReader:
    """One reader of a LiveCapture, from a byte offset up to an exclusive end."""

    def __init__(self, capture: LiveCapture, start: int, end: Optional[int]):
        self._capture = capture
        self._position = start
        self._end = end
        self._closed = False

    @property
    def position(self) -> int:
        return self._position

    def _ready(self) -> bool:
        c = self._capture
        return (
            self._closed
            or c._cancelled
            or c.failure is not None
            or c.complete
            or self._position < c.available
        )

    async def read(self, count: int) -> bytes:
        c = self._capture
        async with c._condition:
            try:
                await asyncio.wait_for(c._condition.wait_for(self._ready), c.read_timeout_s)
            except TimeoutError:
                raise OSError("The stream produced nothing in time") from None
            if self._closed or c._cancelled or c.failure:
                raise OSError(c.failure or "The stream was cancelled")
            remaining = c.available - self._position
            if self._end is not None:
                remaining = min(remaining, self._end - self._position)
            if remaining <= 0:
                return b""
            index, offset = divmod(self._position, _FILE_BYTES)
            count = min(count, remaining, _FILE_BYTES - offset)
            data = await _disk_call(os.pread, c._files[index].fileno(), count, offset)
            if not data:
                raise OSError("The stream's buffer was truncated")
            self._position += len(data)
            c.delivered = max(c.delivered, self._position)
            c._let_go_of_read_bytes()
            return data

    async def aclose(self) -> None:
        c = self._capture
        async with c._condition:
            self._closed = True
            c._readers.discard(self)
            c._condition.notify_all()
            c._close_if_unused()
