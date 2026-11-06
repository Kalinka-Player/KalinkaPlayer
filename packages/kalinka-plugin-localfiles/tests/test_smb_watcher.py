"""SMB watcher recovery and translation, including responses rare on a LAN."""

import threading
from contextlib import nullcontext
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

    def _as_os_error(self, locator):
        return nullcontext()

    def _logon(self, locator):
        return {"connection_cache": {"existing": True}, "username": "reader"}

    def _unc(self, locator):
        return "\\\\nas\\music"

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

    def open_file(*args, **kwargs):
        handle = Handle()
        handles.append(handle)
        opens.append(kwargs)
        return handle

    Notification.made = []
    monkeypatch.setattr(module, "_Notification", Notification)
    monkeypatch.setattr(module.smbclient, "open_file", open_file)
    storage = Storage()
    watcher = module.SmbChangeWatcher(storage)
    assert watcher.watch(ROOT)
    assert watcher.poll(0).rescan == {ROOT}
    yield watcher, storage, handles, opens
    watcher.close()


def test_watch_reuses_login_and_does_not_lock_writers(armed):
    watcher, _, handles, opens = armed
    assert opens[0]["share_access"] == "rwd"
    assert opens[0]["file_type"] == "dir"
    assert opens[0]["username"] == "reader"
    assert opens[0]["connection_cache"] == {"existing": True}
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
    result = watcher.poll(0)
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
    assert watcher.poll(0).changes == {
        (ChangeKind.PATH_REMOVED, ROOT + "/Old Album"),
        (ChangeKind.DIR_ADDED, ROOT + "/New Album"),
        (ChangeKind.PATH_REMOVED, ROOT + "/gone.flac"),
    }


def test_file_gone_before_stat_is_a_removal(armed):
    watcher, _, _, _ = armed
    Notification.made[-1].complete(event(FileAction.FILE_ACTION_ADDED, "gone.flac"))
    assert watcher.poll(0).changes == {(ChangeKind.PATH_REMOVED, ROOT + "/gone.flac")}


def test_overflow_rescans_without_closing_the_subscription(armed):
    watcher, _, handles, _ = armed
    Notification.made[-1].complete()
    assert watcher.poll(0).rescan == {ROOT}
    assert len(Notification.made) == 2
    assert not handles[0].closed


@pytest.mark.parametrize(
    "name", ["..\\escape.flac", "\\outside", "/outside", "a/../b", ""]
)
def test_untrusted_notification_paths_cannot_escape_the_root(armed, name):
    watcher, _, _, _ = armed
    Notification.made[-1].complete(event(FileAction.FILE_ACTION_ADDED, name))
    result = watcher.poll(0)
    assert not result.changes
    assert result.rescan == {ROOT}


def test_disconnect_owes_reconciliation_even_when_share_identity_is_unchanged(armed):
    watcher, _, handles, _ = armed
    Notification.made[-1].complete(error=SMBConnectionClosed("server gone"))
    result = watcher.poll(0)
    assert result.vanished == {ROOT}
    assert not result.unmounted
    assert handles[0].closed
    assert watcher.watch(ROOT)
    assert watcher.poll(0).rescan == {ROOT}


def test_server_without_notifications_is_not_treated_as_disconnected(armed):
    watcher, _, handles, _ = armed
    response = SMB2HeaderResponse()
    response["status"] = NtStatus.STATUS_NOT_SUPPORTED
    Notification.made[-1].complete(error=SMBResponseException(response))
    result = watcher.poll(0)
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


def test_protocol_helper_surfaces_receiver_error_with_a_pending_response():
    # Exercise the real smbprotocol helper's error ordering, without a server.
    watcher = module._Notification(object())
    response = SMB2HeaderResponse()
    response["status"] = NtStatus.STATUS_PENDING
    watcher._request = SimpleNamespace(response=response, cancelled=False)
    watcher._t_exc = SMBConnectionClosed("dropped while pending")
    with pytest.raises(SMBConnectionClosed):
        _ = watcher.result
