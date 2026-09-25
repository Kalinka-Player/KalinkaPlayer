"""Which folders and files are somebody's housekeeping rather than music."""

import pytest

from kalinka_plugin_localfiles.housekeeping import (
    in_housekeeping,
    is_hidden_file,
    is_housekeeping_dir,
)
from kalinka_plugin_localfiles.indexer.indexer import is_supported_audio_file


@pytest.mark.parametrize(
    "name",
    [
        "#recycle",
        "#snapshot",
        "@eaDir",
        "@Recycle",
        "@Recently-Snapshot",
        "$RECYCLE.BIN",
        "$Recycle.Bin",
        "System Volume Information",
        "lost+found",
        ".Trash-1000",
        ".snapshot",
        ".zfs",
        ".recycle",
        ".AppleDouble",
    ],
)
def test_a_bin_a_snapshot_or_a_cache_is_housekeeping(name):
    assert is_housekeeping_dir(name)


@pytest.mark.parametrize(
    "name",
    ["#1 Record", "@ Home", "$uicideboy$", "Recycle", "Snapshot of Summer", "Bach"],
)
def test_music_named_like_it_is_not(name):
    """Big Star's #1 Record and $uicideboy$ are folders people keep music in."""
    assert not is_housekeeping_dir(name)


def test_a_resource_fork_is_hidden():
    assert is_hidden_file("._01 Aria.flac")
    assert not is_supported_audio_file("smb://nas/music/Bach/._01 Aria.flac")
    assert is_supported_audio_file("smb://nas/music/Bach/01 Aria.flac")


def test_only_folders_below_the_root_are_judged():
    """A library someone keeps under a hidden folder of their own is theirs."""
    root = "/home/me/.music"
    assert not in_housekeeping(f"{root}/Bach/01 Aria.flac", root)
    assert in_housekeeping(f"{root}/#recycle/01 Aria.flac", root)


def test_a_folder_is_judged_by_its_own_name_when_asked():
    root = "smb://nas/music"
    assert in_housekeeping(f"{root}/#recycle", root, is_dir=True)
    assert not in_housekeeping(f"{root}/#recycle", root)


def test_a_path_outside_the_root_is_not_judged():
    assert not in_housekeeping("/elsewhere/#recycle/a.flac", "/srv/music")
