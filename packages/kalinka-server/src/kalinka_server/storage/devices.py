"""What Drives & shares lists: every drive and share the server could read
music from, mounted or not, and what may be done with each."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import posixpath
import re
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional, Protocol

from pydantic import BaseModel

from kalinka_plugin_sdk.storage import VolumeDescriptor, VolumeKind, VolumeLibrary, VolumeState

from ..config_model import DiskMount, NfsMount, NfsVersion, StorageConfig
from .describe import Medium, drive_summary, medium_of, size_text, summary_of
from .manager import Filesystem, StorageManager
from .nfs_discovery import Export, HostAnswer, ScanState
from .requests import UNANSWERED, UNSENT

logger = logging.getLogger(__name__)

#: What a share's host may be: a name or an IPv4 address, or an IPv6 one in
#: brackets, as the mount helper takes it.
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,252}|\[[0-9A-Fa-f:.]{2,45}\]")
_MAX_EXPORT = 1024

_ORDER = {Medium.INTERNAL: 0, Medium.DISK: 1, Medium.USB: 2, Medium.NETWORK: 3}

#: How long a mount or an unmount is waited on before it is answered as
#: still under way, short of the app's 15 s.
ANSWER_WAIT_S = 10.0

NO_HELPER = "This server cannot mount: the mount helper comes with the Kalinka package"

_NFS_PORT = 2049

#: What mount.nfs says when the share's server will not answer.
_NO_ANSWER = ("Connection refused", "timed out", "No route to host", "Network is unreachable")

#: What a failed mount tells whoever has to put it right, where nothing
#: more particular is known; the mount's own words go with it as details.
SHARE_FAILED = "Check the connection or share settings."
DRIVE_FAILED = "Check that the drive is plugged in and readable."

#: Failures Kalinka words itself, which need no details.
_OWN_WORDS = (UNSENT, UNANSWERED)


def nfs_id(host: str, export: str) -> str:
    """A share's id, the same whichever way it was found."""
    return "nfs-" + hashlib.sha1(f"{host}:{export}".encode()).hexdigest()[:12]


def share_name(host: str, export: str) -> str:
    """What a share is called until someone names it: its folder."""
    return posixpath.basename(export.rstrip("/")) or host


def share_summary(host: str, server: Optional[str] = None) -> str:
    """What a share is and who serves it, by the name its host announces
    where it announced one."""
    return f"NFS · {server or host}"


def address_seen_by(host: str) -> Optional[str]:
    """This machine's address as ``host`` sees it: the one a packet to it
    would leave from. Nothing is sent."""
    try:
        family, _, _, _, address = socket.getaddrinfo(host.strip("[]"), _NFS_PORT, type=socket.SOCK_DGRAM)[0]
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect(address)
            return probe.getsockname()[0]
    except OSError:
        return None


def nfs_failure(
    error: str, share: "NfsTarget", server: Optional[str], seen_by: Callable[[str], Optional[str]]
) -> Optional[str]:
    """What a failed mount of ``share`` means to whoever has to put it right,
    or None where the mount's words say nothing more particular."""
    name = server or share.host
    if "access denied by server" in error:
        address = seen_by(share.host) or "this streamer's address"
        return f"{name} does not let this streamer in. Add {address} to the share's NFS exports."
    if "No such file or directory" in error:
        return f"{name} has no share at {share.export}."
    if "not supported" in error:
        version = share.version.value if share.version else NfsVersion.v4.value
        return f"{name} does not offer NFS {version}. Choose another NFS version for it."
    if any(words in error for words in _NO_ANSWER):
        return f"{name} did not answer. Check that it is on and shares over NFS."
    return None


def failure_of(device: "StorageDevice", seen_by: Callable[[str], Optional[str]]) -> tuple[str, Optional[str]]:
    """A failed device's error in plain words, and the words of the mount
    behind it, None where Kalinka worded the failure itself."""
    error = device.error or ""
    if error in _OWN_WORDS:
        return error, None
    if device.nfs is not None:
        return nfs_failure(error, device.nfs, device.server, seen_by) or SHARE_FAILED, error
    return DRIVE_FAILED, error


