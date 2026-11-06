"""SMB watcher recovery and translation, including responses rare on a LAN."""

import threading
import time
from types import SimpleNamespace

import pytest
from smbprotocol.change_notify import FileAction, FileNotifyInformation
from smbprotocol.exceptions import SMBConnectionClosed, SMBResponseException
from smbprotocol.header import NtStatus, SMB2HeaderResponse

from kalinka_plugin_localfiles.storage import ChangeKind, FileStat
from kalinka_plugin_localfiles.storage import smb_watcher as module

ROOT = "smb://nas/music"


def event(action, name):
    value = FileNotifyInformation()
    value["action"] = action
    value["file_name"] = name
    return value


class Notification:
    made = []

    def __init__(self, opened):
        self.opened = opened
        self.response_event = threading.Event()
        self.actions = []
        self.error = None
        self.cancelled = False
        self.made.append(self)

    def start(self, completion_filter, flags):
        self.filter = completion_filter
        self.flags = flags

    @property
    def result(self):
        if self.error:
            raise self.error
        return self.actions

    def complete(self, *actions, error=None):
        self.actions = list(actions)
        self.error = error
        self.response_event.set()

    def cancel(self):
        self.cancelled = True
        self.response_event.set()


class Handle:
    fd = object()

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Storage:
    def __init__(self):
        self.nodes = {}

    def stat(self, path):
        node = self.nodes.get(path)
        if isinstance(node, Exception):
            raise node
        if node is None:
            raise FileNotFoundError(path)
        return FileStat(size=1, mtime_ns=0, is_dir=node == "dir")


@pytest.fixture
def armed(monkeypatch):
    handles, opens = [], []

    def open_directory(root):
        handle = Handle()
        handles.append(handle)
        opens.append(root)
        return handle

    Notification.made = []
    monkeypatch.setattr(module, "_Notification", Notification)
    storage = Storage()
    watcher = module.SmbChangeWatcher(open_directory, storage.stat)
    assert watcher.watch(ROOT)
    assert not watcher.poll(0)  # The coordinator owns startup reconciliation.
    yield watcher, storage, handles, opens
    watcher.close()


def test_watch_is_recursive_and_reuses_the_open_directory(armed):
    watcher, _, handles, opens = armed
    assert opens == [ROOT]
    assert Notification.made[0].flags == module.ChangeNotifyFlags.SMB2_WATCH_TREE
    assert watcher.watch(ROOT)
    assert len(handles) == 1


def test_nested_files_and_populated_directories_are_reported(armed):
    watcher, storage, _, _ = armed
    storage.nodes = {ROOT + "/Artist/Album": "dir", ROOT + "/Artist/曲.flac": "file"}
    Notification.made[-1].complete(
        event(FileAction.FILE_ACTION_ADDED, "Artist\\Album"),
        event(FileAction.FILE_ACTION_MODIFIED, "Artist\\曲.flac"),
    )
    result = watcher.poll(2)
    assert result.changes == {
        (ChangeKind.DIR_ADDED, ROOT + "/Artist/Album"),
        (ChangeKind.FILE_WRITTEN, ROOT + "/Artist/曲.flac"),
    }
    assert len(Notification.made) == 2  # continuously armed on the same handle


def test_rename_and_deletion_do_not_need_the_old_path_to_exist(armed):
    watcher, storage, _, _ = armed
    storage.nodes[ROOT + "/New Album"] = "dir"
    Notification.made[-1].complete(
        event(FileAction.FILE_ACTION_RENAMED_OLD_NAME, "Old Album"),
        event(FileAction.FILE_ACTION_RENAMED_NEW_NAME, "New Album"),
        event(FileAction.FILE_ACTION_REMOVED, "gone.flac"),
    )
    assert watcher.poll(2).changes == {
        (ChangeKind.PATH_REMOVED, ROOT + "/Old Album"),
        (ChangeKind.DIR_ADDED, ROOT + "/New Album"),
        (ChangeKind.PATH_REMOVED, ROOT + "/gone.flac"),
    }


def test_file_gone_before_stat_is_a_removal(armed):
    watcher, _, _, _ = armed
    Notification.made[-1].complete(event(FileAction.FILE_ACTION_ADDED, "gone.flac"))
    assert watcher.poll(2).changes == {(ChangeKind.PATH_REMOVED, ROOT + "/gone.flac")}


def test_overflow_rescans_without_closing_the_subscription(armed):
    watcher, _, handles, _ = armed
    Notification.made[-1].complete()
    assert watcher.poll(2).rescan == {ROOT}
    assert len(Notification.made) == 2
    assert not handles[0].closed


@pytest.mark.parametrize(
    "name", ["..\\escape.flac", "\\outside", "/outside", "a/../b", ""]
)
def test_untrusted_notification_paths_cannot_escape_the_root(armed, name):
    watcher, _, _, _ = armed
    Notification.made[-1].complete(event(FileAction.FILE_ACTION_ADDED, name))
    result = watcher.poll(2)
    assert not result.changes
    assert result.rescan == {ROOT}


def test_disconnect_owes_reconciliation_even_when_share_identity_is_unchanged(armed):
    watcher, _, handles, _ = armed
    Notification.made[-1].complete(error=SMBConnectionClosed("server gone"))
    result = watcher.poll(2)
    assert result.vanished == {ROOT}
    assert not result.unmounted
    assert handles[0].closed
    assert watcher.watch(ROOT)
    assert not watcher.poll(0)  # Reconnect reconciliation is also the caller's.


