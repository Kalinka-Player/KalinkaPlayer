#!/usr/bin/env python3
"""A music root that bounces (autofs idle expiry) and returns with the mount
identity the library was indexed from re-arms its watches without the full
rescan — however long it was gone, since remote changes are invisible to
inotify anyway and the periodic scan covers them. A changed identity (media
swap), a missing recorded identity, a deleted root, or startup deferral
still owes a rescan.
"""

from kalinka_plugin_localfiles.indexer.indexer import rearm_needs_rescan
from kalinka_plugin_localfiles.storage import RootStatus

NFS = "nfs4 192.168.1.5:/export/music"


def _status(identity, legacy=None):
    return RootStatus(
        root="/m", available=True, reason="", fs_type=None, is_network=False,
        is_autofs=False, identity=identity, legacy_identity=legacy,
    )


def test_unmount_bounce_with_matching_identity_skips_rescan():
    assert rearm_needs_rescan(True, _status(NFS), NFS) is False


def test_unconditional_rescan_without_a_seen_unmount():
    # Startup deferral / deleted root.
    assert rearm_needs_rescan(False, _status(NFS), NFS) is True


def test_changed_identity_rescans():
    assert rearm_needs_rescan(True, _status("vfat uuid:3A1F-9C2B", "vfat /dev/sda1"), NFS) is True


def test_missing_stored_signature_rescans():
    assert rearm_needs_rescan(True, _status(NFS), None) is True
    assert rearm_needs_rescan(True, _status(None), NFS) is True


def test_a_drive_back_on_another_device_node_is_the_same_drive():
    replugged = _status("vfat uuid:3A1F-9C2B", "vfat /dev/sdb1")

    assert rearm_needs_rescan(True, replugged, "vfat uuid:3A1F-9C2B") is False


def test_a_library_recorded_before_uuids_still_matches():
    same_node = _status("vfat uuid:3A1F-9C2B", "vfat /dev/sda1")

    assert rearm_needs_rescan(True, same_node, "vfat /dev/sda1") is False
