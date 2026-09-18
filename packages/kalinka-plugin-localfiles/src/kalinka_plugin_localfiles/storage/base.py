"""What the library needs of the storage its music sits on.

:class:`FileStorage` is the whole of it: list a directory, measure a file,
open one for reading, and say whether a configured root is reachable. One
implementation wraps the local filesystem — and with it every share the
kernel has mounted — another talks SMB itself. Callers hold the abstraction
and never test for a protocol.

Every method here is blocking and belongs off the event loop; a remote
listing is network I/O and a hung mount never returns. The availability
probes are the exception: they are async because they are bounded, and
because one slow root must not cost a worker thread per caller.

Learning about changes as they happen is a separate ability, because only
some protocols have it: :meth:`FileStorage.watcher` returns a
:class:`ChangeWatcher` where the protocol can push, and None where the
periodic scan is all there is.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import BinaryIO, Iterable, Iterator, Optional

logger = logging.getLogger(__name__.split(".")[-1])

#: How long a single availability probe may take before the root is called
#: unreachable. The blocked worker is left to its call and reused.
PROBE_TIMEOUT_S = 5.0

#: Gaps between retries while a root is given its chance to appear — long
#: enough for an automount to finish, short enough not to stall a scan.
_RETRY_DELAYS_S = (0.5, 1.0, 2.0)

_SPILL_CHUNK = 1024 * 1024

#: Specific to a spilled copy, because the sweep below deletes by name and
#: the default spill directory is the one every other program shares.
_SPILL_PREFIX = "kalinka-spill-"

#: Room a spilled copy must leave behind it. The library database and its
#: artwork share the filesystem, and filling it costs more than one
#: fingerprint does.
_SPILL_HEADROOM = 256 * 1024 * 1024

#: Age past which a spilled copy can only be an orphan: every one of them is
#: removed by the block that made it, so a survivor outlived a kill.
_SPILL_STALE_S = 3600.0


@dataclass(frozen=True)
class FileIdentity:
    """What makes a file the same file after it is renamed.

    Locally this is the device and inode; over SMB, the volume serial and
    the server's file index.

    @note ``device`` must be unique across storages, not merely within one:
        the library keeps every identity in a single table, so a storage
        whose numbering could collide with another's namespaces it. A
        storage that cannot identify a file reports no identity at all
        rather than a placeholder — one shared between files would make each
        look like a rename of the last.
    """

    device: str
    inode: str


@dataclass(frozen=True)
class FileStat:
    """What one file or directory is, as the storage reports it.

    @param identity None where the protocol will not say, which costs
        move detection and nothing else.
    """

    size: int
    mtime_ns: int
    is_dir: bool
    identity: Optional[FileIdentity] = None

    @property
    def mtime(self) -> int:
        """Whole seconds, which is the resolution the index stores."""
        return self.mtime_ns // 1_000_000_000


@dataclass(frozen=True)
class DirEntry:
    """One child of a directory.

    ``is_dir`` is False for a symbolic link to a directory: a scan must not
    descend through one, matching ``os.walk`` without ``followlinks``.

    ``size`` is None unless the listing itself reported it. A remote
    directory query carries sizes, while locally they would cost a stat per
    entry — which the pre-count pass over a large library cannot afford.
    Read it through :meth:`FileStorage.size_of`.
    """

    name: str
    path: str
    is_dir: bool
    size: Optional[int] = None


@dataclass(frozen=True)
class RootStatus:
    """Availability verdict for one configured music folder.

    ``available`` means the folder exists, is readable, and is not hidden
    behind a pending automount. ``reason`` is a user-presentable phrase,
    non-empty when unavailable.

    Whether the folder holds anything is not part of the verdict: only the
    purge guard needs that, every playback needs this, and over a share it
    is a round trip of its own (:meth:`FileStorage.is_empty`).

    ``identity`` names the storage the folder currently lives on
    ("nfs4 192.168.1.5:/export", "ext4 /dev/sda1", "smb //nas/music 1a2b3c4d").
    Stable across reboots, it lets callers detect that a static mount
    silently gave way to the local directory underneath it — which
    availability alone cannot see.
    """

    root: str
    available: bool
    reason: str
    fs_type: Optional[str]
    is_network: bool
    is_autofs: bool
    identity: Optional[str] = None


class ChangeKind(str, Enum):
    """What happened to a path.

    A ``str`` enum because these values travel to the indexer's queue and
    are compared against the plain strings the queue has always carried.
    """

    DIR_ADDED = "dir_added"
    DIR_REMOVED = "dir_removed"
    FILE_WRITTEN = "file_closed"
    FILE_MOVED_IN = "file_moved"
    PATH_REMOVED = "path_removed"


@dataclass(frozen=True)
class WatchResult:
    """One batch from a watcher.

    @param changes Paths that changed, as ``(kind, path)`` pairs.
    @param unmounted Roots whose filesystem went away under them. The
        storage has already dropped their watches; whoever armed them owns
        re-arming.
    @param vanished Roots that were deleted or could not be watched, which
        owe a rescan whenever they come back.
    """

    changes: set[tuple[ChangeKind, str]] = field(default_factory=set)
    unmounted: set[str] = field(default_factory=set)
    vanished: set[str] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.changes or self.unmounted or self.vanished)


class ChangeWatcher(ABC):
    """A protocol's ability to report changes as they happen.

    Armed per root, polled for batches, and responsible for keeping itself
    armed as directories come and go underneath a watched root — the caller
    only re-arms a root the watcher reports as lost.

    Blocking, single-threaded and not reentrant: :meth:`poll` is meant to be
    driven from one worker.
    """

    @abstractmethod
    def watch(self, root: str) -> bool:
        """Arm ``root`` and everything under it. False when it could not be
        armed, which is not an error — the root may simply not be there
        yet."""

    @abstractmethod
    def unwatch(self, root: str) -> None:
        """Retire ``root`` and its subtree."""

    @abstractmethod
    def poll(self, timeout_s: float) -> WatchResult:
        """Block for up to ``timeout_s`` and return what changed. An empty
        result means the wait elapsed quietly."""

    @abstractmethod
    def close(self) -> None:
        """Release the watcher's resources. Idempotent."""


