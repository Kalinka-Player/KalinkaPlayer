"""SDK 3.20: storage the server has mounted, and how a library takes it in.

A plugin that keeps a library opts in as a :class:`LibraryAdopter`, so the
server can offer "Add to library" without knowing the plugin's settings.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, model_validator

from .datamodel import EntityId


class VolumeKind(str, Enum):
    """Where a volume came from."""

    REMOVABLE = "removable"
    MOUNT = "mount"
    INTERNAL = "internal"


class VolumeState(str, Enum):
    MOUNTED = "mounted"
    MOUNTING = "mounting"
    FAILED = "failed"
    UNMOUNTED = "unmounted"


class VolumeLibrary(BaseModel):
    """What the library makes of a whole volume."""

    module: str
    title: str
    holds: bool
    library_empty: bool


class VolumeDescriptor(BaseModel):
    """One volume, as ``GET /server/storage/volumes`` lists it.

    ``id`` is the filesystem UUID of a removable drive, the record id of a
    configured mount, or ``internal`` for the server's own music folder. It
    names the volume's directory under the media root and stays the same
    across plugs and restarts.
    """

    id: str
    kind: VolumeKind
    label: str
    mount_point: str
    state: VolumeState = VolumeState.MOUNTED
    fs_type: Optional[str] = None
    device: Optional[str] = None
    size_bytes: Optional[int] = None
    free_bytes: Optional[int] = None
    bus: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    error: Optional[str] = None
    library: Optional[VolumeLibrary] = None
    #: What a client shows under the label: its kind and size, such as
    #: "USB drive · 31 GB".
    summary: Optional[str] = None


class StorageLocation(BaseModel):
    """A folder on a volume.

    ``path`` is where the folder is on the server's own filesystem. A
    library running on another machine cannot read it there, and declines.
    """

    volume_id: str
    subpath: str = ""
    path: str
    label: str = ""


class LocationStatus(BaseModel):
    """A library's answer for one location.

    @param holds Whether a source the library already has covers it.
    @param library_empty Whether the library holds no tracks yet.
    @param removable Whether one of its sources is exactly this location,
        which :meth:`LibraryAdopter.release_location` takes out; False when
        it is held through a folder above it, or not at all.
    @param restart_required Whether adding or removing the location takes
        effect only after the server restarts.
    """

    holds: bool
    library_empty: bool = False
    removable: bool = False
    restart_required: bool = False


class SourcesChange(BaseModel):
    """How a library's settings change to take a location in or out.

    One record list, at ``collection`` inside the plugin's config, gains the
    record ``add`` or loses the record whose id is ``remove``. A change names
    one record rather than the whole list, so the credentials saved on the
    others stay as they are.
    """

    collection: str
    add: Optional[Dict[str, Any]] = None
    remove: Optional[str] = None

    @model_validator(mode="after")
    def _one_edit(self) -> "SourcesChange":
        if (self.add is None) == (self.remove is None):
            raise ValueError("a change adds one record or removes one")
        return self


@runtime_checkable
class LibraryAdopter(Protocol):
    """SDK 3.20: a plugin whose library can take in a folder from storage.

    The server asks every enabled plugin that implements these three, so a
    plugin with no library implements none of them.
    """

    async def location_status(self, location: StorageLocation) -> Optional[LocationStatus]:
        """Whether the library can keep ``location``, and whether it does.

        None for a location it cannot reach, such as a path on another
        machine, and "Add to library" is not offered for it.

        @note Called while folders are listed, so it must answer from state
            the plugin already holds.
        """
        ...

    async def adopt_location(self, location: StorageLocation) -> Optional[SourcesChange]:
        """The change that adds ``location`` as a source, or None when it
        cannot be added."""
        ...

    async def release_location(self, location: StorageLocation) -> Optional[SourcesChange]:
        """The change that removes the source that is exactly ``location``,
        or None when there is none."""
        ...


@runtime_checkable
class StorageBacked(Protocol):
    """SDK 3.20: an input module whose pages are folders on the server's
    storage, so a client can add one to a library or eject its drive."""

    async def storage_location(self, entity_id: EntityId) -> Optional[StorageLocation]:
        """Where the page ``entity_id`` names is, or None for a page that is
        not a folder of a volume."""
        ...
