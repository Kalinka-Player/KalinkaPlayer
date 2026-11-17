"""SQLite connections kept per thread and lent to one call at a time."""

from __future__ import annotations

import sqlite3
import threading
import weakref
from typing import Callable


class LentConnection(sqlite3.Connection):
    """A thread's kept connection, lent to one call at a time.

    ``close()`` gives it back rather than closing it, as a pooled connection's
    does. Giving it back leaves it holding nothing, as closing it did: every
    statement the call ran is reset, and a transaction it left open is rolled
    back. An idle kept connection so holds no lock. It keeps no writer
    waiting and no checkpoint from finishing, and the next call reads the
    database as it now stands.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lent = False
        self.discarded = False
        self.generation = 0
        self._ran: weakref.WeakSet[sqlite3.Cursor] = weakref.WeakSet()

    # Each of these makes a cursor: Python 3.11's execute() makes its own
    # without calling cursor().
    def cursor(self, *args, **kwargs) -> sqlite3.Cursor:
        return self._track(super().cursor(*args, **kwargs))

    def execute(self, *args, **kwargs) -> sqlite3.Cursor:
        return self._track(super().execute(*args, **kwargs))

    def executemany(self, *args, **kwargs) -> sqlite3.Cursor:
        return self._track(super().executemany(*args, **kwargs))

    def executescript(self, *args, **kwargs) -> sqlite3.Cursor:
        return self._track(super().executescript(*args, **kwargs))

    def _track(self, cursor: sqlite3.Cursor) -> sqlite3.Cursor:
        self._ran.add(cursor)
        return cursor

    def close(self) -> None:
        """Give the connection back, holding nothing.

        A connection that cannot be emptied is closed for good, and its
        thread opens another.
        """
        try:
            for cursor in list(self._ran):
                cursor.close()
            if self.in_transaction:
                self.rollback()
        except sqlite3.Error:
            self.discard()
        finally:
            self._ran.clear()
            self.lent = False

    def discard(self) -> None:
        """Close the connection for good."""
        self.discarded = True
        super().close()


class KeptConnections:
    """One connection per thread, opened on the thread's first call and kept.

    Opening a connection to the library parses its whole schema, 2.3 ms of
    CPU on a Pi 4, where a query on one kept open costs 0.05 ms, and every
    range a renderer streamed used to open two. Each thread keeps one
    and lends it to one call at a time (see :class:`LentConnection`). A call
    made while its thread's connection is out gets one of its own, closed for
    good when it closes it. Connections are never shared between threads.

    @param open_connection Opens a connection to the database, set up for
        use, as an instance of the class it is given.
    """

    def __init__(
        self, open_connection: Callable[[type[sqlite3.Connection]], sqlite3.Connection]
    ) -> None:
        self._open = open_connection
        self._local = threading.local()
        self._generation = 0

    def lend(self) -> sqlite3.Connection:
        """This thread's connection, until the caller closes it."""
        kept = getattr(self._local, "connection", None)
        if kept is not None and (kept.discarded or kept.generation != self._generation):
            kept.discard()
            kept = None
        if kept is None:
            kept = self._open(LentConnection)
            kept.generation = self._generation
            self._local.connection = kept
        elif kept.lent:
            return self._open(sqlite3.Connection)
        kept.lent = True
        return kept

    def renew(self) -> None:
        """Have each thread open its connection afresh on its next call: the
        database file was replaced, and a kept connection still reads the
        old one."""
        self._generation += 1
