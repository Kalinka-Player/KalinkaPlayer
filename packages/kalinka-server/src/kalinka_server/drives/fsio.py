"""The threads the files on drives and shares are read on."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, Optional, Sequence, TypeVar

from kalinka_plugin_sdk.inputmodule import SourceUnavailableError

T = TypeVar("T")

#: How long a drive or share gets to answer one look at its files before it
#: is said not to be answering.
FS_TIMEOUT_S = 10.0
#: How long a page waits on its folders' counts; a folder not counted by
#: then is listed without them.
COUNT_BUDGET_S = 2.0


class DriveIO:
    """Threads for the files on drives and shares, a few for each volume: one
    that stops answering holds up its own, never another drive, the event
    loop or the threads the rest share.

    @param lane_of The volume a path is on, which names the threads it is
        read on.
    @note Thread-safe.
    """

    def __init__(self, lane_of: Callable[[str], str] = lambda _path: "", workers: int = 3):
        self._lane_of = lane_of
        self._workers = workers
        self._pools: dict[str, ThreadPoolExecutor] = {}
        self._lock = threading.Lock()

    def pool_for(self, path: str) -> ThreadPoolExecutor:
        """The threads files at ``path`` are read on."""
        lane = self._lane_of(path)
        with self._lock:
            pool = self._pools.get(lane)
            if pool is None:
                pool = self._pools[lane] = ThreadPoolExecutor(
                    max_workers=self._workers, thread_name_prefix="kal-drives"
                )
        return pool

    async def run(self, fn: Callable[..., T], *args, at: str, timeout: Optional[float] = None) -> T:
        """``fn(*args)`` on the threads of the volume ``at`` is on.

        @raise SourceUnavailableError When it has not answered in ``timeout``,
            FS_TIMEOUT_S unless given.
        """
        future = asyncio.get_running_loop().run_in_executor(self.pool_for(at), partial(fn, *args))
        try:
            return await asyncio.wait_for(future, FS_TIMEOUT_S if timeout is None else timeout)
        except asyncio.TimeoutError as e:
            raise SourceUnavailableError("The drive is not answering") from e

    async def each_within(self, fn: Callable[[str], T], paths: Sequence[str], budget: float) -> list[Optional[T]]:
        """``fn(path)`` for each of ``paths``, or None for one not done within
        ``budget``."""
        if not paths:
            return []
        loop = asyncio.get_running_loop()
        futures = [loop.run_in_executor(self.pool_for(path), fn, path) for path in paths]
        done, waiting = await asyncio.wait(futures, timeout=budget)
        for future in waiting:
            future.cancel()
        return [f.result() if f in done else None for f in futures]

    def close(self) -> None:
        with self._lock:
            pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            pool.shutdown(wait=False, cancel_futures=True)
