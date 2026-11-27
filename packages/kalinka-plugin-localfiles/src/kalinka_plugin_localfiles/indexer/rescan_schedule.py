"""When the library's nightly full scan is due."""

from __future__ import annotations

from typing import Awaitable, Callable

# The window the server's auto-upgrade uses too: nobody is listening then.
NIGHT_HOURS = range(3, 6)

# A box started at night, or rescanned in the evening, has nothing left to find.
SPACING_S = 20 * 60 * 60

# A failed scan gets another try while the night lasts, not one after another.
RETRY_S = 30 * 60


class DailyRescan:
    """Decides when the indexer worker starts its nightly full scan.

    At most one a night, inside :data:`NIGHT_HOURS`, and never within
    :data:`SPACING_S` of the start of the last full scan of any kind that
    completed, nor within :data:`RETRY_S` of the start of one that did not.

    @param enabled Whether nightly scans run at all.
    @param monotonic Seconds on a clock that never steps, for the spacing: a
        Pi without a real-time clock jumps its wall clock when it first syncs.
    @param local_hour The local wall-clock hour, for the window.
    @note Owned by the worker's event loop; not thread-safe.
    """

    def __init__(
        self,
        enabled: bool,
        monotonic: Callable[[], float],
        local_hour: Callable[[], int],
    ):
        self._enabled = enabled
        self._monotonic = monotonic
        self._local_hour = local_hour
        self._last_scan = self._last_try = monotonic()

    async def run(self, scan: Callable[[], Awaitable[bool]]) -> None:
        """Runs a full scan, whatever started it, which returns whether it
        completed. One that completes counts from its start, so a long one
        does not push the next night's out; one that fails or raises does
        not count."""
        began = self._last_try = self._monotonic()
        if await scan():
            self._last_scan = began

    def due(self) -> bool:
        now = self._monotonic()
        return (
            self._enabled
            and self._local_hour() in NIGHT_HOURS
            and now - self._last_scan >= SPACING_S
            and now - self._last_try >= RETRY_S
        )
