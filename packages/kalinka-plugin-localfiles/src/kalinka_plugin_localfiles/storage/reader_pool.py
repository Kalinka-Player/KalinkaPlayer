"""Files kept open between the ranges a renderer asks for.

A renderer plays a track as a chain of range requests of a few hundred
kilobytes, each sent once the one before has arrived. Over a share, opening
the file for every one of them is a round trip, and so are the folder probe
and the measurement made before it: DSD128 gives a 384,000-byte range about
270 ms, and those trips spent most of it. Kept open, a track pays for them
once.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, wait
from dataclasses import dataclass
from typing import BinaryIO, Callable, Optional, TypeVar

logger = logging.getLogger(__name__.split(".")[-1])

#: How long a file waits for its next range before it is closed. A playing
#: renderer asks again within seconds; a paused one should not hold a handle
#: on the NAS for longer than this.
IDLE_S = 30.0

#: How long a measured length is vouched for, however busy the track is kept.
#: Past it the next range checks the folder and measures the file again, so
#: a file replaced in place is served at its new length within minutes.
REMEASURE_S = 120.0

#: Files kept waiting at once: the track playing and the one queued after
#: it, for a couple of renderers.
CAPACITY = 4

_T = TypeVar("_T")


def _after(delay_s: float, callback: Callable[[], None]) -> None:
    timer = threading.Timer(delay_s, callback)
    timer.name = "stream-readers"
    timer.daemon = True
    timer.start()


def _in_thread(work: Callable[[], _T]) -> "Future[_T]":
    """``work`` run on a thread of its own, which the waiter may give up on."""
    outcome: Future = Future()

    def run() -> None:
        try:
            outcome.set_result(work())
        except BaseException as e:
            outcome.set_exception(e)

    threading.Thread(target=run, name="stream-reopen", daemon=True).start()
    return outcome


def _close_quietly(handle: BinaryIO) -> None:
    try:
        handle.close()
    except Exception as e:
        logger.debug("Closing a kept reader failed: %s", e)


def _close_if_opened(opening: "Future[BinaryIO]") -> None:
    if opening.exception() is None:
        _close_quietly(opening.result())


@dataclass(eq=False)
class _Kept:
    """What is kept for one file: its length and when it was measured, the
    reader waiting for the next range, and how many are out with a
    response."""

    size: int
    measured_at: float
    lent: int = 0
    idle: Optional[BinaryIO] = None
    returned_at: float = 0.0


class ReaderPool:
    """Readers lent to one response at a time and kept open between them.

    A reader is never shared: a range that arrives while another of the same
    file is still streaming — a renderer seeking — gets a reader of its own,
    and of the two that come back one is kept. Closing what :meth:`lend`
    returned gives the reader back, once any read still running on it has
    finished.

    @note Thread-safe: lending, giving back and the idle sweep all happen on
        worker threads. Opening and closing, which are round trips on a
        share, happen outside the lock.
    @param open_file Opens a file for reading, as the storage does.
    @param reopen_timeout_s How long a reader that waited here gets to open
        its file again after a failed read (see :class:`_Lease`).
    @param clock Monotonic seconds; injected so a test can move time on.
    @param schedule Runs a callback once a delay has passed, on a thread of
        its own.
    """

    def __init__(
        self,
        open_file: Callable[[str], BinaryIO],
        *,
        reopen_timeout_s: float,
        idle_s: float = IDLE_S,
        remeasure_s: float = REMEASURE_S,
        capacity: int = CAPACITY,
        clock: Callable[[], float] = time.monotonic,
        schedule: Callable[[float, Callable[[], None]], None] = _after,
    ) -> None:
        self._open_file = open_file
        self._reopen_timeout_s = reopen_timeout_s
        self._idle_s = idle_s
        self._remeasure_s = remeasure_s
        self._capacity = capacity
        self._clock = clock
        self._schedule = schedule
        self._kept: OrderedDict[str, _Kept] = OrderedDict()
        self._lock = threading.Lock()
        self._sweep_pending = False
        self._closed = False

    def size_of(self, path: str) -> Optional[int]:
        """The length measured for ``path`` while a reader on it is kept or
        lent out and the measurement is under :data:`REMEASURE_S` old, or
        None. What spares the next range measuring it again."""
        now = self._clock()
        with self._lock:
            kept = self._kept.get(path)
            if kept is None or now - kept.measured_at >= self._remeasure_s:
                return None
            return kept.size

    def lend(self, path: str, size: int, *, measured: bool = True) -> BinaryIO:
        """A reader on ``path``, at its first byte, for one response: the one
        kept for it if it is waiting, a new one otherwise.

        @param size The length the caller measured, or had from
            :meth:`size_of`. One that differs from the length kept means the
            file changed, so the reader waiting on the old one is closed
            rather than lent. A measurement :meth:`size_of` no longer offers
            is taken to have been made again.
        @param measured False for a length had from :meth:`size_of`. That
            vouches for nothing new: should the file have been forgotten or
            measured again meanwhile, the reader goes with the response
            rather than starting a new stay here.
        @raise OSError If a new reader cannot be opened.
        """
        now = self._clock()
        outdated: Optional[BinaryIO] = None
        handle: Optional[BinaryIO] = None
        with self._lock:
            kept = None if self._closed else self._kept.get(path)
            if kept is not None and kept.size != size:
                if measured:
                    outdated, kept.idle = kept.idle, None
                kept = None
            if measured and not self._closed:
                if kept is None:
                    kept = self._kept[path] = _Kept(size, measured_at=now)
                elif now - kept.measured_at >= self._remeasure_s:
                    kept.measured_at = now
            if kept is not None:
                kept.lent += 1
                handle, kept.idle = kept.idle, None
        if outdated is not None:
            _close_quietly(outdated)
        if kept is None:
            return self._open(path)
        waited = handle is not None
        if handle is None:
            try:
                handle = self._open(path)
            except BaseException:
                self._give_back(path, kept, None)
                raise
        lease = _Lease(self, path, kept, handle, waited=waited)
        try:
            # A kept reader is wherever the last range left it.
            lease.seek(0)
        except BaseException:
            lease.close()
            raise
        return lease

    def close_idle(self) -> None:
        """Close every reader that has waited the idle period out, and sweep
        again when the next one will have."""
        now = self._clock()
        with self._lock:
            expired = [
                path
                for path, kept in self._kept.items()
                if kept.idle is not None and now - kept.returned_at >= self._idle_s
            ]
            handles = [self._take_idle(path) for path in expired]
            due = [
                kept.returned_at + self._idle_s - now
                for kept in self._kept.values()
                if kept.idle is not None
            ]
            self._sweep_pending = bool(due)
        for path, handle in zip(expired, handles):
            _close_quietly(handle)
            logger.debug(
                "Closed %s, which no range asked for in %.0f s", path, self._idle_s
            )
        if due:
            self._schedule(min(due), self.close_idle)

    def close(self) -> None:
        """Close every reader kept here, and each one lent out as it comes
        back. Nothing is kept from then on: a reader lent afterwards closes
        with its response."""
        with self._lock:
            self._closed = True
            waiting = [
                kept.idle for kept in self._kept.values() if kept.idle is not None
            ]
            self._kept.clear()
        for handle in waiting:
            _close_quietly(handle)

    def _open(self, path: str) -> BinaryIO:
        handle = self._open_file(path)
        logger.debug("Opened %s to stream it", path)
        return handle

    def _reopen(self, path: str, position: int) -> BinaryIO:
        """``path`` opened again at ``position``, given no longer than the
        storage allows. Unbounded, a share that stopped answering would hold
        the response for as long again as the failed read did.

        @raise TimeoutError If it did not open in time. A file that opens
            afterwards is closed then.
        """
        opening = _in_thread(lambda: self._open_at(path, position))
        if not wait([opening], timeout=self._reopen_timeout_s).done:
            opening.add_done_callback(_close_if_opened)
            raise TimeoutError(
                f"{path} did not open again within {self._reopen_timeout_s:.0f} s"
            )
        return opening.result()

    def _open_at(self, path: str, position: int) -> BinaryIO:
        handle = self._open(path)
        try:
            handle.seek(position)
        except BaseException:
            _close_quietly(handle)
            raise
        return handle

    def _give_back(self, path: str, kept: _Kept, handle: Optional[BinaryIO]) -> None:
        """Take a reader back from a response, keeping it for the next range
        if nothing is kept for ``path`` yet. None for one that failed."""
        surplus: list[BinaryIO] = []
        sweep = False
        with self._lock:
            kept.lent -= 1
            current = self._kept.get(path) is kept
            if handle is not None:
                if current and kept.idle is None:
                    kept.idle, kept.returned_at = handle, self._clock()
                    self._kept.move_to_end(path)
                    surplus = self._over_capacity()
                    sweep = not self._sweep_pending
                    self._sweep_pending = True
                else:
                    surplus.append(handle)
            if self._kept.get(path) is kept and not kept.lent and kept.idle is None:
                del self._kept[path]
        for extra in surplus:
            _close_quietly(extra)
        if sweep:
            self._schedule(self._idle_s, self.close_idle)

    def _forget(self, path: str, kept: _Kept) -> None:
        """Drop what is kept for ``path``, so the next range checks, measures
        and opens it as if for the first time."""
        with self._lock:
            if self._kept.get(path) is not kept:
                return
            del self._kept[path]
            handle, kept.idle = kept.idle, None
        if handle is not None:
            _close_quietly(handle)

    def _over_capacity(self) -> list[BinaryIO]:
        waiting = [path for path, kept in self._kept.items() if kept.idle is not None]
        return [
            self._take_idle(path)
            for path in waiting[: max(0, len(waiting) - self._capacity)]
        ]

    def _take_idle(self, path: str) -> BinaryIO:
        kept = self._kept[path]
        handle, kept.idle = kept.idle, None
        if not kept.lent:
            del self._kept[path]
        return handle


class _Lease(io.RawIOBase):
    """A kept reader on loan to one response. Closing it gives it back.

    A reader that waited in the pool can outlive the session it was opened
    on, and a share that dropped an idle connection still has the bytes, so
    the first read that fails on it is made once more on the file opened
    afresh, within the pool's reopen timeout. A reader opened for this
    response gets no second try. Whatever failure gets through, the file is
    forgotten, so the next range checks the share before it is served from
    it.

    @note Every operation holds the lease, and so does closing it. A response
        whose client went away is cancelled while its read is still running
        on a worker thread, and a reader given back then would be sought by
        the next range under that read.
    """

    def __init__(
        self,
        pool: ReaderPool,
        path: str,
        kept: _Kept,
        handle: BinaryIO,
        *,
        waited: bool,
    ) -> None:
        super().__init__()
        self._pool = pool
        self._path = path
        self._kept = kept
        self._handle: Optional[BinaryIO] = handle
        self._waited = waited
        self._position = 0
        self._busy = threading.Lock()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        data = self._retrying(lambda handle: handle.read(size))
        self._position += len(data)
        return data

    def readinto(self, buffer) -> int:
        count = self._retrying(lambda handle: handle.readinto(buffer))
        self._position += count
        return count

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._position = self._retrying(lambda handle: handle.seek(offset, whence))
        return self._position

    def tell(self) -> int:
        return self._position

    def close(self) -> None:
        with self._busy:
            if self.closed:
                return
            handle, self._handle = self._handle, None
            try:
                self._pool._give_back(self._path, self._kept, handle)
            finally:
                super().close()

    def _retrying(self, operation: Callable[[BinaryIO], _T]) -> _T:
        with self._busy:
            if self.closed:
                raise ValueError("I/O operation on closed file")
            try:
                return self._attempt(operation)
            except OSError:
                self._discard()
                self._pool._forget(self._path, self._kept)
                raise

    def _attempt(self, operation: Callable[[BinaryIO], _T]) -> _T:
        if self._handle is None:
            raise OSError(f"{self._path} could not be read")
        try:
            return operation(self._handle)
        except OSError as e:
            if not self._waited:
                raise
            logger.debug("Opening %s again after a failed read: %s", self._path, e)
        self._waited = False
        self._discard()
        self._handle = self._pool._reopen(self._path, self._position)
        return operation(self._handle)

    def _discard(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            _close_quietly(handle)
