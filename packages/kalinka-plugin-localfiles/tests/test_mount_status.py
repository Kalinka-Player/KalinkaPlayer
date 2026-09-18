#!/usr/bin/env python3
"""Mount awareness: mountinfo parsing and the root availability probe.

The probe's job is to tell an unmounted network share apart from a deleted
library — the distinction every purge and playback guard hangs off. Bounding
and retrying it is every storage's business rather than the local mount's, so
that lives with the storage interface and is covered by
``test_storage_availability``.
"""

from kalinka_plugin_localfiles.storage import RootStatus, root_of
from kalinka_plugin_localfiles.utils.mount_status import (
    Mount,
    autofs_pending,
    covering_mount,
    list_mounts,
    probe_root,
)

# A realistic mountinfo snapshot: root fs, an autofs-managed NFS share that
# is currently mounted (two rows, nfs stacked on autofs), an autofs mount
# whose share is NOT mounted, and a mount point holding escaped spaces.
MOUNTINFO = "\n".join(
    [
        "36 25 0:32 / / rw,relatime shared:1 - ext4 /dev/sda1 rw",
        "40 36 0:38 / /mnt/nas rw,relatime - autofs /etc/auto.nas rw,fd=6,timeout=300",
        "52 40 0:44 / /mnt/nas rw,relatime - nfs4 192.168.1.5:/export rw,vers=4.2",
        "44 36 0:39 / /mnt/media rw,relatime - autofs /etc/auto.media rw,timeout=300",
        "48 36 8:17 / /path\\040with\\040space rw,relatime - ext4 /dev/sdb1 rw",
    ]
)


def _mounts():
    return list_mounts(MOUNTINFO)


def test_list_mounts_parses_type_source_and_escapes():
    mounts = _mounts()
    assert Mount(mount_point="/", fs_type="ext4", source="/dev/sda1") in mounts
    assert (
        Mount(mount_point="/mnt/nas", fs_type="nfs4", source="192.168.1.5:/export")
        in mounts
    )
    assert any(m.mount_point == "/path with space" for m in mounts)


def test_covering_mount_prefers_longest_then_latest():
    mounts = _mounts()
    # Same mount point twice: the later (stacked-on-top) row wins.
    assert covering_mount("/mnt/nas/music/a.mp3", mounts).fs_type == "nfs4"
    assert covering_mount("/mnt/media/music", mounts).fs_type == "autofs"
    assert covering_mount("/home/user/Music", mounts).fs_type == "ext4"


def test_covering_mount_matches_whole_components_only():
    mounts = _mounts()
    # /mnt/nas2 shares a string prefix with /mnt/nas but is not under it.
    assert covering_mount("/mnt/nas2/a.mp3", mounts).fs_type == "ext4"


def test_autofs_pending_reads_the_covering_type():
    mounts = _mounts()
    assert autofs_pending("/mnt/media/music", mounts)
    assert not autofs_pending("/mnt/nas/music", mounts)
    assert not autofs_pending("/home/user", mounts)


def test_root_of_matches_textually():
    roots = ["/mnt/nas/music", "/srv/media"]
    assert root_of("/mnt/nas/music/a/b.mp3", roots) == "/mnt/nas/music"
    assert root_of("/mnt/nas/music", roots) == "/mnt/nas/music"
    assert root_of("/mnt/nas/music2/b.mp3", roots) is None
    assert root_of("/elsewhere/b.mp3", roots) is None


def test_probe_available_nonempty_folder(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"x")
    status = probe_root(str(tmp_path))
    assert status.available
    assert not status.empty
    assert status.reason == ""


def test_probe_available_but_empty_folder(tmp_path):
    status = probe_root(str(tmp_path))
    assert status.available
    assert status.empty


def test_probe_missing_folder_is_unavailable(tmp_path):
    status = probe_root(str(tmp_path / "gone"))
    assert not status.available
    assert status.reason


def test_probe_autofs_pending_root_is_unavailable(tmp_path):
    # An injected autofs row covering the (real) folder simulates an expired
    # automount: the directory stats fine, but nothing is mounted on it.
    mounts = [
        Mount(mount_point="/", fs_type="ext4", source="/dev/sda1"),
        Mount(mount_point=str(tmp_path), fs_type="autofs", source="/etc/auto.x"),
    ]
    status = probe_root(str(tmp_path), mounts)
    assert not status.available
    assert status.is_autofs
    assert "automounter" in status.reason


def test_probe_classifies_mounted_network_share(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"x")
    mounts = [
        Mount(mount_point="/", fs_type="ext4", source="/dev/sda1"),
        Mount(mount_point=str(tmp_path), fs_type="autofs", source="/etc/auto.x"),
        Mount(mount_point=str(tmp_path), fs_type="nfs4", source="host:/export"),
    ]
    status = probe_root(str(tmp_path), mounts)
    assert status.available
    assert status.is_network
    assert status.is_autofs
    assert status.identity == "nfs4 host:/export"


def test_format_root_status_recommends_for_autofs_share():
    from kalinka_plugin_localfiles.module_setup import _format_root_status

    status = RootStatus(
        root="/mnt/nas/music",
        available=True,
        empty=False,
        reason="",
        fs_type="nfs4",
        is_network=True,
        is_autofs=True,
    )
    text = _format_root_status(status, None, 15)
    assert "**Available**" in text
    assert "nfs4 network share" in text
    assert "autofs" in text
    assert "every 15 min" in text


def test_format_root_status_names_the_reason_when_unavailable():
    from kalinka_plugin_localfiles.module_setup import _format_root_status

    status = RootStatus(
        root="/mnt/nas/music",
        available=False,
        empty=True,
        reason="the automounter has not mounted it",
        fs_type="autofs",
        is_network=False,
        is_autofs=True,
    )
    text = _format_root_status(status, None, 15)
    assert "**Not available**" in text
    assert "automounter" in text


def test_format_root_status_flags_a_mount_identity_mismatch():
    from kalinka_plugin_localfiles.module_setup import _format_root_status

    # The mountpoint answers as a plain local dir while the library was
    # indexed from an NFS share: a silently unmounted static mount.
    status = RootStatus(
        root="/mnt/nas/music",
        available=True,
        empty=False,
        reason="",
        fs_type="ext4",
        is_network=False,
        is_autofs=False,
        identity="ext4 /dev/sda1",
    )
    text = _format_root_status(status, "nfs4 192.168.1.5:/export", 15)
    assert "**Not available**" in text
    assert "nfs4 192.168.1.5:/export" in text
