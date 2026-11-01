"""How a volume is named to people: what kind of storage it is, and how big."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from kalinka_plugin_sdk.mounts import is_network_fs
from kalinka_plugin_sdk.storage import VolumeDescriptor, VolumeKind


class Medium(str, Enum):
    INTERNAL = "internal"
    NETWORK = "network"
    USB = "usb"
    DISK = "disk"


def medium_of(volume: VolumeDescriptor) -> Medium:
    if volume.kind == VolumeKind.INTERNAL:
        return Medium.INTERNAL
    if volume.fs_type and is_network_fs(volume.fs_type):
        return Medium.NETWORK
    if volume.bus == "usb" or volume.kind == VolumeKind.REMOVABLE:
        return Medium.USB
    return Medium.DISK


#: What a client draws for each, as ``BrowseItem.icon`` names it.
_ICONS = {
    Medium.INTERNAL: "internal_storage",
    Medium.NETWORK: "network_share",
    Medium.USB: "usb_drive",
    Medium.DISK: "disk",
}


def icon_of(volume: VolumeDescriptor) -> str:
    return _ICONS[medium_of(volume)]


def _kind_name(volume: VolumeDescriptor) -> str:
    medium = medium_of(volume)
    if medium == Medium.INTERNAL:
        return "On this server"
    if medium == Medium.NETWORK:
        return "NFS share" if (volume.fs_type or "").startswith("nfs") else "Network share"
    return "USB drive" if medium == Medium.USB else "Disk"


def size_text(n_bytes: Optional[int]) -> Optional[str]:
    """A size as people read it on a drive: "29 GB", "1.2 TB"."""
    if not n_bytes:
        return None
    for unit, scale in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6)):
        if n_bytes >= scale:
            value = n_bytes / scale
            return f"{value:.0f} {unit}" if value >= 10 else f"{value:.1f} {unit}"
    return f"{n_bytes} B"


def summary_of(volume: VolumeDescriptor) -> str:
    """Its kind and size: "USB drive · 31 GB", or the space left on the
    server's own storage."""
    if volume.kind == VolumeKind.INTERNAL:
        free = size_text(volume.free_bytes)
        size = f"{free} free" if free else None
    else:
        size = size_text(volume.size_bytes)
    return " · ".join(part for part in (_kind_name(volume), size) if part)
