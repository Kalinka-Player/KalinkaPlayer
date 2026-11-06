"""SMB change notifications on the same sessions used to read a source.

One open directory and one outstanding request per root. Requests are
one-shot: re-arm before handing a batch to the indexer, keeping the handle
open so the server can buffer changes between requests. A recursive watch
is cheap, but some Samba servers only report server-local writes in the
opened directory; periodic scans remain the reconciliation mechanism.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol

from smbprotocol.change_notify import (
    ChangeNotifyFlags,
    CompletionFilter,
    FileAction,
    FileSystemWatcher,
)
from smbprotocol.exceptions import SMBException
from smbprotocol.open import Open

from .base import ChangeKind, ChangeWatcher, FileStat, WatchResult
from .locator import is_within

logger = logging.getLogger(__name__)

_FILTER = (
    CompletionFilter.FILE_NOTIFY_CHANGE_FILE_NAME
    | CompletionFilter.FILE_NOTIFY_CHANGE_DIR_NAME
    | CompletionFilter.FILE_NOTIFY_CHANGE_SIZE
    | CompletionFilter.FILE_NOTIFY_CHANGE_LAST_WRITE
    | CompletionFilter.FILE_NOTIFY_CHANGE_ATTRIBUTES
)
_UNSUPPORTED = {
    0xC00000BB,
    0xC0000002,
    0xC0000010,
}  # not supported/implemented/device request
_REMOVED = {FileAction.FILE_ACTION_REMOVED, FileAction.FILE_ACTION_RENAMED_OLD_NAME}
_ARRIVED = {FileAction.FILE_ACTION_ADDED, FileAction.FILE_ACTION_RENAMED_NEW_NAME}


class _Notification(FileSystemWatcher):
    @property
    def result(self):
        # smbprotocol 1.x checks STATUS_PENDING before its receiver's error.
        # A disconnected pending watch consequently returns None forever.
        # Read only after response_event fires, when the receiver is finished.
        if self._t_exc is not None:
            raise self._t_exc
        return super().result


class WatchDirectory(Protocol):
    """An opened SMB directory, supplied by the storage's session owner."""

    @property
    def fd(self) -> Open: ...

    def close(self) -> None: ...


@dataclass
class _Watch:
    ready: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    armed: bool = False
    worker: threading.Thread = field(init=False)