class FileStorage(ABC):
    """One protocol's view of the files a library is indexed from.

    Paths are opaque strings that only the storage that produced them may
    interpret; :class:`~.resolver.StorageResolver` is what maps a path back
    to its storage. Every path a caller passes must be canonical (see
    :meth:`canonical`), because membership of a configured root is decided
    textually.

    @note Implementations are shared between threads. They must be stateless
        or internally synchronised.
    """

    def __init__(self, spill_dir: Optional[str] = None) -> None:
        self._spill_dir = spill_dir
        self._inflight: dict[
            str, tuple[asyncio.AbstractEventLoop, "asyncio.Task[RootStatus]"]
        ] = {}
        self._swept = False


    @property
    @abstractmethod
    def scheme(self) -> str:
        """The protocol name this storage answers for."""

    @abstractmethod
    def handles(self, path: str) -> bool:
        """Whether ``path`` names a location this storage speaks for. Decided
        from the protocol alone, so it stays cheap enough for a per-file
        call and never touches the network."""

    @abstractmethod
    def canonical(self, path: str) -> str:
        """``path`` in the one spelling this storage compares and stores.

        @raise LocatorError If the path cannot be used as written.
        """

    @abstractmethod
    def contains(self, path: str, roots: Iterable[str]) -> bool:
        """Whether ``path`` lies inside any of ``roots``.

        The access boundary: only files under a configured music folder may
        be indexed or served. Matching is on a separator boundary, so
        ``/Music`` does not admit a sibling ``/Music2``, and a path equal to
        a root is inside it.

        @note Every root is offered at once so the path is canonicalised
            once per question rather than once per root — this is asked for
            each file the library touches, and resolving a local path walks
            it with an ``lstat`` per component.
        """


    @abstractmethod
    def listdir(self, path: str) -> list[DirEntry]:
        """The children of a directory.

        @raise OSError If the directory cannot be listed.
        """

    @abstractmethod
    def stat(self, path: str) -> FileStat:
        """Measure one file or directory.

        @raise OSError If it is absent or cannot be measured.
        """

    @abstractmethod
    def open(self, path: str) -> BinaryIO:
        """Open a file for reading bytes.

        The object is seekable: tag readers and the audio embedder both
        jump around inside a file rather than reading it through.

        @raise OSError If it cannot be opened.
        """

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
        except OSError:
            return False
        return True

    def is_dir(self, path: str) -> bool:
        try:
            return self.stat(path).is_dir
        except OSError:
            return False

    def is_file(self, path: str) -> bool:
        try:
            return not self.stat(path).is_dir
        except OSError:
            return False

    def readable(self, path: str) -> bool:
        """Whether the file's bytes can actually be read. The default asks
        the only question the protocol answers reliably — it opens it."""
        try:
            with self.open(path):
                return True
        except OSError:
            return False

    def size_of(self, entry: DirEntry) -> int:
        """How big a listed entry is, measuring it only when the listing did
        not say. 0 for anything that cannot be measured, which reads as
        "nothing worth opening" to every caller."""
        if entry.size is not None:
            return entry.size
        try:
            return self.stat(entry.path).size
        except OSError:
            return 0

    def is_empty(self, root: str) -> bool:
        """Whether ``root`` holds nothing at all.

        Kept out of the availability probe on purpose: an empty folder the
        library expects files under may be a mountpoint with nothing mounted
        on it, which only the purge guard cares about, and the listing it
        costs is a round trip every playback would otherwise pay for.

        @raise OSError If the root cannot be listed, which is not the same
            answer as "empty" and is not this method's to interpret.
        """
        return not self.listdir(root)

    def local_path(self, path: str) -> Optional[str]:
        """Where a process on this machine can read the file directly, or
        None when only this storage can reach it. What lets the server hand
        a local file straight to the kernel instead of proxying it."""
        return None

    @contextmanager
    def materialize(self, path: str) -> Iterator[str]:
        """A real filesystem path for ``path``, for the length of the block.

        For tools that will not take a file object — ``fpcalc`` decodes a
        file it opens by name. The default copies the bytes out and removes
        the copy afterwards; storage that is already local yields the file
        itself and copies nothing.
        """
        local = self.local_path(path)
        if local is not None:
            yield local
            return

        spill_dir = self._spill_dir or tempfile.gettempdir()
        os.makedirs(spill_dir, exist_ok=True)
        self._sweep_spill(spill_dir)
        self._require_room(spill_dir, path)
        fd, temp_path = tempfile.mkstemp(
            dir=spill_dir, prefix=_SPILL_PREFIX, suffix=os.path.splitext(path)[1]
        )
        try:
            with os.fdopen(fd, "wb") as sink, self.open(path) as source:
                shutil.copyfileobj(source, sink, _SPILL_CHUNK)
            yield temp_path
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                logger.debug("Could not remove the copy of %s", path)

    def _require_room(self, spill_dir: str, path: str) -> None:
        """Refuse a copy the filesystem cannot take.

        The appliance spills onto its SD card, and a high-resolution album
        is large enough that filling it is a real outcome rather than a
        theoretical one. Better to lose one file's fingerprint than the
        room the library and the database need.

        @raise OSError If the copy would not leave :data:`_SPILL_HEADROOM`
            free, or the size cannot be established.
        """
        needed = self.stat(path).size + _SPILL_HEADROOM
        free = shutil.disk_usage(spill_dir).free
        if free < needed:
            raise OSError(
                f"not enough room in {spill_dir} to copy {path} "
                f"({free} bytes free, {needed} needed)"
            )

    def _sweep_spill(self, spill_dir: str) -> None:
        """Remove copies a previous run was killed before releasing.

        Every copy is removed by the block that made it, so anything older
        than :data:`_SPILL_STALE_S` was orphaned by a kill or a power cut.
        Once per process is enough: nothing else leaves one behind.

        @note Only :data:`_SPILL_PREFIX` names are touched, which is the
            whole of what keeps a shared temporary directory safe here.
        """
        if self._swept:
            return
        self._swept = True
        cutoff = time.time() - _SPILL_STALE_S
        try:
            with os.scandir(spill_dir) as entries:
                stale = [
                    entry.path
                    for entry in entries
                    if entry.name.startswith(_SPILL_PREFIX)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff
                ]
        except OSError as e:
            logger.debug("Could not sweep %s: %s", spill_dir, e)
            return
        for orphan in stale:
            try:
                os.unlink(orphan)
                logger.info("Removed an orphaned copy: %s", orphan)
            except OSError:
                logger.debug("Could not remove the orphaned copy %s", orphan)

    @abstractmethod
    def probe_root_blocking(self, root: str) -> RootStatus:
        """Decide whether ``root`` is usable right now, blocking for as long
        as the protocol takes. Called off the event loop by
        :meth:`probe_root`, which is what callers use."""

    async def probe_root(
        self, root: str, timeout: float = PROBE_TIMEOUT_S
    ) -> RootStatus:
        """:meth:`probe_root_blocking` off the event loop, bounded.

        On timeout the shared worker thread is left to its blocked call —
        later probes of the same root reuse it — and the root is reported
        unavailable. One in-flight probe per root is what keeps a hung
        share from claiming a thread per waiting caller and draining the
        executor.
        """
        loop = asyncio.get_running_loop()
        entry = self._inflight.get(root)
        if entry is None or entry[0] is not loop or entry[1].done():
            task = loop.create_task(asyncio.to_thread(self.probe_root_blocking, root))
            self._inflight[root] = (loop, task)
            task.add_done_callback(
                lambda finished, key=root: self._forget_probe(key, finished)
            )
        else:
            task = entry[1]
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return self.unavailable(
                root, "it did not respond (hung storage?)"
            )
        except Exception as e:
            # A probe owes a verdict, not an exception: callers gather these
            # for the settings page and for the scan, and one raising would
            # take every other root's answer down with it.
            logger.exception("Probing %s failed", root)
            return self.unavailable(root, f"it could not be checked ({e})")

    def _forget_probe(
        self, root: str, task: "asyncio.Task[RootStatus]"
    ) -> None:
        """Drop a finished probe so the registry does not accumulate roots
        that have since left the configuration.

        Reads the exception too: a probe that raises after its waiter timed
        out has nobody left to receive it, and asyncio would report it from
        the garbage collector instead. Debug, because a waiter that is still
        there logs it for itself.
        """
        if self._inflight.get(root, (None, None))[1] is task:
            del self._inflight[root]
        if not task.cancelled() and task.exception() is not None:
            logger.debug("Probing %s failed: %s", root, task.exception())

    def should_defer_probe(self, root: str) -> bool:
        """Whether probing ``root`` right now would do harm rather than good.

        False for storage where a probe is only a request. Local storage
        says True while an automounter owns the path: the stat inside a
        probe is what would mount it again, which is the opposite of what a
        watcher waiting for the mount to come back on its own wants.
        """
        return False

    async def await_root_available(
        self, root: str, deadline_s: float = 6.0
    ) -> RootStatus:
        """Probe until ``root`` is available or the deadline passes.

        The probes themselves are what trigger a pending automount; the
        retries are what give slow storage its chance.
        """
        start = time.monotonic()
        status = await self.probe_root(root, timeout=deadline_s)
        for delay in _RETRY_DELAYS_S:
            if status.available:
                break
            remaining = deadline_s - (time.monotonic() - start)
            if remaining <= delay:
                break
            await asyncio.sleep(delay)
            remaining = deadline_s - (time.monotonic() - start)
            status = await self.probe_root(root, timeout=max(0.5, remaining))
        return status

    def unavailable(self, root: str, reason: str) -> RootStatus:
        """A negative verdict for ``root``, filled in the way this storage
        would have filled a positive one."""
        return RootStatus(
            root=root,
            available=False,
            reason=reason,
            fs_type=self.scheme,
            is_network=self.is_network,
            is_autofs=False,
            identity=None,
        )

    @property
    def is_network(self) -> bool:
        """Whether reaching this storage goes over the network. Local
        storage answers per root, since a mount may be either."""
        return True


    def watcher(self) -> Optional[ChangeWatcher]:
        """A watcher for roots on this storage, or None when the protocol
        cannot report changes and the periodic scan is all there is."""
        return None