def test_server_without_notifications_is_not_treated_as_disconnected(armed):
    watcher, _, handles, _ = armed
    response = SMB2HeaderResponse()
    response["status"] = NtStatus.STATUS_NOT_SUPPORTED
    Notification.made[-1].complete(error=SMBResponseException(response))
    result = watcher.poll(2)
    assert result.unsupported == {ROOT}
    assert not result.vanished
    assert handles[0].closed


def test_closing_cancels_pending_requests_and_is_idempotent(armed):
    watcher, _, handles, _ = armed
    pending = Notification.made[-1]
    watcher.close()
    watcher.close()
    assert pending.cancelled
    assert handles[0].closed
    assert not watcher.watch(ROOT)
    assert not watcher.poll(0)


@pytest.mark.parametrize("blocked_operation", ["stat", "rearm", "close"])
def test_slow_network_io_does_not_extend_poll_or_block_other_roots(
    monkeypatch, blocked_operation
):
    entered, release = threading.Event(), threading.Event()
    other = "smb://another/music"
    handles = {}
    Notification.made = []
    monkeypatch.setattr(module, "_Notification", Notification)

    def block():
        entered.set()
        assert release.wait(5), "test did not release blocked network operation"

    class Directory(Handle):
        def __init__(self, root):
            super().__init__()
            self.fd = root

        def close(self):
            if self.fd == ROOT and blocked_operation == "close":
                block()
            super().close()

    def open_directory(root):
        handles[root] = Directory(root)
        return handles[root]

    def stat(path):
        if path.startswith(ROOT) and blocked_operation == "stat":
            block()
        return FileStat(size=1, mtime_ns=0, is_dir=False)

    original_start = Notification.start

    def start(notification, *args, **kwargs):
        if (
            notification.opened == ROOT and blocked_operation == "rearm"
            and sum(n.opened == ROOT for n in Notification.made) > 1
        ):
            block()
        original_start(notification, *args, **kwargs)

    monkeypatch.setattr(Notification, "start", start)
    watcher = module.SmbChangeWatcher(open_directory, stat)
    try:
        assert watcher.watch(ROOT)
        slow = Notification.made[-1]
        assert watcher.watch(other)
        healthy = Notification.made[-1]
        if blocked_operation == "close":
            slow.complete(error=SMBConnectionClosed("dropped"))
        else:
            slow.complete(event(FileAction.FILE_ACTION_MODIFIED, "slow.flac"))
        assert entered.wait(2)

        started = time.monotonic()
        assert not watcher.poll(0.02)
        assert time.monotonic() - started < 0.25
        healthy.complete(event(FileAction.FILE_ACTION_MODIFIED, "healthy.flac"))
        assert watcher.poll(0.5).changes == {
            (ChangeKind.FILE_WRITTEN, other + "/healthy.flac")
        }
        assert not release.is_set()
    finally:
        release.set()
        watcher.close()
    assert all(handle.closed for handle in handles.values())


def test_close_wakes_poll_and_joins_inflight_work_before_releasing_storage(monkeypatch):
    entered, release, polled = (threading.Event() for _ in range(3))
    Notification.made = []
    monkeypatch.setattr(module, "_Notification", Notification)
    handle = Handle()
    lookups = []

    def stat(path):
        lookups.append(path)
        entered.set()
        assert release.wait(5)
        return FileStat(size=1, mtime_ns=0, is_dir=False)

    watcher = module.SmbChangeWatcher(lambda root: handle, stat)
    assert watcher.watch(ROOT)
    Notification.made[-1].complete(
        event(FileAction.FILE_ACTION_MODIFIED, "first.flac"),
        event(FileAction.FILE_ACTION_MODIFIED, "second.flac"),
    )

    def poll():
        assert not watcher.poll(10)
        polled.set()

    polling = threading.Thread(target=poll)
    closing = threading.Thread(target=watcher.close)
    try:
        assert entered.wait(2)
        polling.start()
        closing.start()
        assert polled.wait(1)  # Shutdown wakes poll even while metadata I/O is stuck.
        assert closing.is_alive()  # It still owns the directory and connection.
        assert not handle.closed
    finally:
        release.set()
        for thread in (closing, polling):
            if thread.ident is not None:
                thread.join(2)
        watcher.close()
    assert handle.closed
    assert lookups == [ROOT + "/first.flac"]
    assert not watcher.poll(0)  # No late publication after shutdown.


def test_shutdown_during_directory_open_retires_the_late_handle(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    Notification.made = []
    monkeypatch.setattr(module, "_Notification", Notification)
    handle = Handle()

    def open_directory(root):
        entered.set()
        assert release.wait(5)
        return handle

    watcher = module.SmbChangeWatcher(open_directory, lambda path: None)
    armed = []
    arming = threading.Thread(target=lambda: armed.append(watcher.watch(ROOT)))
    closing = threading.Thread(target=watcher.close)
    arming.start()
    try:
        assert entered.wait(2)
        closing.start()
        # close wakes poll immediately, even though opening has not returned.
        assert not watcher.poll(2)
        assert closing.is_alive()
    finally:
        release.set()
        arming.join(2)
        if closing.ident is not None:
            closing.join(2)
        watcher.close()
    assert armed == [False]
    assert handle.closed
    assert not Notification.made


def test_protocol_helper_surfaces_receiver_error_with_a_pending_response():
    # Exercise the real smbprotocol helper's error ordering, without a server.
    watcher = module._Notification(object())
    response = SMB2HeaderResponse()
    response["status"] = NtStatus.STATUS_PENDING
    watcher._request = SimpleNamespace(response=response, cancelled=False)
    watcher._t_exc = SMBConnectionClosed("dropped while pending")
    with pytest.raises(SMBConnectionClosed):
        _ = watcher.result
