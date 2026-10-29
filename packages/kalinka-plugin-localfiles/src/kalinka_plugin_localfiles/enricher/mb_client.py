"""Paced access to the MusicBrainz web service.

Every MusicBrainz call in this package goes through :func:`mb_call`, which
owns the whole access policy: it spaces request *starts* one second apart
(MusicBrainz's published limit for anonymous clients) and runs the blocking
call in a worker thread.

musicbrainzngs has its own limiter, but it holds a lock around the request
itself, so concurrent callers execute strictly one at a time — pacing and
round-trip latency add up, and a request that retries a 5xx internally
(tens of seconds) freezes every other caller behind it. Since the enricher
now works on several entities at once, that serialisation was costing more
than the rate limit itself, so this module disables it and paces the starts
instead: the average stays within the published limit while round-trips
overlap. That makes this module the only correct way in: a caller that
reaches musicbrainzngs directly is no longer paced by anything.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

import musicbrainzngs

# Request starts, in seconds. MusicBrainz allows an anonymous client roughly
# one request per second averaged over time.
_MIN_INTERVAL = 1.0

# Ceiling on requests in flight at once. Pacing alone keeps this near 1-2;
# the cap only matters when responses turn slow, and keeps us from holding a
# pile of connections open against one host.
_MAX_IN_FLIGHT = 4

# See the module docstring: musicbrainzngs' own limiter serialises whole
# requests, so we take over the pacing.
musicbrainzngs.set_rate_limit(False)


class _Pacer:
    """Hands out request slots spaced ``min_interval`` apart, and caps how
    many calls are in flight.

    The lock is held only while reserving a slot, never across the wait, so
    callers queue in arrival order without blocking each other's requests.

    Its lock and semaphore belong to the event loop that first contends on
    them, so they are built on first use and rebuilt if the running loop
    changes — the slot clock starts over with it.

    @param clock A monotonic clock, in seconds; the one asyncio's own loop reads.
    @param sleep Waits out a delay in that clock's seconds.
    """

    def __init__(
        self,
        min_interval: float,
        max_in_flight: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._max_in_flight = max_in_flight
        self._clock = clock
        self._sleep = sleep
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock: Optional[asyncio.Lock] = None
        self._in_flight: Optional[asyncio.Semaphore] = None
        self._next_slot = 0.0

    def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._lock = asyncio.Lock()
            self._in_flight = asyncio.Semaphore(self._max_in_flight)
            self._next_slot = 0.0

    async def wait_turn(self) -> None:
        self._bind()
        async with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._min_interval
        delay = slot - now
        if delay > 0:
            await self._sleep(delay)

    def in_flight(self) -> asyncio.Semaphore:
        self._bind()
        return self._in_flight


_pacer = _Pacer(_MIN_INTERVAL, _MAX_IN_FLIGHT)


def set_user_agent(user_agent: str) -> None:
    """Identify this client to MusicBrainz, which rejects anonymous callers.

    musicbrainzngs keeps the identification on the module, so whichever
    plugin is constructed first supplies it for every caller. Each plugin
    that reaches MusicBrainz sets it, rather than relying on another plugin
    being enabled.

    @param user_agent A ``name/version (contact)`` string, split into the
        three parts musicbrainzngs takes separately.
    """
    name, _, rest = user_agent.partition("/")
    version, _, contact = rest.partition(" ")
    musicbrainzngs.set_useragent(
        name.strip() or "Kalinka", version.strip() or "1.0", contact.strip("() ")
    )


async def mb_call(fn, *args, **kwargs):
    """Await one paced MusicBrainz call, executed off the event loop.

    ``fn`` is the musicbrainzngs function (``search_artists``,
    ``get_release_by_id``, …); exceptions propagate to the caller unchanged,
    so the plugins keep classifying them as verdicts or transient failures.

    The permit is taken before the slot is reserved: a slot elapsing while
    every permit is held would otherwise release its waiters together.
    """
    async with _pacer.in_flight():
        await _pacer.wait_turn()
        return await asyncio.to_thread(fn, *args, **kwargs)
