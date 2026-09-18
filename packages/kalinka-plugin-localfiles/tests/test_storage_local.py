#!/usr/bin/env python3
"""Reading the local filesystem, mounts included.

Most music is here — a disk, a USB drive, or a share the kernel has mounted,
NFS and CIFS alike. What this storage owes the rest of the module is the
behaviour the module had before there was a storage abstraction at all: links
that cannot be used to leave a music folder, listings that do not descend
through a symlinked directory, and a listing cheap enough to run over a large
library.
"""

import os

import pytest

from kalinka_plugin_localfiles.storage.local import HAS_INOTIFY, LocalStorage
from kalinka_plugin_localfiles.storage.locator import LocatorError

LOCAL = LocalStorage()


class TestListing:
    def test_children_are_named_and_classified(self, tmp_path):
        (tmp_path / "album").mkdir()
        (tmp_path / "a.flac").write_bytes(b"x")

        entries = {e.name: e for e in LOCAL.listdir(str(tmp_path))}

        assert entries["album"].is_dir
        assert not entries["a.flac"].is_dir
        assert entries["a.flac"].path == str(tmp_path / "a.flac")

    def test_sizes_are_not_measured_up_front(self, tmp_path):
        """A stat per entry is what the pre-count pass over a large library
        cannot afford, so the size is fetched only where it is wanted."""
        (tmp_path / "a.flac").write_bytes(b"12345")
        [entry] = LOCAL.listdir(str(tmp_path))

        assert entry.size is None
        assert LOCAL.size_of(entry) == 5

    def test_a_symlinked_directory_is_not_one_to_descend(self, tmp_path):
        """Matching os.walk without followlinks: a link loop would otherwise
        walk forever."""
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "link").symlink_to(real)

        entries = {e.name: e for e in LOCAL.listdir(str(tmp_path))}
        assert entries["real"].is_dir
        assert not entries["link"].is_dir

    def test_a_broken_link_is_listed_and_falls_over_when_measured(self, tmp_path):
        """os.walk lists one too. The indexer drops it at the point it tries
        to measure it, which is where a file that went away is handled
        anyway."""
        (tmp_path / "dangling.flac").symlink_to(tmp_path / "nothing")

        [entry] = LOCAL.listdir(str(tmp_path))
        assert entry.name == "dangling.flac"
        assert not entry.is_dir
        assert not LOCAL.exists(entry.path)

    def test_a_directory_that_is_not_there_is_an_os_error(self, tmp_path):
        with pytest.raises(OSError):
            LOCAL.listdir(str(tmp_path / "nope"))


class TestMeasuring:
    def test_size_identity_and_kind(self, tmp_path):
        path = tmp_path / "a.flac"
        path.write_bytes(b"audio")
        raw = os.stat(path)

        measured = LOCAL.stat(str(path))

        assert measured.size == 5
        assert not measured.is_dir
        assert measured.mtime == int(raw.st_mtime)
        assert measured.identity.device == str(raw.st_dev)
        assert measured.identity.inode == str(raw.st_ino)

    def test_a_folder_says_so(self, tmp_path):
        assert LOCAL.stat(str(tmp_path)).is_dir

    def test_readability_is_answered_without_opening(self, tmp_path):
        path = tmp_path / "a.flac"
        path.write_bytes(b"x")
        assert LOCAL.readable(str(path))

        path.chmod(0o000)
        try:
            assert not LOCAL.readable(str(path))
        finally:
            path.chmod(0o644)


class TestTheAccessBoundary:
    def test_a_link_that_leaves_the_folder_is_outside_it(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "x.flac").write_bytes(b"x")
        (music / "link.flac").symlink_to(elsewhere / "x.flac")

        assert not LOCAL.contains(str(music / "link.flac"), [str(music)])

    def test_a_sibling_sharing_a_name_prefix_is_outside(self, tmp_path):
        assert not LOCAL.contains(
            str(tmp_path / "Music2" / "a.flac"), [str(tmp_path / "Music")]
        )

    def test_the_folder_itself_is_inside(self, tmp_path):
        assert LOCAL.contains(str(tmp_path), [str(tmp_path)])


class TestReading:
    def test_a_file_opens_for_reading(self, tmp_path):
        path = tmp_path / "a.flac"
        path.write_bytes(b"fLaC-ish")
        with LOCAL.open(str(path)) as handle:
            assert handle.read() == b"fLaC-ish"

    def test_the_file_itself_is_what_another_process_gets(self, tmp_path):
        """Nothing is copied for a local file, which is what lets the server
        hand it to the kernel and fpcalc open it by name."""
        path = tmp_path / "a.flac"
        path.write_bytes(b"x")
        assert LOCAL.local_path(str(path)) == str(path)

        with LOCAL.materialize(str(path)) as local:
            assert local == str(path)
        assert path.exists()  # nothing was cleaned up under it

    def test_a_home_relative_folder_is_canonical_once_expanded(self):
        assert LOCAL.canonical("~/Music") == os.path.expanduser("~/Music")

    def test_a_path_that_cannot_be_resolved_is_an_os_error(self):
        with pytest.raises(LocatorError):
            LOCAL.canonical("\0")


class TestChangeNotification:
    def test_the_local_filesystem_can_report_changes(self):
        watcher = LOCAL.watcher()
        if not HAS_INOTIFY:
            pytest.skip("inotify_simple is not installed")
        assert watcher is not None
        watcher.close()