class NfsTarget(BaseModel):
    host: str
    export: str
    version: Optional[NfsVersion] = None


class StorageDevice(BaseModel):
    """One row of Drives & shares.

    The ``can_*`` flags say which actions apply now; a client offers those
    and works none out for itself. ``at_start`` is the Mount after restart
    switch, None where there is none, and for a device not kept yet the
    switch keeping it would give it; ``can_edit`` is whether there are
    settings to change at all.
    """

    id: str
    medium: Medium
    label: str
    summary: str
    state: VolumeState = VolumeState.UNMOUNTED
    #: Why it failed to mount, in plain words.
    error: Optional[str] = None
    #: What the mount itself said, for whoever wants the details.
    error_detail: Optional[str] = None
    managed_by: Literal["kalinka", "system"] = "kalinka"
    saved: bool = False
    at_start: Optional[bool] = None
    #: False for a saved drive that is not plugged in.
    connected: bool = True
    #: For a share: whether the last search saw it.
    found: Optional[bool] = None
    read_only: Optional[bool] = None
    nfs: Optional[NfsTarget] = None
    #: What the host of a found share calls itself.
    server: Optional[str] = None
    #: Who a found share lets in, as its host says.
    clients: list[str] = []
    library: Optional[VolumeLibrary] = None
    can_mount: bool = False
    can_unmount: bool = False
    can_eject: bool = False
    can_set_at_start: bool = False
    can_edit: bool = False
    can_forget: bool = False
    #: For a device not kept whose mount failed: the failure can be put away.
    can_dismiss: bool = False


class ScanInfo(BaseModel):
    running: bool
    finished_at: Optional[float] = None


class DeviceList(BaseModel):
    devices: list[StorageDevice]
    scan: ScanInfo
    #: False where no mount helper carries a mount out, and nothing offers one.
    mounting_available: bool = True


class ActionResult(BaseModel):
    """Where a device stands once its mount or unmount was answered, or the
    wait for that ran out: ``mounting`` then, for a mount."""

    id: str
    state: VolumeState
    error: Optional[str] = None
    error_detail: Optional[str] = None


class DeviceChanges(BaseModel):
    """What ``PUT /server/storage/devices/{id}`` may change, any subset."""

    at_start: Optional[bool] = None
    label: Optional[str] = None
    read_only: Optional[bool] = None
    version: Optional[NfsVersion] = None


class NewShare(BaseModel):
    """A share ``POST /server/storage/shares`` keeps and mounts, and how."""

    host: str
    export: str
    version: Optional[NfsVersion] = None
    at_start: bool = True
    read_only: bool = True


class ShareFinder(Protocol):
    def exports(self) -> list[Export]: ...

    def scan_state(self) -> ScanState: ...

    def probe(self, host: str) -> HostAnswer: ...

    def rescan(self) -> None: ...


#: Commits a change to the saved settings, as ``PUT /server/config`` does;
#: answers why it was refused, or None.
SaveChanges = Callable[[dict], Awaitable[Optional[str]]]


@dataclass(frozen=True)
class _Trial:
    """A mount asked for from here of a device not kept: the record it is
    mounted with, and whether it is kept once mounted."""

    record: object
    keep: bool


class DeviceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class StorageDevices:
    """The device list, and the actions on it as config changes and mount
    requests.

    A device not kept is kept only once a mount of it from here succeeds;
    a failed one is remembered, not kept, until it is tried again or put
    away, and is gone after a restart.

    @note Lives on the event loop. :meth:`listing` and :meth:`probe` read
        disks and the network: call them off it.
    """

    MOUNTS_PATH = "base_config.storage.mounts"

    def __init__(
        self,
        storage: StorageManager,
        config: Callable[[], StorageConfig],
        finder: ShareFinder,
        save: SaveChanges,
        answer_wait: float = ANSWER_WAIT_S,
        seen_by: Callable[[str], Optional[str]] = address_seen_by,
    ):
        self._storage = storage
        self._config = config
        self._finder = finder
        self._save = save
        self._answer_wait = answer_wait
        self._seen_by = seen_by
        # One change to the saved list at a time: each starts from the list
        # as saved, which another in between would leave behind.
        self._changing = asyncio.Lock()
        self._trials: dict[str, _Trial] = {}
        self._settling: set[asyncio.Task] = set()
        storage.after_change(self._keep_when_mounted)

    def listing(self) -> DeviceList:
        config = self._config()
        records = {record.id: record for record in config.mounts}
        filesystems = self._storage.unmounted_filesystems()
        present = {fs.uuid: fs for fs in filesystems}
        devices = [
            self._from_volume(volume, records.get(volume.id), config, present)
            for volume in self._storage.volumes()
        ]
        listed = {device.id for device in devices}
        kept_uuids = {record.fs_uuid for record in config.mounts if isinstance(record, DiskMount)}
        devices += [
            self._from_filesystem(fs)
            for fs in filesystems
            if fs.uuid not in listed and fs.uuid not in kept_uuids
        ]
        devices = self._with_trials(self._with_exports(devices, records, self._finder.exports()))
        devices = [self._explained(d) for d in devices]
        available = self._storage.can_mount()
        if not available:
            devices = [d.model_copy(update={"can_mount": False}) for d in devices]
        state = self._finder.scan_state()
        return DeviceList(
            devices=sorted(devices, key=lambda d: (_ORDER[d.medium], not d.saved, d.label.casefold(), d.id)),
            scan=ScanInfo(running=state.running, finished_at=state.finished_at),
            mounting_available=available,
        )

    def probe(self, host: str) -> HostAnswer:
        return self._finder.probe(_checked_host(host))

    def rescan(self) -> None:
        self._finder.rescan()

    async def mount(self, device_id: str, changes: Optional[DeviceChanges] = None) -> ActionResult:
        """Mount a device now with ``changes`` to its settings, and answer
        once it is mounted, failed, or the wait ran out. One kept has the
        changes saved first; a disk or share not kept is kept once mounted,
        with its switch as listed unless ``changes`` say."""
        self._check_can_mount()
        async with self._changing:
            device = await self._device(device_id)
            if not device.can_mount:
                raise DeviceError(409, f"{device.label} cannot be mounted now")
            kept = self._record(device_id)
            tried = self._trials.get(device_id)
            changes = changes or DeviceChanges()
            base = kept or (tried.record if tried else self._new_record(device))
            record = self._changed(device, base, changes)
            if kept is not None:
                if record != kept:
                    await self._commit(_keeping(self._records(), record))
            else:
                # A drive that comes and goes is kept only once something of it is.
                keep = device.medium != Medium.USB or bool(changes.model_dump(exclude_none=True))
                self._trials[device_id] = _Trial(record, keep or (tried is not None and tried.keep))
            self._storage.request_mount(record)
        return await self._answered_mount(device_id)

    async def unmount(self, device_id: str) -> ActionResult:
        """Unmount a device now. Its Mount after restart switch stays as it
        is, so one kept with it on comes back at the next start.

        @throws DeviceError 502 with the helper's reason where it refused.
        """
        async with self._changing:
            device = await self._device(device_id)
            if not device.can_unmount:
                raise DeviceError(409, f"{device.label} cannot be unmounted now")
            if not self._storage.unmount(device_id):
                raise DeviceError(409, f"{device.label} cannot be unmounted now")
        answer = await self._storage.answer(device_id, self._answer_wait)
        if answer is None:
            return ActionResult(id=device_id, state=VolumeState.MOUNTED)
        if not answer.ok:
            raise DeviceError(502, answer.error or f"{device.label} could not be unmounted")
        return ActionResult(id=device_id, state=VolumeState.UNMOUNTED)

    async def update(self, device_id: str, changes: DeviceChanges) -> None:
        """Change a device's record, keeping the device first if it is not,
        with its switch as listed; mounts nothing."""
        async with self._changing:
            device = await self._device(device_id)
            if not device.can_edit:
                raise DeviceError(409, f"{device.label} has no settings Kalinka keeps")
            record = self._changed(device, self._record(device_id) or self._new_record(device), changes)
            await self._commit(_keeping(self._records(), record))

    async def dismiss(self, device_id: str) -> None:
        """Put away the failed mount of a device not kept."""
        async with self._changing:
            device = await self._device(device_id)
            if not device.can_dismiss:
                raise DeviceError(409, f"{device.label} has no failure to put away")
            self._trials.pop(device_id, None)

    async def forget(self, device_id: str) -> None:
        async with self._changing:
            device = await self._device(device_id)
            if not device.can_forget:
                raise DeviceError(409, f"{device.label} is not saved")
            # Taking the record out unmounts it.
            await self._commit([r for r in self._records() if r.id != device_id])

    async def add_share(self, share: NewShare) -> ActionResult:
        """Mount the share ``share`` describes, as it says, answering as
        :meth:`mount` does: kept once mounted, or with the change saved
        where it is kept already."""
        self._check_can_mount()
        host = _checked_host(share.host)
        export = share.export.strip()
        if not export.startswith("/") or len(export) > _MAX_EXPORT or "\n" in export or "," in export:
            raise DeviceError(422, "An export is a path, such as /volume1/music")
        async with self._changing:
            kept = next(
                (r for r in self._records() if isinstance(r, NfsMount) and (r.host, r.export) == (host, export)),
                None,
            )
            record = kept or NfsMount(
                id=nfs_id(host, export),
                label=share_name(host, export),
                host=host,
                export=export,
                version=NfsVersion.v4,
            )
            if self._mounted(record.id):
                raise DeviceError(409, f"{record.label} is mounted already")
            record = record.model_copy(
                update={
                    "at_start": share.at_start,
                    "read_only": share.read_only,
                    "version": share.version or record.version,
                }
            )
            if kept is not None:
                await self._commit(_keeping(self._records(), record))
            else:
                self._trials[record.id] = _Trial(record, keep=True)
            self._storage.request_mount(record)
        return await self._answered_mount(record.id)

    @staticmethod
    def _changed(device: StorageDevice, record, changes: DeviceChanges):
        """``record`` with ``changes``, refused where they do not apply to
        ``device``."""
        if changes.model_dump(exclude_none=True) and not device.can_edit:
            raise DeviceError(409, f"{device.label} has no settings Kalinka keeps")
        if changes.at_start is not None and not device.can_set_at_start:
            raise DeviceError(409, "USB drives mount by themselves while automount is on")
        if changes.version is not None and not isinstance(record, NfsMount):
            raise DeviceError(422, "Only a share has an NFS version")
        update = changes.model_dump(exclude_none=True)
        if "label" in update:
            update["label"] = update["label"].strip()
        return record.model_copy(update=update)

    def _check_can_mount(self) -> None:
        if not self._storage.can_mount():
            raise DeviceError(503, NO_HELPER)

    async def _answered_mount(self, device_id: str) -> ActionResult:
        answer = await self._storage.answer(device_id, self._answer_wait)
        if answer is None:
            return ActionResult(id=device_id, state=VolumeState.MOUNTING)
        if not answer.ok:
            device = await self._device(device_id)
            return ActionResult(
                id=device_id,
                state=VolumeState.FAILED,
                error=device.error or answer.error,
                error_detail=device.error_detail,
            )
        await self._keep_mounted_trials()
        return ActionResult(id=device_id, state=VolumeState.MOUNTED)

    def _mounted(self, device_id: str) -> bool:
        volume = self._storage.volume(device_id)
        return volume is not None and volume.state == VolumeState.MOUNTED

    async def _keep_when_mounted(self) -> None:
        """Run as the volumes change, perhaps with a change under way:
        keeping what mounted waits its turn."""
        if any(self._mounted(device_id) for device_id in self._trials):
            task = asyncio.ensure_future(self._keep_mounted_trials())
            self._settling.add(task)
            task.add_done_callback(self._settling.discard)

    async def _keep_mounted_trials(self) -> None:
        """Keep each device tried from here that is mounted now, and end its
        trial."""
        async with self._changing:
            mounted = [self._trials.pop(device_id) for device_id in list(self._trials) if self._mounted(device_id)]
            # One saved while it mounted keeps what was saved.
            kept = [trial.record for trial in mounted if trial.keep and self._record(trial.record.id) is None]
            if not kept:
                return
            records = self._records()
            for record in kept:
                records = _keeping(records, record)
            try:
                await self._commit(records)
            except DeviceError as e:
                logger.warning("Could not keep %s once mounted: %s", ", ".join(r.id for r in kept), e.message)

    def _with_trials(self, devices: list[StorageDevice]) -> list[StorageDevice]:
        """The devices tried from here and not kept, as the trial left them:
        under way, or failed with the reason; a share that nothing else
        lists after the rest."""
        by_id = {device.id: device for device in devices}
        # Copied at once: the list is built off the loop the trials change on.
        for device_id, trial in list(self._trials.items()):
            listed = by_id.get(device_id)
            if listed is not None and (listed.saved or listed.state == VolumeState.MOUNTED):
                continue
            if listed is None and not isinstance(trial.record, NfsMount):
                continue
            by_id[device_id] = _trial_row(trial.record, listed, self._storage.attempt(trial.record))
        return list(by_id.values())

    def _explained(self, device: StorageDevice) -> StorageDevice:
        if device.error is None:
            return device
        error, detail = failure_of(device, self._seen_by)
        return device.model_copy(update={"error": error, "error_detail": detail})

    async def _device(self, device_id: str) -> StorageDevice:
        listing = await asyncio.to_thread(self.listing)
        device = next((d for d in listing.devices if d.id == device_id), None)
        if device is None:
            raise DeviceError(404, "No such drive or share")
        return device

    def _records(self) -> list:
        return list(self._config().mounts)

    def _record(self, device_id: str):
        return next((r for r in self._config().mounts if r.id == device_id), None)

    async def _commit(self, records: list) -> None:
        refused = await self._save({self.MOUNTS_PATH: [r.model_dump(mode="json") for r in records]})
        if refused is not None:
            raise DeviceError(422, refused)

    @staticmethod
    def _new_record(device: StorageDevice):
        """The record that would keep ``device``, with its switch where it
        starts: on for what nothing else would mount, off for a USB drive."""
        if device.medium == Medium.NETWORK and device.nfs is not None:
            return NfsMount(
                id=device.id,
                label=device.label,
                host=device.nfs.host,
                export=device.nfs.export,
                version=device.nfs.version or NfsVersion.v4,
            )
        usb = device.medium == Medium.USB
        return DiskMount(id=device.id, fs_uuid=device.id, usb=usb, at_start=not usb)

    def _from_volume(
        self,
        volume: VolumeDescriptor,
        record,
        config: StorageConfig,
        present: dict[str, Filesystem],
    ) -> StorageDevice:
        medium = medium_of(volume)
        summary = volume.summary or summary_of(volume)
        if volume.kind == VolumeKind.INTERNAL:
            # Listed on this streamer already; its space is all there is to say.
            free = size_text(volume.free_bytes)
            return StorageDevice(
                id=volume.id,
                medium=medium,
                label=volume.label,
                summary=f"{free} free" if free else "",
                state=volume.state,
            )
        if not self._storage.manages(volume):
            if medium == Medium.NETWORK and volume.device:
                summary = volume.device
            return StorageDevice(
                id=volume.id,
                medium=medium,
                label=volume.label,
                summary=summary,
                state=volume.state,
                managed_by="system",
                library=volume.library,
            )

        mounted = volume.state == VolumeState.MOUNTED
        label = volume.label
        nfs = None
        connected = True
        if isinstance(record, NfsMount):
            label = record.label or share_name(record.host, record.export)
            summary = share_summary(record.host)
            nfs = NfsTarget(host=record.host, export=record.export, version=record.version)
        elif isinstance(record, DiskMount):
            if record.usb:
                medium = Medium.USB
            fs = present.get(record.fs_uuid)
            if fs is not None:
                label = record.label or fs.label
                medium = Medium.USB if fs.usb or record.usb else Medium.DISK
                summary = drive_summary(medium, fs.size_bytes)
            connected = mounted or fs is not None or volume.state == VolumeState.MOUNTING
        # Automount mounts every USB drive and leaves nothing to decide.
        automatic = medium == Medium.USB and config.automount_usb
        return StorageDevice(
            id=volume.id,
            medium=medium,
            label=label,
            summary=summary,
            state=volume.state,
            error=volume.error,
            saved=record is not None,
            at_start=None if automatic else record.at_start if record is not None else medium != Medium.USB,
            connected=connected,
            found=False if nfs is not None else None,
            read_only=record.read_only if record is not None else None,
            nfs=nfs,
            library=volume.library,
            can_mount=not automatic
            and connected
            and volume.state in (VolumeState.UNMOUNTED, VolumeState.FAILED),
            can_unmount=mounted and not automatic,
            can_eject=mounted and medium == Medium.USB,
            can_set_at_start=not automatic,
            can_edit=not automatic,
            can_forget=record is not None,
        )

    @staticmethod
    def _from_filesystem(fs: Filesystem) -> StorageDevice:
        medium = Medium.USB if fs.usb else Medium.DISK
        return StorageDevice(
            id=fs.uuid,
            medium=medium,
            label=fs.label,
            summary=drive_summary(medium, fs.size_bytes),
            at_start=not fs.usb,
            can_mount=True,
            can_set_at_start=True,
            can_edit=True,
        )

    @staticmethod
    def _with_exports(devices: list[StorageDevice], records: dict, exports: list[Export]) -> list[StorageDevice]:
        """The saved shares marked as found where the search saw them, and
        the shares it saw that are not saved after them."""
        by_id = {device.id: device for device in devices}
        shares = {r.id: r for r in records.values() if isinstance(r, NfsMount)}
        for export in exports:
            saved = next(
                (r for r in shares.values() if r.export == export.path and export.is_on(r.host)),
                None,
            )
            hint = {"found": True, "server": export.server or None, "clients": list(export.clients)}
            if saved is not None and saved.id in by_id:
                by_id[saved.id] = by_id[saved.id].model_copy(
                    update={**hint, "summary": share_summary(saved.host, export.server)}
                )
                continue
            device_id = nfs_id(export.host, export.path)
            by_id.setdefault(
                device_id,
                StorageDevice(
                    id=device_id,
                    medium=Medium.NETWORK,
                    label=share_name(export.host, export.path),
                    summary=share_summary(export.host, export.server),
                    at_start=True,
                    nfs=NfsTarget(host=export.host, export=export.path),
                    can_mount=True,
                    can_set_at_start=True,
                    can_edit=True,
                    **hint,
                ),
            )
        return list(by_id.values())


