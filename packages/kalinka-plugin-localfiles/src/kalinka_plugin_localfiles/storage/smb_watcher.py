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
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import smbclient
from smbprotocol.change_notify import (
    ChangeNotifyFlags,
    CompletionFilter,
    FileAction,
    FileSystemWatcher,
)
from smbprotocol.exceptions import SMBException
from smbprotocol.open import DirectoryAccessMask

from .base import ChangeKind, ChangeWatcher, WatchResult
from .locator import is_within, parse

if TYPE_CHECKING:
    from .smb import SmbStorage

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


@dataclass
class _Watch:
    handle: object
    notification: _Notification


class SmbChangeWatcher(ChangeWatcher):
    def __init__(self, storage: SmbStorage) -> None:
        self._storage = storage
        self._watches: dict[str, _Watch] = {}
        self._rescan: set[str] = set()
        self._closed = False
        # Cancelling asyncio.to_thread does not stop the worker. Serialize
        # teardown with an in-flight poll/arm before closing its handles.
        self._lock = threading.RLock()

    @staticmethod
    def _start(handle) -> _Notification:
        notification = _Notification(handle.fd)
        notification.start(_FILTER, flags=ChangeNotifyFlags.SMB2_WATCH_TREE)
        return notification

    def watch(self, root: str) -> bool:
        with self._lock:
            if self._closed:
                return False
            if root in self._watches:
                return True
            handle = None
            try:
                locator = parse(root)
                with self._storage._as_os_error(locator):
                    handle = smbclient.open_file(
                        self._storage._unc(locator),
                        mode="rb",
                        buffering=0,
                        file_type="dir",
                        share_access="rwd",
                        desired_access=DirectoryAccessMask.FILE_LIST_DIRECTORY,
                        **self._storage._logon(locator),
                    )
                    notification = self._start(handle)
            except OSError as error:
                if handle is not None:
                    self._close_handle(handle)
                logger.debug("Could not watch %s: %s", root, error)
                return False
            self._watches[root] = _Watch(handle, notification)
            # Arm before reconciling: covers both startup's scan/watch race
            # and changes missed while a connection was down.
            self._rescan.add(root)
            return True

    def unwatch(self, root: str) -> None:
        with self._lock:
            self._rescan.discard(root)
            watch = self._watches.pop(root, None)
            if watch is None:
                return
            try:
                if not watch.notification.response_event.is_set():
                    watch.notification.cancel()
            except (OSError, SMBException):
                pass  # A dropped connection has already retired the request.
            self._close_handle(watch.handle)
            watch.notification.response_event.wait(1.0)

    @staticmethod
    def _close_handle(handle) -> None:
        try:
            handle.close()
        except (OSError, SMBException):
            logger.debug("SMB watch handle was already gone", exc_info=True)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for root in list(self._watches):
                self.unwatch(root)

    def poll(self, timeout_s: float) -> WatchResult:
        with self._lock:
            result = WatchResult(rescan=set(self._rescan))
            self._rescan.clear()
            deadline = time.monotonic() + timeout_s
            while not self._closed:
                for root, watch in list(self._watches.items()):
                    if not watch.notification.response_event.is_set():
                        continue
                    try:
                        actions = watch.notification.result
                        watch.notification = self._start(watch.handle)
                        if not actions:
                            # STATUS_NOTIFY_ENUM_DIR means details were lost,
                            # including an overflow of the server's buffer.
                            result.rescan.add(root)
                        else:
                            for action in actions:
                                self._translate(root, action, result)
                    except Exception as error:
                        self.unwatch(root)
                        result.rescan.discard(root)
                        if getattr(error, "status", None) in _UNSUPPORTED:
                            result.unsupported.add(root)
                            logger.warning(
                                "SMB notifications unsupported for %s; using scans",
                                root,
                            )
                        else:
                            # Never use unmounted: its autofs optimization
                            # can skip reconciliation when identity is unchanged.
                            result.vanished.add(root)
                            logger.warning("SMB watch lost for %s: %s", root, error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Poll the library's completion events, not the filesystem.
                # Finish the window to batch bursts, rearming throughout it.
                time.sleep(min(0.05, remaining))
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
                is_dir = self._storage.stat(path).is_dir
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