class SmbChangeWatcher(ChangeWatcher):
    """Translate notifications independently for each root.

    Directory opening and metadata lookup are injected; credentials and
    connection pools belong to storage. Each subscription owns its network
    work and teardown. Poll only waits for completed batches, so a slow
    server cannot extend its timeout or hold up another root's events.
    """

    def __init__(
        self,
        open_directory: Callable[[str], WatchDirectory],
        stat: Callable[[str], FileStat],
    ) -> None:
        self._open_directory = open_directory
        self._stat = stat
        self._watches: dict[str, _Watch] = {}
        self._pending: dict[str, WatchResult] = {}
        self._closed = False
        # Never hold this condition across network I/O or a worker join.
        self._condition = threading.Condition()

    @staticmethod
    def _start(handle: WatchDirectory) -> _Notification:
        notification = _Notification(handle.fd)
        notification.start(_FILTER, flags=ChangeNotifyFlags.SMB2_WATCH_TREE)
        return notification

    def watch(self, root: str) -> bool:
        with self._condition:
            if self._closed:
                return False
            watch = self._watches.get(root)
            if watch is None:
                watch = _Watch()
                watch.worker = threading.Thread(
                    target=self._run, args=(root, watch),
                    name="smb-change-watch", daemon=True,
                )
                self._watches[root] = watch
                try:
                    watch.worker.start()
                except Exception:
                    self._watches.pop(root)
                    raise
        # Arming may perform network I/O; unlike poll it has no deadline.
        watch.ready.wait()
        return watch.armed and not watch.stop.is_set()

    def _run(self, root: str, watch: _Watch) -> None:
        handle = notification = None
        failure = WatchResult()
        try:
            handle = self._open_directory(root)
            if watch.stop.is_set():
                return
            notification = self._start(handle)
            watch.armed = True
            watch.ready.set()
            while not watch.stop.is_set():
                if not notification.response_event.wait(0.05):
                    continue
                if watch.stop.is_set():
                    break
                actions = notification.result
                notification = self._start(handle)
                result = WatchResult()
                if not actions:
                    # STATUS_NOTIFY_ENUM_DIR: the server lost event details.
                    result.rescan.add(root)
                else:
                    for action in actions:
                        if watch.stop.is_set():
                            break
                        self._translate(root, action, result)
                with self._condition:
                    if not watch.stop.is_set():
                        self._publish(root, result)
        except Exception as error:
            if not watch.stop.is_set():
                if not watch.armed:
                    logger.debug("Could not watch %s: %s", root, error)
                elif getattr(error, "status", None) in _UNSUPPORTED:
                    failure.unsupported.add(root)
                    logger.warning(
                        "SMB notifications unsupported for %s; using scans", root
                    )
                else:
                    # Never use unmounted: its autofs optimization can skip
                    # reconciliation when the share's identity is unchanged.
                    failure.vanished.add(root)
                    logger.warning("SMB watch lost for %s: %s", root, error)
        finally:
            # The subscription owns its handles until all I/O has finished.
            # Closing storage before joining this worker could let a late
            # metadata lookup recreate a connection in an already closed pool.
            try:
                if notification is not None:
                    try:
                        if not notification.response_event.is_set():
                            notification.cancel()
                    except (OSError, SMBException):
                        pass
                if handle is not None:
                    try:
                        handle.close()
                    except (OSError, SMBException):
                        logger.debug("SMB watch handle was already gone", exc_info=True)
                if notification is not None:
                    notification.response_event.wait(1.0)
            finally:
                with self._condition:
                    self._watches.pop(root, None)
                    if not watch.stop.is_set():
                        self._publish(root, failure)
                    watch.ready.set()

    def _publish(self, root: str, result: WatchResult) -> None:
        """Merge under the condition; bound memory while the consumer is busy."""
        if not result:
            return
        pending = self._pending.setdefault(root, WatchResult())
        pending.changes.update(result.changes)
        pending.rescan.update(result.rescan)
        pending.vanished.update(result.vanished)
        pending.unsupported.update(result.unsupported)
        if len(pending.changes) > 10000:
            pending.changes.clear()
            pending.rescan.add(root)
        if pending.vanished or pending.unsupported:
            pending.rescan.discard(root)
        self._condition.notify_all()

    def unwatch(self, root: str) -> None:
        with self._condition:
            watch = self._watches.get(root)
            if watch is not None:
                watch.stop.set()
            self._pending.pop(root, None)
        if watch is not None:
            watch.worker.join()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            watches = list(self._watches.values())
            for watch in watches:
                watch.stop.set()
            self._pending.clear()
            self._condition.notify_all()
        # Join every subscription before its storage's connection pool closes.
        # This may wait for in-flight network I/O; poll remains interruptible.
        for watch in watches:
            watch.worker.join()

    def poll(self, timeout_s: float) -> WatchResult:
        with self._condition:
            self._condition.wait_for(
                lambda: self._pending or self._closed, timeout=max(0, timeout_s)
            )
            batches, self._pending = self._pending, {}
        result = WatchResult()
        for batch in batches.values():
            result.changes.update(batch.changes)
            result.rescan.update(batch.rescan)
            result.vanished.update(batch.vanished)
            result.unsupported.update(batch.unsupported)
        return result

    def _translate(self, root: str, action, result: WatchResult) -> None:
        name = action["file_name"].get_value().replace("\\", "/")
        parts = name.split("/")
        if not name or any(part in ("", ".", "..") for part in parts):
            result.rescan.add(root)
            return
        path = root.rstrip("/") + "/" + name
        if not is_within(path, root):
            result.rescan.add(root)
            return
        kind = action["action"].get_value()
        if kind in _REMOVED:
            # FILE_NOTIFY_INFORMATION has no directory/type bit. The path
            # is already gone, so callers must accept removals without an
            # audio extension and reconcile descendants as well as the file.
            result.changes.add((ChangeKind.PATH_REMOVED, path))
        elif kind in _ARRIVED or kind == FileAction.FILE_ACTION_MODIFIED:
            try:
                is_dir = self._stat(path).is_dir
            except FileNotFoundError:
                result.changes.add((ChangeKind.PATH_REMOVED, path))
                return
            except OSError:
                # A rename can briefly expose a server-internal name which
                # cannot be opened. Reconcile without losing a healthy watch.
                result.rescan.add(root)
                return
            if is_dir:
                if kind in _ARRIVED:
                    result.changes.add((ChangeKind.DIR_ADDED, path))
            else:
                change = (
                    ChangeKind.FILE_MOVED_IN
                    if kind == FileAction.FILE_ACTION_RENAMED_NEW_NAME
                    else ChangeKind.FILE_WRITTEN
                )
                result.changes.add((change, path))