def _trial_row(record, listed: Optional[StorageDevice], attempt: VolumeDescriptor) -> StorageDevice:
    """``listed``, or a row of its own where nothing lists the device, as a
    trial of ``record`` left it."""
    if listed is None:
        listed = StorageDevice(
            id=record.id,
            medium=Medium.NETWORK,
            label=record.label,
            summary=share_summary(record.host),
            can_set_at_start=True,
            can_edit=True,
        )
    tried = {
        "label": record.label or listed.label,
        "state": attempt.state,
        "error": attempt.error,
        "at_start": record.at_start,
        "read_only": record.read_only,
        "can_mount": attempt.state != VolumeState.MOUNTING,
        "can_dismiss": attempt.state == VolumeState.FAILED,
    }
    if isinstance(record, NfsMount):
        tried["nfs"] = NfsTarget(host=record.host, export=record.export, version=record.version)
    return listed.model_copy(update=tried)


def _keeping(records: list, record) -> list:
    """``records`` with ``record`` in place of the one with its id, or after
    them all."""
    if any(r.id == record.id for r in records):
        return [record if r.id == record.id else r for r in records]
    return [*records, record]


def _checked_host(host: str) -> str:
    host = host.strip()
    if not _HOST.fullmatch(host):
        raise DeviceError(422, "A server is a name or an address, such as nas.local")
    return host
