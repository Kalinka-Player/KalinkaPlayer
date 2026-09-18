#!/usr/bin/env python3
"""Reporting local changes as the kernel sees them.

The watcher is the one part of the storage layer with state that outlives a
call: which directories are armed, and which roots it has lost. Both are
answered here against a real inotify instance rather than a double, because
what is worth pinning down is the kernel's behaviour — that a moved-in tree
arrives as one event, that an unmount drops every watch at once — and a
double would only restate the code.
"""

import os
from contextlib import contextmanager

import pytest

import kalinka_plugin_localfiles.storage.local as local_mod
from kalinka_plugin_localfiles.storage.base import ChangeKind
from kalinka_plugin_localfiles.storage.local import HAS_INOTIFY, LocalStorage

pytestmark = pytest.mark.skipif(
    not HAS_INOTIFY, reason="inotify_simple is not installed"
)

#: Long enough for the kernel to have queued what the test just did.
_SETTLE_S = 0.5


class _WillNotSay:
    """A directory entry that refuses to report its type, as one that is
    removed between the directory read and the question does."""

    def __init__(self, path):
        self.name = os.path.basename(path)
        self.path = path

    def is_dir(self, follow_symlinks=True):
        raise OSError("vanished mid-listing")


@pytest.fixture
def watcher():
    made = LocalStorage().watcher()
    yield made
    made.close()


def _drain(watcher, timeout_s=_SETTLE_S):
    """Every change the kernel has queued, over one or two reads.

    A single directory operation can land in two batches, so this keeps
    reading until a poll comes back empty.
    """
    changes, unmounted, vanished = set(), set(), set()
    while True:
        result = watcher.poll(timeout_s)
        if not result:
            return changes, unmounted, vanished
        changes |= result.changes
        unmounted |= result.unmounted
        vanished |= result.vanished
        timeout_s = 0.1


class TestArming:
    def test_a_root_and_everything_under_it(self, tmp_path, watcher):
        (tmp_path / "Albums" / "Roxy Music").mkdir(parents=True)
        (tmp_path / "Singles").mkdir()

        assert watcher.watch(str(tmp_path))
        # A file written in the deepest directory proves it was armed too.
        (tmp_path / "Albums" / "Roxy Music" / "a.flac").write_bytes(b"x")
        changes, _, _ = _drain(watcher)

        assert (
            ChangeKind.FILE_WRITTEN,
            str(tmp_path / "Albums" / "Roxy Music" / "a.flac"),
        ) in changes

    def test_a_folder_that_is_not_there_is_not_an_error(self, tmp_path, watcher):
        """A music folder on a share that has not come up yet is an ordinary
        state, and the caller retries it rather than treating it as broken."""
        assert not watcher.watch(str(tmp_path / "absent"))

    def test_one_unreadable_child_does_not_cost_its_siblings(
        self, tmp_path, watcher, monkeypatch
    ):
        """The per-child question used to sit inside the try that armed the
        whole subtree, so a child that would not answer abandoned every
        directory listed after it — silently, and with no path back, because
        the root itself had armed and so never counted as lost.

        The hostile entry is forced to come first because ``os.scandir``
        returns children in no particular order, and a test that let it land
        last would pass against the defect.
        """
        for name in ["a", "b", "c"]:
            (tmp_path / name).mkdir()

        real_scandir = os.scandir

        @contextmanager
        def hostile_first(path):
            with real_scandir(path) as entries:
                children = sorted(entries, key=lambda e: e.name)
            if os.path.samefile(path, tmp_path):
                children.insert(0, _WillNotSay(os.path.join(path, "gone")))
            yield children

        monkeypatch.setattr(local_mod.os, "scandir", hostile_first)
        assert watcher.watch(str(tmp_path))
        monkeypatch.undo()

        for name in ["a", "b", "c"]:
            (tmp_path / name / "late.flac").write_bytes(b"x")
        changes, _, _ = _drain(watcher)

        assert {
            (ChangeKind.FILE_WRITTEN, str(tmp_path / name / "late.flac"))
            for name in ["a", "b", "c"]
        } <= changes

    def test_a_link_to_a_folder_is_not_descended(self, tmp_path, watcher):
        """Following one would index the target twice, under two paths."""
        target = tmp_path / "outside"
        (target / "deep").mkdir(parents=True)
        root = tmp_path / "music"
        root.mkdir()
        (root / "link").symlink_to(target)

        watcher.watch(str(root))
        (target / "deep" / "a.flac").write_bytes(b"x")
        # Written in the root as well, so an empty result cannot pass for
        # the watcher simply being inert.
        (root / "here.flac").write_bytes(b"x")
        changes, _, _ = _drain(watcher)

        assert (ChangeKind.FILE_WRITTEN, str(root / "here.flac")) in changes
        assert not any("deep" in path for _, path in changes)


