"""MusicBrainz pacing: request *starts* are spaced, round-trips overlap.

musicbrainzngs' own limiter holds a lock across the whole request, so
concurrent callers run strictly one at a time and pay pacing + latency each.
``mb_client`` disables it and spaces the starts instead, which is what makes
concurrent enrichment worth anything: N calls cost about N intervals, not N
× (interval + latency).

The pacer runs on a clock that stands still, so no test here waits out an
interval or measures one: each reads the start the pacer planned for every
call.
"""

from __future__ import annotations

import asyncio
import threading

import musicbrainzngs
import pytest

from kalinka_plugin_localfiles.enricher import mb_client
from kalinka_plugin_localfiles.enricher.mb_client import mb_call

INTERVAL = 1.0

# Bounds only a failure, a call that never gets to run; passing never waits on it.
DEADLOCK = 5.0


class _StillClock:
    """A pacer clock that moves only when the test moves it.

    The pacer reads the clock once per reservation and sleeps straight after
    if the slot is still ahead, so each reading books a caller to start then,
    and a sleep moves that caller's start later.
    """

    def __init__(self, now: float = 100.0) -> None:
        self.now = now
        self.starts: list[float] = []

    def time(self) -> float:
        self.starts.append(self.now)
        return self.now

    async def sleep(self, delay: float) -> None:
        self.starts[-1] += delay


def _still_pacer(monkeypatch, max_in_flight: int = 4) -> _StillClock:
    clock = _StillClock()
    monkeypatch.setattr(
        mb_client,
        "_pacer",
        mb_client._Pacer(INTERVAL, max_in_flight, clock=clock.time, sleep=clock.sleep),
    )
    return clock


@pytest.fixture
def still(monkeypatch):
    return _still_pacer(monkeypatch)


def test_library_rate_limiting_is_disabled():
    """Ours replaces it; leaving both on would serialise every request
    behind the library's lock again."""
    assert musicbrainzngs.musicbrainz.do_rate_limit is False


@pytest.mark.asyncio
async def test_starts_are_spaced(still):
    await asyncio.gather(*(mb_call(lambda: None) for _ in range(5)))

    assert still.starts == [100.0, 101.0, 102.0, 103.0, 104.0]


@pytest.mark.asyncio
async def test_slow_requests_overlap(still):
    """A slow response must not hold back the next request's start — that
    serialisation is exactly what the library's limiter did. The first call
    returns only once the second has started."""
    second_started = threading.Event()

    def first():
        return second_started.wait(DEADLOCK)

    overlapped, _ = await asyncio.gather(mb_call(first), mb_call(second_started.set))

    assert overlapped


@pytest.mark.asyncio
async def test_waiting_for_a_permit_does_not_bank_a_slot(monkeypatch):
    """A caller must reserve its slot after it gets a permit, not before.

    Slots taken while every permit is held elapse unused in the queue, so
    when two slow calls finish at the same instant their waiters start
    together — a burst straight through the one-per-second budget.
    """
    clock = _still_pacer(monkeypatch, max_in_flight=2)
    holding = threading.Semaphore(0)
    release = threading.Event()

    def hold():
        holding.release()
        release.wait(DEADLOCK)

    calls = asyncio.ensure_future(
        asyncio.gather(mb_call(hold), mb_call(hold), *(mb_call(lambda: None) for _ in range(4)))
    )
    for _ in range(2):
        assert await asyncio.to_thread(holding.acquire, timeout=DEADLOCK)
    clock.now = 200.0
    release.set()
    await calls

    assert clock.starts == [100.0, 101.0, 200.0, 201.0, 202.0, 203.0]


def test_survives_a_change_of_event_loop(still):
    """The lock and semaphore belong to the loop that first contends on
    them; a later loop must get its own rather than "bound to a different
    event loop"."""

    async def burst():
        await asyncio.gather(*(mb_call(lambda: None) for _ in range(6)))

    asyncio.run(burst())
    asyncio.run(burst())


@pytest.mark.asyncio
async def test_exceptions_propagate_unchanged(still):
    """The plugins classify MB errors as verdicts or transient failures, so
    the wrapper must not wrap or swallow them."""

    def boom():
        raise musicbrainzngs.NetworkError("down")

    with pytest.raises(musicbrainzngs.NetworkError):
        await mb_call(boom)


@pytest.mark.asyncio
async def test_arguments_are_forwarded(still):
    def echo(*args, **kwargs):
        return args, kwargs

    assert await mb_call(echo, "a", limit=20) == (("a",), {"limit": 20})
