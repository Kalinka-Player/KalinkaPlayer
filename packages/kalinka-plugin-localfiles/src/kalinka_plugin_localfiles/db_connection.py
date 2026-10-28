"""Connections to the library database.

Every class that reads or writes it inherits :class:`LibraryDb`, and each
of its calls opens a connection of its own — unless the calling task is
inside :func:`shared_connection` for the same database, where they all
reuse one. Opening one costs about a millisecond, and indexing a new file
makes more than a dozen calls.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any, AsyncIterator, List, Mapping, Optional, Union

import aiosqlite


class _Shared:
    """A connection :func:`shared_connection` lends, one call at a time."""

    __slots__ = ("_conn", "_lock", "_holder")

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        self._lock = asyncio.Lock()
        self._holder: Optional[asyncio.Task] = None

    @asynccontextmanager
    async def turn(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._holder is asyncio.current_task():
            raise RuntimeError(
                "a library-database call was made while its caller holds the "
                "shared connection, and would wait for it forever"
            )
        async with self._lock:
            self._holder = asyncio.current_task()
            try:
                yield self._conn
            finally:
                self._holder = None


_shared: ContextVar[Mapping[str, _Shared]] = ContextVar(
    "shared_library_connections", default=MappingProxyType({})
)


def _key(db_path: Union[str, os.PathLike]) -> str:
    """One spelling per database file: the classes keep the configured path
    as a string or as a resolved ``Path``."""
    return os.path.realpath(os.path.expanduser(os.fspath(db_path)))


@asynccontextmanager
async def _connected(db_path: str) -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(db_path, timeout=5.0) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        yield conn


@asynccontextmanager
async def shared_connection(
    db_path: Union[str, os.PathLike],
) -> AsyncIterator[None]:
    """Let the library-database calls the current task makes inside this
    block reuse one connection, closed when the block ends.

    Each call still leaves the database as a connection of its own would on
    closing (see :class:`LibraryConnection`), and calls from tasks gathered
    inside the block take turns. Between calls the connection holds neither
    a transaction nor a read, so it neither locks out nor holds back the
    other processes that write the library. A block inside another for the
    same database reuses the outer one.

    @note A task started inside the block must finish inside it, since it
        shares the connection the block closes.
    """
    key = _key(db_path)
    if key in _shared.get():
        yield
        return
    async with _connected(key) as conn:
        token = _shared.set(
            MappingProxyType({**_shared.get(), key: _Shared(conn)})
        )
        try:
            yield
        finally:
            _shared.reset(token)


class _Opening:
    """A cursor being opened for a :class:`LibraryConnection`, recorded once
    it is — awaited or entered with ``async with``, as aiosqlite's own
    result is."""

    __slots__ = ("_result", "_cursors", "_cursor")

    def __init__(self, result: Any, cursors: List[aiosqlite.Cursor]):
        self._result = result
        self._cursors = cursors

    def __await__(self):
        return self._open().__await__()

    async def _open(self) -> aiosqlite.Cursor:
        cursor = await self._result
        self._cursors.append(cursor)
        return cursor

    async def __aenter__(self) -> aiosqlite.Cursor:
        self._cursor = await self._open()
        return self._cursor

    async def __aexit__(self, *exc_info) -> None:
        await self._cursor.close()


class LibraryConnection:
    """A connection as one call sees it.

    Letting go closes every cursor the call opened and rolls back what it
    left uncommitted, which is what closing a connection of its own would
    do. A cursor left part-way through its rows holds a read snapshot until
    it is closed, and an open transaction would be committed by the next
    call's commit.

    @note Only what the library's calls use is offered — no ``close``, which
        would take a shared connection away from the rest of the scan.
    """

    __slots__ = ("_conn", "_cursors")

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        self._cursors: List[aiosqlite.Cursor] = []

    @property
    def row_factory(self) -> Any:
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, factory: Any) -> None:
        self._conn.row_factory = factory

    def cursor(self) -> _Opening:
        return _Opening(self._conn.cursor(), self._cursors)

    def execute(self, *args: Any) -> _Opening:
        return _Opening(self._conn.execute(*args), self._cursors)

    def executemany(self, *args: Any) -> _Opening:
        return _Opening(self._conn.executemany(*args), self._cursors)

    async def commit(self) -> None:
        await self._conn.commit()

    async def enable_load_extension(self, enabled: bool) -> None:
        await self._conn.enable_load_extension(enabled)

    async def load_extension(self, path: str) -> None:
        await self._conn.load_extension(path)

    async def _release(self) -> None:
        try:
            for cursor in self._cursors:
                await cursor.close()
        finally:
            try:
                if self._conn.in_transaction:
                    await self._conn.rollback()
            finally:
                self._conn.row_factory = None


@asynccontextmanager
async def _calling(
    conn: aiosqlite.Connection,
) -> AsyncIterator[LibraryConnection]:
    call = LibraryConnection(conn)
    try:
        yield call
    finally:
        await call._release()


class LibraryDb:
    """Base for the classes that read and write the library database at
    ``db_path``, which each sets as it is constructed.

    @note A method must not call another while it holds its connection:
        inside :func:`shared_connection` the second raises ``RuntimeError``
        rather than wait for the first to let go.
    """

    db_path: Union[str, os.PathLike]

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[LibraryConnection]:
        """A connection for one call, opened for it or lent by
        :func:`shared_connection`."""
        shared = _shared.get().get(_key(self.db_path))
        if shared is None:
            async with _connected(self.db_path) as conn, _calling(conn) as call:
                yield call
            return
        async with shared.turn() as conn, _calling(conn) as call:
            yield call
