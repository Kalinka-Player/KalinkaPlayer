"""Files this machine can reach through its own filesystem.

Which is most of them: a local disk, a USB drive, and every share the kernel
has mounted, NFS and CIFS included. What makes a mount different from a disk
is only that it can go away, and :mod:`..utils.mount_status` is what tells
the two apart.

This is also the one storage that can report changes as they happen, through
inotify.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from stat import S_ISDIR
from typing import BinaryIO, Iterable, Iterator, Optional

try:
    from inotify_simple import INotify, flags

    HAS_INOTIFY = True
except ImportError:
    HAS_INOTIFY = False

from ..utils import mount_status
from .base import (
    ChangeKind,
    ChangeWatcher,
    DirEntry,
    FileIdentity,
    FileStat,
    FileStorage,
    RootStatus,
    WatchResult,
)
from .locator import FILE_SCHEME, LocatorError, is_within, root_of, scheme_of

logger = logging.getLogger(__name__.split(".")[-1])

# CLOSE_WRITE for content arriving, MOVED_TO for an atomic rename into place,
# CREATE for a new directory, DELETE/MOVED_FROM for content leaving,
# DELETE_SELF and UNMOUNT for a watched root going away.
_WATCH_MASK = (
    flags.CLOSE_WRITE
    | flags.MOVED_TO
    | flags.CREATE
    | flags.DELETE
    | flags.MOVED_FROM
    | flags.DELETE_SELF
    | flags.UNMOUNT
) if HAS_INOTIFY else 0


def _typed_children(path: str) -> Iterator[tuple[os.DirEntry, bool]]:
    """Each child of ``path`` with whether it is a directory.

    A child that will not answer is skipped rather than allowed to abandon
    its siblings: it may have vanished between the directory read and the
    question, or be one the service cannot stat. Symbolic links are reported
    as the links they are, which is what keeps a scan from descending
    through one.
    """
    with os.scandir(path) as children:
        for child in children:
            try:
                yield child, child.is_dir(follow_symlinks=False)
            except OSError:
                continue


class LocalStorage(FileStorage):
    """The local filesystem, mounts and all."""

    @property
    def scheme(self) -> str:
        return FILE_SCHEME

    @property
    def is_network(self) -> bool:
        """Answered per root by the mount probe, not by the storage: the same
        filesystem serves a local disk and an NFS share."""
        return False

    def handles(self, path: str) -> bool:
        return scheme_of(path) == FILE_SCHEME

    def canonical(self, path: str) -> str:
        try:
            return str(Path(path).expanduser().resolve())
        except (OSError, RuntimeError, ValueError) as e:
            raise LocatorError(f"{path} is not a usable path: {e}") from e

    def contains(self, path: str, roots: Iterable[str]) -> bool:
        """Symlink-aware: a link sitting under a music folder that resolves
        outside it is outside it, which is what keeps the boundary from being
        walked around."""
        if not path:
            return False
        try:
            target = str(Path(path).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            return False
        return any(is_within(target, root) for root in roots)

    def listdir(self, path: str) -> list[DirEntry]:
        """Sizes are left unreported: ``os.scandir`` caches the directory
        entry's type but not its size, so filling one in would cost a stat
        per entry on every pass over the library."""
        return [
            DirEntry(name=child.name, path=child.path, is_dir=is_dir)
            for child, is_dir in _typed_children(path)
        ]

    def stat(self, path: str) -> FileStat:
        raw = os.stat(path)
        return FileStat(
            size=raw.st_size,
            mtime_ns=raw.st_mtime_ns,
            is_dir=S_ISDIR(raw.st_mode),
            identity=FileIdentity(device=str(raw.st_dev), inode=str(raw.st_ino)),
        )

    def readable(self, path: str) -> bool:
        return os.access(path, os.R_OK)

    def open(self, path: str) -> BinaryIO:
        return open(path, "rb")

    def local_path(self, path: str) -> Optional[str]:
        return path

    def should_defer_probe(self, root: str) -> bool:
        """True while an autofs mount answers for the root: reading
        mountinfo says so without the stat that would re-trigger the
        automount whose idle expiry just unmounted it."""
        return mount_status.autofs_pending(root)

    def probe_root_blocking(self, root: str) -> RootStatus:
        return mount_status.probe_root(root)

    def watcher(self) -> Optional[ChangeWatcher]:
        if not HAS_INOTIFY:
            logger.warning(
                "inotify_simple not available, local file watching disabled. "
                "Install with: pip install inotify_simple"
            )
            return None
        return InotifyWatcher()


class InotifyWatcher(ChangeWatcher):
    """Local changes as the kernel reports them.

    Keeps itself armed as directories come and go under a watched root; the
    caller only re-arms a root this reports as lost, because an unmount or a
    deleted root is the one thing inotify cannot tell us about from inside.
    """

    def __init__(self) -> None:
        self._inotify = INotify()
        self._watched: dict[int, str] = {}
        self._roots: set[str] = set()

    def watch(self, root: str) -> bool:
        self._roots.add(root)
        return self._arm(root) > 0

    def unwatch(self, root: str) -> None:
        self._roots.discard(root)
        self._retire(root, remove_watches=True)

    def close(self) -> None:
        self._watched.clear()
        self._roots.clear()
        try:
            self._inotify.close()
        except OSError:
            logger.debug("inotify was already closed")

    def poll(self, timeout_s: float) -> WatchResult:
        # inotify_simple's timeout follows select.poll() — MILLISECONDS, not
        # seconds. A value of 1 was a busy-spin at ~1000 reads/sec.
        events = self._inotify.read(timeout=int(timeout_s * 1000))
        result = WatchResult()
        for event in events:
            directory = self._watched.get(event.wd, "")
            if not directory:
                continue
            path = (
                os.path.join(directory, event.name) if event.name else directory
            )
            self._translate(event, directory, path, result)
        return result

    def _translate(
        self, event, directory: str, path: str, result: WatchResult
    ) -> None:
        mask = event.mask
        if mask & flags.UNMOUNT:
            # The kernel already dropped every watch on the filesystem and no
            # per-file event follows, so the whole root is retired here.
            root = root_of(directory, self._roots) or directory
            self._retire(root, remove_watches=False)
            result.unmounted.add(root)
            return

        if mask & (flags.CREATE | flags.MOVED_TO) and os.path.isdir(path):
            # A directory appeared — created in place (mkdir, cp -r) or moved
            # in whole. A moved-in tree is already populated and emits no
            # per-file events, so it is watched AND scanned as a unit.
            result.changes.add((ChangeKind.DIR_ADDED, path))
            self._arm(path)
            return

        if mask & flags.CLOSE_WRITE:
            result.changes.add((ChangeKind.FILE_WRITTEN, path))
            return

        if mask & flags.MOVED_TO:
            result.changes.add((ChangeKind.FILE_MOVED_IN, path))
            return

        if mask & (flags.DELETE | flags.MOVED_FROM):
            if mask & flags.ISDIR:
                self._retire(path, remove_watches=True)
                result.changes.add((ChangeKind.DIR_REMOVED, path))
            else:
                result.changes.add((ChangeKind.PATH_REMOVED, path))
            return

        if mask & flags.DELETE_SELF:
            self._watched.pop(event.wd, None)
            if directory in self._roots:
                result.vanished.add(directory)

    def _arm(self, path: str) -> int:
        """Watch ``path`` and its subdirectories. Returns how many were
        armed, so an unreachable root reads as zero rather than as an
        error — a folder that is not there yet is an ordinary state.

        A subtree that cannot be listed still leaves ``path`` itself armed:
        losing the whole root because one directory underneath it is
        unreadable would mean losing every change below it.
        """
        try:
            descriptor = self._inotify.add_watch(path, _WATCH_MASK)
        except OSError as e:
            logger.debug("Could not watch %s: %s", path, e)
            return 0
        self._watched[descriptor] = path

        armed = 1
        try:
            for child, is_dir in _typed_children(path):
                if is_dir:
                    armed += self._arm(child.path)
        except OSError as e:
            logger.debug("Could not list %s to watch below it: %s", path, e)
        return armed

    def _retire(self, path: str, *, remove_watches: bool) -> None:
        """Forget ``path`` and its subtree. ``remove_watches`` is False after
        an unmount, where the descriptors are already gone."""
        for descriptor, watched in list(self._watched.items()):
            if not is_within(watched, path):
                continue
            del self._watched[descriptor]
            if not remove_watches:
                continue
            try:
                self._inotify.rm_watch(descriptor)
            except OSError:
                logger.debug("Watch on %s was already gone", watched)