class TestWhatItReports:
    def test_a_file_finished_being_written(self, tmp_path, watcher):
        watcher.watch(str(tmp_path))
        with open(tmp_path / "a.flac", "wb") as handle:
            handle.write(b"x")
        changes, _, _ = _drain(watcher)
        assert (ChangeKind.FILE_WRITTEN, str(tmp_path / "a.flac")) in changes

    def test_a_new_folder_is_armed_as_well_as_announced(self, tmp_path, watcher):
        watcher.watch(str(tmp_path))
        (tmp_path / "New Album").mkdir()
        _drain(watcher)

        (tmp_path / "New Album" / "a.flac").write_bytes(b"x")
        changes, _, _ = _drain(watcher)
        assert (
            ChangeKind.FILE_WRITTEN,
            str(tmp_path / "New Album" / "a.flac"),
        ) in changes

    def test_a_folder_moved_in_whole(self, tmp_path, watcher):
        """A moved-in tree is already populated and emits no per-file events,
        so the directory itself has to be reported for scanning."""
        staging = tmp_path / "staging"
        (staging / "Album").mkdir(parents=True)
        (staging / "Album" / "a.flac").write_bytes(b"x")
        root = tmp_path / "music"
        root.mkdir()

        watcher.watch(str(root))
        os.rename(staging / "Album", root / "Album")
        changes, _, _ = _drain(watcher)

        assert (ChangeKind.DIR_ADDED, str(root / "Album")) in changes

    def test_a_file_removed(self, tmp_path, watcher):
        path = tmp_path / "a.flac"
        path.write_bytes(b"x")
        watcher.watch(str(tmp_path))
        path.unlink()
        changes, _, _ = _drain(watcher)
        assert (ChangeKind.PATH_REMOVED, str(path)) in changes

    def test_a_folder_removed(self, tmp_path, watcher):
        gone = tmp_path / "Album"
        gone.mkdir()
        watcher.watch(str(tmp_path))
        gone.rmdir()
        changes, _, _ = _drain(watcher)
        assert (ChangeKind.DIR_REMOVED, str(gone)) in changes

    def test_a_watched_root_being_deleted(self, tmp_path, watcher):
        """Reported as vanished rather than as a change: the caller owes it a
        rescan whenever it comes back, and nothing under it can be trusted
        meanwhile."""
        root = tmp_path / "music"
        root.mkdir()
        watcher.watch(str(root))
        root.rmdir()
        _, _, vanished = _drain(watcher)
        assert str(root) in vanished


class TestRetiring:
    def test_an_unwatched_root_stops_reporting(self, tmp_path, watcher):
        watcher.watch(str(tmp_path))
        watcher.unwatch(str(tmp_path))
        (tmp_path / "a.flac").write_bytes(b"x")
        changes, _, _ = _drain(watcher, 0.2)
        assert not changes

    def test_closing_twice_is_allowed(self, tmp_path):
        made = LocalStorage().watcher()
        made.watch(str(tmp_path))
        made.close()
        made.close()
