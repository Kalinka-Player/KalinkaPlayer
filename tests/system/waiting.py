"""Polling with a deadline, for a test that waits on background work."""

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger("system-test")

_PROGRESS_EVERY_S = 15.0


def wait_until(
    condition: Callable[[], bool],
    *,
    timeout: float,
    what: str,
    interval: float = 1.0,
    progress: Optional[Callable[[], str]] = None,
) -> None:
    """Block until ``condition`` holds, raising ``TimeoutError`` naming
    ``what`` otherwise. ``progress`` is logged periodically so a long wait
    shows what it is waiting on."""
    deadline = time.monotonic() + timeout
    next_progress = time.monotonic() + _PROGRESS_EVERY_S
    while True:
        if condition():
            return
        now = time.monotonic()
        if now >= deadline:
            detail = f" ({progress()})" if progress else ""
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}{detail}")
        if progress and now >= next_progress:
            logger.info("waiting for %s: %s", what, progress())
            next_progress = now + _PROGRESS_EVERY_S
        time.sleep(interval)
