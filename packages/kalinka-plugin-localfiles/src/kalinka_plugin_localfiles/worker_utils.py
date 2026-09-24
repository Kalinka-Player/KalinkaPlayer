"""Shared utilities for long-lived subprocess workers."""

from __future__ import annotations

import asyncio
import ctypes
import functools
import inspect
import logging
import logging.handlers
import multiprocessing
import queue
import random
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__.split(".")[-1])


def configure_worker_logging(logger_queue: multiprocessing.Queue) -> None:
    """Forward worker logs, suppressing SMB packet dumps at their source.

    QueueHandler formats records before sending them. Filtering only in the
    parent is too late: SMB DEBUG records render entire audio packets as hex
    while holding the handler lock, blocking unrelated metadata requests.
    Spawned workers must set these levels themselves, even when the parent's
    loggers already suppress the protocol chatter.
    """
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG)
    for name in ("smbprotocol", "smbclient"):
        logging.getLogger(name).setLevel(logging.WARNING)
    root.addHandler(logging.handlers.QueueHandler(logger_queue))


# ---------------------------------------------------------------------------
# SQLite "database is locked" retry
# ---------------------------------------------------------------------------
#
# The localfiles workers (indexer, enricher, searcher, embedder) run as
# separate processes that all write the *same* SQLite file. WAL permits one
# writer at a time, so during a burst — e.g. every scheduled scan cycle wakes
# several workers at once — a writer can wait past ``busy_timeout`` and surface
# ``sqlite3.OperationalError: database is locked``. Reopening a fresh
# connection/transaction and trying again clears the vast majority of these.

# How many times an operation is attempted in total (initial try + retries).
_DB_LOCK_ATTEMPTS = 3
# Base backoff; the per-attempt cap grows exponentially from here.
_DB_LOCK_BASE_DELAY = 0.05
# Upper bound on any single backoff wait.
_DB_LOCK_MAX_DELAY = 1.0


def _is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def retry_on_locked(fn):
    """Wrap an async DB method so it retries on ``database is locked``.

    Tries at most ``_DB_LOCK_ATTEMPTS`` times. Backoff is exponential with
    *full jitter* (``random.uniform(0, cap)``) so the contending workers don't
    resynchronise and collide again on the same retry tick. Any non-lock error
    — or exhausting the attempts — re-raises immediately.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        for attempt in range(1, _DB_LOCK_ATTEMPTS + 1):
            try:
                return await fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc) or attempt == _DB_LOCK_ATTEMPTS:
                    raise
                cap = min(_DB_LOCK_MAX_DELAY, _DB_LOCK_BASE_DELAY * 2 ** (attempt - 1))
                delay = random.uniform(0, cap)
                logger.warning(
                    "%s: database is locked (attempt %d/%d), retrying in %.3fs",
                    fn.__qualname__,
                    attempt,
                    _DB_LOCK_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)

    return wrapper


def retry_db_locked(cls):
    """Class decorator: wrap every public async method with ``retry_on_locked``.

    Only public (non ``_``-prefixed) coroutine methods are wrapped — these own
    their full connection lifecycle via ``_open()``, so a retry always reopens a
    clean connection rather than reusing one mid-transaction. Internal helpers
    (including the ``_open`` context manager and any method handed an existing
    connection) are left untouched.
    """
    for name, member in list(vars(cls).items()):
        if name.startswith("_"):
            continue
        if inspect.iscoroutinefunction(member):
            setattr(cls, name, retry_on_locked(member))
    return cls


def stage_status(
    total: int, done: int, failed: int = 0, in_progress: int = 0
) -> dict:
    """One stage entry of ``get_indexer_status()`` — the fixed shape the
    client's StageStatus model parses. Shared by the indexing, enrichment
    and embedding producers so the wire format cannot drift between them.
    ``pending`` is derived: the four statuses always partition ``total``."""
    return {
        "total": total,
        "done": done,
        "pending": max(total - done - failed - in_progress, 0),
        "in_progress": in_progress,
        "failed": failed,
        "coverage_pct": round(100.0 * done / total, 1) if total else 0.0,
    }


# Linux prctl op for setting the kernel-level process name (visible as
# COMM in `ps -o comm`). Kernel truncates to 15 chars + NUL.
_PR_SET_NAME = 15


def set_proc_title(name: str) -> None:
    """Set the running process's ``comm`` (kernel-level name) so it
    shows up distinctly in ``ps`` / ``htop`` / ``top``.

    Without this, multiprocessing-spawned children inherit the parent's
    command line and command name, so ``ps -o comm`` shows five
    identical ``kalinka-server`` rows and it's impossible to tell at a
    glance which one is hot.

    Best-effort: silently no-ops on non-Linux or if libc isn't
    available — process naming is purely a diagnostic affordance and
    a failure here must never block worker startup.
    """
    if not name:
        return
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        # Linux truncates to TASK_COMM_LEN-1 = 15 bytes anyway, but
        # being explicit avoids passing the rest into kernel space.
        libc.prctl(_PR_SET_NAME, name.encode("utf-8")[:15], 0, 0, 0)
    except Exception:  # noqa: BLE001 — diagnostic only
        pass


def nudge(nudge_queue: Optional[multiprocessing.Queue]) -> None:
    """Best-effort wake-up for a sibling worker's interruptible sleep.

    Losing a nudge only costs waiting out the poll interval, so a full or
    torn-down queue is ignored rather than propagated into the caller's own
    work loop.
    """
    if nudge_queue is None:
        return
    try:
        nudge_queue.put_nowait("nudge")
    except Exception:  # noqa: BLE001 — wake-ups are advisory
        pass


async def sleep_interruptible(
    duration: float,
    shutdown_event: asyncio.Event,
    nudge_queue: Optional[multiprocessing.Queue],
    label: str = "Worker",
) -> bool:
    """Sleep for *duration* seconds, waking early on shutdown or nudge.

    Returns True if woken by a nudge, False otherwise.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    while not shutdown_event.is_set():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        if nudge_queue is not None:
            try:
                nudge_queue.get_nowait()
                logger.debug("%s woken by nudge", label)
                return True
            except queue.Empty:
                pass
        await asyncio.sleep(min(1.0, remaining))
    return False
