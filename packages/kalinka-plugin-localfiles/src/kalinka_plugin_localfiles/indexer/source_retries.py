"""Source outages have a different retry policy from unfinished uploads."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ..storage import ChangeKind


@dataclass
class _Retry:
    delay: float
    deadline: float
    changes: set[tuple[str, str]] = field(default_factory=set)


class SourceRetries:
    """Back off unavailable roots and reconcile their entire tree on recovery.

    Fresh events cannot bypass a source's cooldown. Remember write events as
    well as the owed scan: a tagger can preserve a file's size and timestamp,
    so a scan alone would miss the edit. Bound that buffer during long outages.
    """

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic,
        initial_delay: float = 30.0, max_delay: float = 300.0,
    ) -> None:
        self._clock = clock
        self._initial_delay = initial_delay
        self._max_delay = max_delay
        self._roots: dict[str, _Retry] = {}

    def __contains__(self, root: str) -> bool:
        # A deadline expiring permits a probe, not per-file reads.
        return root in self._roots

    def unavailable(self, root: str) -> float:
        previous = self._roots.get(root)
        delay = min(
            previous.delay * 2 if previous else self._initial_delay,
            self._max_delay,
        )
        self._roots[root] = _Retry(
            delay, self._clock() + delay,
            previous.changes if previous else set(),
        )
        return delay

    def defer(self, root: str, change: tuple[str, str]) -> bool:
        retry = self._roots.get(root)
        if retry is None:
            return False
        # The recovery scan covers arrivals, deletions, renames and artwork.
        if change[0] == ChangeKind.FILE_WRITTEN and len(retry.changes) < 10000:
            retry.changes.add(change)
        return True

    def due(self) -> list[str]:
        now = self._clock()
        return [root for root, retry in self._roots.items() if retry.deadline <= now]

    def next_retry_in(self) -> float | None:
        if not self._roots:
            return None
        return max(0.0, min(r.deadline for r in self._roots.values()) - self._clock())

    def recovered(self, root: str) -> set[tuple[str, str]]:
        retry = self._roots.pop(root)
        return retry.changes | {(ChangeKind.DIR_ADDED, root)}
