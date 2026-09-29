"""Asynchronous content whose final length is not yet known (SDK 3.5)."""

from typing import Optional, Protocol, runtime_checkable


class LiveContentError(Exception):
    """An explicit refusal before HTTP headers are sent."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class LiveReader(Protocol):
    async def read(self, count: int) -> bytes:
        """Wait for bytes; b'' means genuine completion, never a temporary gap."""
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class LiveContent(Protocol):
    @property
    def size(self) -> Optional[int]:
        """Final, immutable length, or None while being captured."""
        ...

    async def open(self, start: int, end: Optional[int]) -> LiveReader:
        """Pin a reader at a byte offset; reject evicted/unavailable bytes.

        end is exclusive. Implementations must bound waits, storage and reader
        count, wake on cancellation, and retain pinned data until aclose. The
        server gives up on an open or read that produces nothing for 30
        minutes.
        """
        ...
