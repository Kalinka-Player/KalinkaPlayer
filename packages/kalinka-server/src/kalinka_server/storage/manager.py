"""The volumes this machine has, kept current as drives come and go."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import select
import threading
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Optional, Sequence

from kalinka_plugin_sdk import paths
from kalinka_plugin_sdk.events import VolumesChangedEvent
from kalinka_plugin_sdk.mounts import (
    KALINKA_MEDIA_ROOT,
    UDEV_DATA_DIR,
    BlockFacts,
    Mount,
    block_facts,
    covers,
    is_network_fs,
    list_mounts,
)
from kalinka_plugin_sdk.storage import (
    LibraryAdopter,
    LocationStatus,
    StorageLocation,
    VolumeDescriptor,
    VolumeKind,
    VolumeLibrary,
    VolumeState,
)

from ..config_model import DiskMount, NfsMount, StorageConfig
from .describe import summary_of
from .requests import MountRequests

logger = logging.getLogger(__name__.split(".")[-1])

INTERNAL_ID = "internal"


def no_automount_marker() -> str:
    """What tells the root helper to leave USB drives alone."""
    return os.path.join(paths.state_dir(), "storage", "no-automount")

#: Where a desktop, the owner's fstab or an automounter puts a disk. Kalinka's
#: own root is under the first.
_MEDIA_ROOTS = ("/media", "/mnt", "/run/media")
_DISK_SOURCES = ("/dev/sd", "/dev/nvme", "/dev/mmcblk", "/dev/hd", "/dev/vd")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: How long one library is given to answer for one location.
LIBRARY_TIMEOUT_S = 2.0
#: How often mountinfo is read again when no change was signalled.
FALLBACK_POLL_S = 5.0


@dataclass(frozen=True)
class Adopter:
    """A plugin that may keep storage in its library."""

    module: str
    title: str
    library: LibraryAdopter


VolumesCallback = Callable[[list[VolumeDescriptor]], None]


def _under(path: str, roots: Iterable[str]) -> bool:
    return any(path.startswith(root + os.sep) for root in roots)


def _statvfs(path: str) -> tuple[Optional[int], Optional[int]]:
    try:
        st = os.statvfs(path)
    except OSError:
        return None, None
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def _settled(volumes: Sequence[VolumeDescriptor]) -> list[VolumeDescriptor]:
    """The volumes as a change is told by: free space moves with every write,
    and is no news to the clients."""
    return [v.model_copy(update={"free_bytes": None, "summary": None}) for v in volumes]


def _fstab_uuids(path: str) -> set[str]:
    """The UUIDs ``/etc/fstab`` already says where to mount."""
    found = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                spec = (line.split() or ["#"])[0]
                if spec.startswith("UUID="):
                    found.add(spec[len("UUID="):].strip('"'))
    except OSError:
        pass
    return found


def _disk_of(numbers: str, sys_dev_block: str) -> Optional[str]:
    """The whole disk the block device ``major:minor`` belongs to."""
    link = os.path.join(sys_dev_block, numbers)
    if not os.path.exists(link):
        return None
    real = os.path.realpath(link)
    if os.path.exists(os.path.join(real, "partition")):
        real = os.path.dirname(real)
    return os.path.basename(real)


def _derived_id(mount: Mount, facts: BlockFacts) -> str:
    if facts.uuid and _SAFE_ID.match(facts.uuid):
        return facts.uuid
    return "m-" + hashlib.sha1(mount.mount_point.encode()).hexdigest()[:12]


def _label(facts: BlockFacts, fallback: str) -> str:
    if facts.label:
        return facts.label
    name = " ".join(part for part in (facts.vendor, facts.model) if part)
    return name or fallback


class StorageManager:
    """Every volume the server can read music from, and the mounts it manages.

    Volumes are the server's own music folder, the drives and shares mounted
    under :data:`KALINKA_MEDIA_ROOT`, and the disks others mounted under
    ``/media``, ``/mnt`` or ``/run/media``. The list is rebuilt whenever
    mountinfo changes and pushed to subscribers and clients.

    @note Lives on the server's event loop. Only the mountinfo watch runs
        on a thread of its own, and it does nothing but schedule a refresh.
    """

    def __init__(
        self,
        config: Callable[[], StorageConfig],
        media_dir: str,
        dispatch: Callable[[VolumesChangedEvent], None],
        adopters: Callable[[], Sequence[Adopter]] = lambda: (),
        requests: Optional[MountRequests] = None,
        *,
        mountinfo: Callable[[], list[Mount]] = list_mounts,
        udev_data_dir: str = UDEV_DATA_DIR,
        media_root: str = KALINKA_MEDIA_ROOT,
        marker: Optional[str] = None,
        statvfs: Callable[[str], tuple[Optional[int], Optional[int]]] = _statvfs,
        fstab: str = "/etc/fstab",
        sys_dev_block: str = "/sys/dev/block",
    ):
        self._config = config
        self._media_dir = media_dir
        self._dispatch = dispatch
        self._adopters = adopters
        self._requests = requests or MountRequests()
        self._mountinfo = mountinfo
        self._udev = udev_data_dir
        self._media_root = media_root
        self._marker = marker or no_automount_marker()
        self._statvfs = statvfs
        self._fstab = fstab
        self._sys_dev_block = sys_dev_block
        self._volumes: list[VolumeDescriptor] = []
        self._subscribers: list[VolumesCallback] = []
        self._after_change: list[Callable[[], Awaitable[None]]] = []
        self._applied_mounts: dict[str, dict] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop = threading.Event()
        self._watch: Optional[threading.Thread] = None
        self._refreshing: Optional[asyncio.Task] = None
        self._again = False

    def volumes(self) -> list[VolumeDescriptor]:
        return list(self._volumes)

    def volume(self, volume_id: str) -> Optional[VolumeDescriptor]:
        return next((v for v in self._volumes if v.id == volume_id), None)

    def subscribe(self, callback: VolumesCallback) -> None:
        """``callback`` gets every new list, first thing after it is built."""
        self._subscribers.append(callback)

    def after_change(self, callback: Callable[[], Awaitable[None]]) -> None:
        """``callback`` runs after subscribers have the new list."""
        self._after_change.append(callback)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._apply_automount()
        self._apply_mounts()
        await self.refresh()
        self._watch = threading.Thread(target=self._watch_mountinfo, name="kal-mounts", daemon=True)
        self._watch.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._refreshing is not None:
            self._refreshing.cancel()

    async def config_changed(self) -> None:
        """Bring the automount switch and the configured mounts in line with
        the saved settings, and tell the listeners: switching Drives on or
        off changes no volume but what the server offers."""
        self._apply_automount()
        self._apply_mounts()
        if not await self.refresh():
            await self._tell_listeners()

    def eject(self, volume_id: str) -> bool:
        volume = self.volume(volume_id)
        if volume is None or volume.kind == VolumeKind.INTERNAL:
            return False
        if not _under(volume.mount_point, (self._media_root,)):
            return False
        self._safely(lambda: self._requests.unmount(volume_id, eject=volume.kind == VolumeKind.REMOVABLE))
        return True

    def mount(self, volume_id: str) -> bool:
        record = next((r for r in self._config().mounts if r.id == volume_id), None)
        if record is None:
            return False
        self._request_mount(record)
        return True

    async def location_status(self, location: StorageLocation) -> Optional[tuple[Adopter, LocationStatus]]:
        """The first library that answers for ``location``, and its answer."""
        for adopter in self._adopters():
            try:
                status = await asyncio.wait_for(adopter.library.location_status(location), LIBRARY_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001 — one plugin must not sink the listing
                logger.warning("%s could not answer for %s: %r", adopter.module, location.path, e)
                continue
            if status is not None:
                return adopter, status
        return None

    async def library_holding(self, path: str) -> Optional[str]:
        """The title of the library that holds ``path``, a resolved path, or
        None when none does."""
        location = self.location_of(path) or StorageLocation(volume_id="", path=path)
        answer = await self.location_status(location)
        if answer is None or not answer[1].holds:
            return None
        return answer[0].title

    def location_of(self, path: str) -> Optional[StorageLocation]:
        """The volume ``path``, resolved already, is on, and where on it.

        @note Reads nothing from disk: a share that stopped answering must
            not hold up the event loop.
        """
        real = os.path.normpath(path)
        best: Optional[VolumeDescriptor] = None
        for volume in self._volumes:
            if volume.state == VolumeState.MOUNTED and covers(volume.mount_point, real):
                if best is None or len(volume.mount_point) > len(best.mount_point):
                    best = volume
        if best is None:
            return None
        subpath = os.path.relpath(real, best.mount_point)
        return StorageLocation(
            volume_id=best.id,
            subpath="" if subpath == "." else subpath,
            path=real,
            label=best.label,
        )

    async def refresh(self) -> bool:
        """Rebuild the list now, or once more right after a rebuild under way.

        @return Whether the list changed, and the listeners were told.
        """
        if self._refreshing is not None and not self._refreshing.done():
            self._again = True
            return await asyncio.shield(self._refreshing)
        self._refreshing = asyncio.ensure_future(self._rebuild_until_settled())
        return await asyncio.shield(self._refreshing)

    async def _rebuild_until_settled(self) -> bool:
        told = False
        while True:
            self._again = False
            volumes = await asyncio.to_thread(self.snapshot)
            volumes = [await self._with_library(v) for v in volumes]
            changed = _settled(volumes) != _settled(self._volumes)
            self._volumes = volumes
            if changed:
                self._announce()
                await self._tell_listeners()
                told = True
            if not self._again:
                return told

    async def _tell_listeners(self) -> None:
        for callback in list(self._after_change):
            try:
                await callback()
            except Exception:  # noqa: BLE001
                logger.exception("A storage change listener failed")

    def _announce(self) -> None:
        for callback in list(self._subscribers):
            try:
                callback(self.volumes())
            except Exception:  # noqa: BLE001
                logger.exception("A storage subscriber failed")
        self._dispatch(VolumesChangedEvent(volumes=self.volumes()))

    async def _with_library(self, volume: VolumeDescriptor) -> VolumeDescriptor:
        if volume.state != VolumeState.MOUNTED:
            return volume
        location = StorageLocation(volume_id=volume.id, path=volume.mount_point, label=volume.label)
        answer = await self.location_status(location)
        if answer is None:
            return volume
        adopter, status = answer
        library = VolumeLibrary(
            module=adopter.module,
            title=adopter.title,
            holds=status.holds,
            library_empty=status.library_empty,
        )
        return volume.model_copy(update={"library": library})

    def snapshot(self) -> list[VolumeDescriptor]:
        """The volumes as mountinfo, udev and the settings describe them now."""
        mounts = self._mountinfo()
        config = self._config()
        configured = {record.id: record for record in config.mounts}
        found: dict[str, VolumeDescriptor] = {}

        internal = self._internal()
        if internal is not None:
            found[internal.id] = internal

        # The latest mount on a point hides those under it.
        shown = {mount.mount_point: mount for mount in mounts}
        views: set[str] = set()
        # Kalinka's own mounts first: their id is their directory, so a clone
        # mounted elsewhere is the one renamed, and a view of either is theirs.
        ours = sorted(mounts, key=lambda m: not _under(m.mount_point, (self._media_root,)))
        for mount in ours:
            if shown[mount.mount_point] is not mount:
                continue
            volume = self._describe(mount, configured)
            # A filesystem mounted at two points is one volume.
            if volume is None or (mount.device and mount.device in views):
                continue
            views.add(mount.device)
            if volume.id in found:
                # Cloned sticks share a UUID; the one mounted first keeps it.
                volume = volume.model_copy(update={"id": f"{volume.id}-{os.path.basename(volume.device)}"})
            found[volume.id] = volume

        for record in config.mounts:
            if record.id not in found:
                found[record.id] = self._unmounted(record)

        order = {VolumeKind.INTERNAL: 0, VolumeKind.REMOVABLE: 1, VolumeKind.MOUNT: 2}
        return sorted(
            (v.model_copy(update={"summary": summary_of(v)}) for v in found.values()),
            key=lambda v: (order[v.kind], v.label.casefold(), v.id),
        )

    def _internal(self) -> Optional[VolumeDescriptor]:
        if not os.path.isdir(self._media_dir):
            return None
        size, free = self._statvfs(self._media_dir)
        return VolumeDescriptor(
            id=INTERNAL_ID,
            kind=VolumeKind.INTERNAL,
            label="Internal storage",
            mount_point=self._media_dir,
            size_bytes=size,
            free_bytes=free,
        )

    def _describe(self, mount: Mount, configured: dict) -> Optional[VolumeDescriptor]:
        point = mount.mount_point
        network = is_network_fs(mount.fs_type)
        if not _under(point, _MEDIA_ROOTS):
            return None
        if mount.fs_root not in ("", "/"):
            return None
        if not network and not mount.source.startswith(_DISK_SOURCES):
            return None
        facts = BlockFacts() if network else block_facts(mount.device, self._udev)

        if _under(point, (self._media_root,)):
            volume_id = os.path.relpath(point, self._media_root)
            if os.sep in volume_id or not _SAFE_ID.match(volume_id):
                return None
            record = configured.get(volume_id)
        else:
            volume_id, record = _derived_id(mount, facts), None

        if record is not None:
            kind, label = VolumeKind.MOUNT, record.label or self._record_name(record)
        elif _under(point, (self._media_root,)) and not network:
            kind, label = VolumeKind.REMOVABLE, _label(facts, volume_id)
        else:
            kind, label = VolumeKind.MOUNT, _label(facts, os.path.basename(point) or point)

        size, free = self._statvfs(point) if not network else (None, None)
        return VolumeDescriptor(
            id=volume_id,
            kind=kind,
            label=label,
            mount_point=point,
            fs_type=mount.fs_type,
            device=mount.source,
            size_bytes=size,
            free_bytes=free,
            bus=facts.bus or None,
            vendor=facts.vendor or None,
            model=facts.model or None,
        )

    def _record_name(self, record) -> str:
        if isinstance(record, NfsMount):
            return f"{record.host}:{record.export}" if record.host else "NFS share"
        return "Disk"

    def _unmounted(self, record) -> VolumeDescriptor:
        outcome = self._requests.outcome(record.id)
        if self._requests.pending(record.id):
            state, error = VolumeState.MOUNTING, None
        elif outcome is not None and not outcome.ok:
            state, error = VolumeState.FAILED, outcome.error
        else:
            state, error = VolumeState.UNMOUNTED, None
        return VolumeDescriptor(
            id=record.id,
            kind=VolumeKind.MOUNT,
            label=record.label or self._record_name(record),
            mount_point=os.path.join(self._media_root, record.id),
            state=state,
            error=error,
            fs_type="nfs4" if isinstance(record, NfsMount) else None,
        )

    def _apply_automount(self) -> None:
        enabled = self._config().automount_usb
        try:
            if enabled and os.path.exists(self._marker):
                os.unlink(self._marker)
            elif not enabled and not os.path.exists(self._marker):
                os.makedirs(os.path.dirname(self._marker), exist_ok=True)
                with open(self._marker, "w"):
                    pass
        except OSError as e:
            logger.warning("Could not record the automount setting: %s", e)

    def _apply_mounts(self) -> None:
        wanted = {record.id: record for record in self._config().mounts}
        for volume_id in set(self._applied_mounts) - set(wanted):
            self._safely(lambda vid=volume_id: self._requests.unmount(vid))
            del self._applied_mounts[volume_id]
        for volume_id, record in wanted.items():
            shape = record.model_dump(exclude={"label"})
            if self._applied_mounts.get(volume_id) == shape:
                continue
            if volume_id in self._applied_mounts:
                self._safely(lambda vid=volume_id: self._requests.unmount(vid))
            self._request_mount(record)
            self._applied_mounts[volume_id] = shape

    def _request_mount(self, record) -> None:
        options = ["ro" if record.read_only else "rw"]
        if isinstance(record, NfsMount):
            if not record.host or not record.export:
                return
            fstype = "nfs" if record.version.value == "3" else "nfs4"
            options += ["soft", "timeo=100", f"vers={record.version.value}"]
            source = f"{record.host}:{record.export}"
        elif isinstance(record, DiskMount):
            if not record.fs_uuid:
                return
            fstype, source = "disk", f"UUID={record.fs_uuid}"
        else:
            return
        self._safely(lambda: self._requests.mount(record.id, fstype, source, options))

    @staticmethod
    def _safely(request: Callable[[], None]) -> None:
        try:
            request()
        except OSError as e:
            logger.warning("Could not ask for a mount: %s", e)

    def filesystem_options(self) -> list[dict]:
        """What a disk mount's filesystem can be set to: those nothing has
        mounted, and those the saved mounts name, mounted or not."""
        options = [
            {
                "value": uuid,
                "label": facts.label or uuid,
                "description": " · ".join(p for p in (facts.fs_type, facts.vendor, facts.model) if p),
            }
            for uuid, facts in self.unmounted_filesystems()
        ]
        offered = {option["value"] for option in options}
        for record in self._config().mounts:
            if isinstance(record, DiskMount) and record.fs_uuid and record.fs_uuid not in offered:
                offered.add(record.fs_uuid)
                options.append(
                    {"value": record.fs_uuid, "label": record.label or record.fs_uuid, "description": "A saved mount"}
                )
        return options

    def unmounted_filesystems(self) -> list[tuple[str, BlockFacts]]:
        """Filesystems udev knows of that nothing has mounted, by UUID: none
        that /etc/fstab places, and none on a disk the system runs from,
        which the mount helper refuses as well."""
        mounts = self._mountinfo()
        mounted = {m.device for m in mounts}
        system = {
            _disk_of(m.device, self._sys_dev_block)
            for m in mounts
            if m.device and not _under(m.mount_point, (self._media_root,))
        }
        listed = _fstab_uuids(self._fstab)
        found = []
        try:
            names = os.listdir(self._udev)
        except OSError:
            return []
        for name in sorted(names):
            if not name.startswith("b") or name[1:] in mounted:
                continue
            if _disk_of(name[1:], self._sys_dev_block) in system - {None}:
                continue
            facts = block_facts(name[1:], self._udev)
            # A USB drive mounts by itself, unless that is switched off.
            automounted = facts.bus == "usb" and self._config().automount_usb
            if facts.fs_usage == "filesystem" and facts.uuid and not automounted and facts.uuid not in listed:
                found.append((facts.uuid, facts))
        return found

    def _watch_mountinfo(self) -> None:
        try:
            handle = open("/proc/self/mountinfo", "rb")
        except OSError:
            handle = None
        poller = None
        if handle is not None:
            poller = select.epoll()
            poller.register(handle.fileno(), select.EPOLLPRI | select.EPOLLERR)
        try:
            while not self._stop.is_set():
                signalled = False
                try:
                    if poller is not None:
                        signalled = bool(poller.poll(FALLBACK_POLL_S))
                        handle.seek(0)
                        handle.read()
                    else:
                        self._stop.wait(FALLBACK_POLL_S)
                except (OSError, ValueError) as e:
                    logger.warning("Watching the mounts failed, looking again shortly: %s", e)
                    self._stop.wait(FALLBACK_POLL_S)
                    signalled = True
                if self._stop.is_set():
                    return
                # Without mountinfo to wait on, every poll looks again.
                if signalled or poller is None or self._awaiting_answers():
                    self._schedule_refresh()
        finally:
            if poller is not None:
                poller.close()
            if handle is not None:
                handle.close()

    def _awaiting_answers(self) -> bool:
        """Whether a mount request is out, whose answer changes no mount when
        it is a refusal."""
        return any(v.state == VolumeState.MOUNTING for v in self._volumes)

    def _schedule_refresh(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.refresh()))
        except RuntimeError:
            pass
