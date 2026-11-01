"""The threads the files on drives and shares are read on."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, Optional, Sequence, TypeVar

from kalinka_plugin_sdk.inputmodule import SourceUnavailableError

T = TypeVar("T")
A = TypeVar("A")

#: How long a drive or share gets to answer one look at its files before it
#: is said not to be answering.
FS_TIMEOUT_S = 10.0
#: How long a page waits on its folders' counts; a folder not counted by
#: then is listed without them.
COUNT_BUDGET_S = 2.0


class DriveIO:
    """Threads of their own for drives and shares: one that stops answering
    holds them up, never the event loop or the threads the rest share."""

    def __init__(self, workers: int = 4):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kal-drives")

    async def run(self, fn: Callable[..., T], *args, timeout: Optional[float] = None) -> T:
        """``fn(*args)`` on these threads.

        @raise SourceUnavailableError When it has not answered in ``timeout``,
            FS_TIMEOUT_S unless given.
        """
        future = asyncio.get_running_loop().run_in_executor(self._pool, partial(fn, *args))
        try:
            return await asyncio.wait_for(future, FS_TIMEOUT_S if timeout is None else timeout)
        except asyncio.TimeoutError as e:
            raise SourceUnavailableError("The drive is not answering") from e

    async def each_within(
        self, fn: Callable[[A], T], items: Sequence[A], budget: float
    ) -> list[Optional[T]]:
        """``fn(item)`` for each of ``items``, or None for one not done within
        ``budget``."""
        if not items:
            return []
        loop = asyncio.get_running_loop()
        futures = [loop.run_in_executor(self._pool, fn, item) for item in items]
        done, waiting = await asyncio.wait(futures, timeout=budget)
        for future in waiting:
            future.cancel()
        return [f.result() if f in done else None for f in futures]

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
