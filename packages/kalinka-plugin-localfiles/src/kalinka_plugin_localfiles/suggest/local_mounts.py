"""Drives plugged into this machine, offered as music folders.

A USB disk arrives at a mount point nobody has told the user about, and
``/proc/self/mountinfo`` is the only place it is written down. Everything
here reads that file and nothing else: the answer must be instant, and
listing a sleeping disk to see whether it holds music is not.
"""

from __future__ import annotations

import os

from kalinka_plugin_sdk import ConfigOption

from ..utils.mount_status import Mount, is_network_fs, list_mounts

#: Where a desktop, a removable-media rule or a hand-written fstab entry puts
#: a disk it does not own. A mount *at* one of these is the directory itself,
#: not a drive.
_MEDIA_ROOTS = ("/media", "/mnt", "/run/media")

#: Block devices a music library plausibly sits on. Excludes the loop and
#: device-mapper nodes that back containers and snaps.
_DISK_SOURCES = ("/dev/sd", "/dev/nvme", "/dev/mmcblk", "/dev/hd")

#: Paths that are the operating system's own, with everything below them.
#: ``/`` is not among them: it is system property itself, but what is
#: mounted below it is exactly what this module is looking for.
_SYSTEM_MOUNTS = ("/boot",)


def _under_media_root(mount_point: str) -> bool:
    return any(
        mount_point.startswith(root + os.sep) for root in _MEDIA_ROOTS
    )


def _is_system_mount(mount_point: str) -> bool:
    return mount_point == os.sep or any(
        mount_point == m or mount_point.startswith(m + os.sep)
        for m in _SYSTEM_MOUNTS
    )


def _is_within(path: str, ancestor: str) -> bool:
    return path != ancestor and (
        ancestor == os.sep or path.startswith(ancestor.rstrip(os.sep) + os.sep)
    )


def _is_bind_mount(mount: Mount, mounts: list[Mount]) -> bool:
    """Whether this is a second way in to a subtree already mounted.

    systemd hands a sandboxed service a namespace full of them — ``/etc``,
    ``/usr``, its own state directories — each carrying the source of the
    disk it came off, and so indistinguishable from a drive by source
    alone. A drive is nobody's subtree, and neither is a btrfs subvolume
    mounted in its own right.
    """
    return any(
        other.device == mount.device and _is_within(mount.fs_root, other.fs_root)
        for other in mounts
    )


def _has_real_backing(mount: Mount) -> bool:
    """Whether a disk, a server, or an automounter standing in for one is
    what answers for this mount. Keeps out the tmpfs, overlay and squashfs
    entries that a container runtime or a snap leaves under ``/mnt``."""
    return (
        mount.fs_type == "autofs"
        or is_network_fs(mount.fs_type)
        or mount.source.startswith(_DISK_SOURCES)
    )


def _is_offerable(mount: Mount, mounts: list[Mount]) -> bool:
    if _is_system_mount(mount.mount_point):
        return False
    if _is_bind_mount(mount, mounts):
        return False
    if _under_media_root(mount.mount_point):
        return _has_real_backing(mount)
    return mount.source.startswith(_DISK_SOURCES)


def _describe(mount: Mount) -> str:
    if mount.fs_type == "autofs":
        return "mounted on demand"
    return f"{mount.fs_type} · {mount.source}"


def mount_options(mounts: list[Mount] | None = None) -> list[ConfigOption]:
    """Every mount point worth offering, in path order.

    Later mountinfo rows stack on earlier ones, so a point that appears
    twice is offered once — the suggestion is the path, and the path is the
    same whichever filesystem currently answers for it.
    """
    if mounts is None:
        mounts = list_mounts()
    described: dict[str, str] = {}
    for mount in mounts:
        if _is_offerable(mount, mounts):
            described[mount.mount_point] = _describe(mount)
    return [
        ConfigOption(value=point, label=point, description=description)
        for point, description in sorted(described.items())
    ]


class LocalMountSuggester:
    """Music folders on disks attached to this machine.

    @note Reads mountinfo on every call rather than caching it: the file is
        a few kilobytes of kernel memory, and a cache would have to be
        invalidated by exactly the event this exists to notice.
    """

    def options(self) -> list[ConfigOption]:
        return mount_options()

    def refresh(self) -> None:
        """Nothing to wait for — :meth:`options` is already current."""
