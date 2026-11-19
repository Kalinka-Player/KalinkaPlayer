"""Who can write the library, seen the way a waiting writer sees it."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_POLL_S = 0.1


@dataclass(frozen=True)
class Hold:
    """A stretch in which the write lock was never free when asked for.

    One long transaction and a run of short ones back to back look the same
    here, as they do to a writer waiting its turn.
    """

    started: float
    seconds: float

    @property
    def ended(self) -> float:
        return self.started + self.seconds


class WriteLockProbe:
    """Asks for the library's write lock ten times a second without waiting,
    as SQLite's busy handler polls for it on behalf of a blocked writer, and
    keeps every stretch in which it never got it.

    Getting the lock is undone at once, so the probe changes nothing and
    holds nothing.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._holds: list[Hold] = []
        self._held_since: Optional[float] = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lock-probe", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._thread.join()

    def holds(self, at_least_s: float) -> list[Hold]:
        """The stretches seen so far that lasted ``at_least_s`` or more, the
        one still under way among them."""
        with self._lock:
            holds = list(self._holds)
            if self._held_since is not None:
                holds.append(Hold(self._held_since, time.time() - self._held_since))
        return [hold for hold in holds if hold.seconds >= at_least_s]

    def _run(self) -> None:
        conn = sqlite3.connect(self._db_path, timeout=0, isolation_level=None)
        try:
            while not self._stopping.wait(_POLL_S):
                now = time.time()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    with self._lock:
                        if self._held_since is None:
                            self._held_since = now
                    continue
                self._end_stretch(now)
            self._end_stretch(time.time())
        finally:
            conn.close()

    def _end_stretch(self, now: float) -> None:
        with self._lock:
            if self._held_since is not None:
                self._holds.append(Hold(self._held_since, now - self._held_since))
                self._held_since = None
