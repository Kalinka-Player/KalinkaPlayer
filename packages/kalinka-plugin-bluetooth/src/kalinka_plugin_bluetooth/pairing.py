"""Time-limited audio pairing, coordinated with BLE provisioning.

Both Kalinka clients take the same advisory lock before changing adapter-global
properties or the default agent. Capture does not hold the lease. Audio access
is remembered separately from BlueZ bonds. Explicit Forget removes the shared
bond under the same lease; it never resets network provisioning settings.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel
from kalinka_plugin_sdk.receiver_control import ReceiverError

from dbus_fast import Message

from .backend import ADAPTER, AUDIO_UUIDS, BLUEZ, DEVICE, PROPERTIES, plain

AGENT = "/org/kalinka/bluetooth_agent"


class AccessRecord(BaseModel):
    alias: str
    trust_before_grant: bool | None = None
    state: Literal["pending", "allowed", "revoked", "forgetting"] = "pending"


class AudioAccess:
    """Write-ahead trust restoration, separate from shared Bluetooth bonds."""

    def __init__(self, path: Path):
        self.path = path
        self.records: dict[str, AccessRecord] = {}
        self._write = asyncio.Lock()

    @property
    def devices(self):
        return {a: r.alias for a, r in self.records.items() if r.state == "allowed"}

    async def load(self):
        def read():
            if not self.path.exists():
                return {}
            if self.path.stat().st_size > 65536:
                raise ValueError("Bluetooth authorization file is too large")
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict):
                raise ValueError("Invalid Bluetooth authorization file")
            if value.get("version") == 2:
                return {
                    a.upper(): AccessRecord.model_validate(r)
                    for a, r in value["devices"].items()
                }
            if any(
                not isinstance(a, str) or not isinstance(r, str)
                for a, r in value.items()
            ):
                raise ValueError("Invalid Bluetooth authorization file")
            # Test images did not record prior trust. Never invent a baseline.
            return {
                a.upper(): AccessRecord(alias=name, state="allowed")
                for a, name in value.items()
            }

        self.records = await asyncio.to_thread(read)

    async def begin(self, address, alias, trusted):
        address = address.upper()
        async with self._write:
            existing = self.records.get(address)
            if existing is not None:
                if existing.state in ("revoked", "forgetting"):
                    raise ReceiverError(
                        "backend_unavailable",
                        "Previous audio-access cleanup is still pending",
                    )
                return
            await self._persist(
                {
                    **self.records,
                    address: AccessRecord(alias=alias, trust_before_grant=trusted),
                }
            )

    async def commit(self, address):
        await self._state(address, "allowed")

    async def forget(self, address):
        await self._state(address, "revoked")

    async def _state(self, address, state):
        address = address.upper()
        async with self._write:
            record = self.records.get(address)
            if record is not None:
                await self._persist(
                    {
                        **self.records,
                        address: record.model_copy(update={"state": state}),
                    }
                )

    async def forget_bond(self, address, alias):
        async with self._write:
            record = self.records.get(address, AccessRecord(alias=alias))
            await self._persist(
                {
                    **self.records,
                    address: record.model_copy(update={"state": "forgetting"}),
                }
            )

    async def cleaned(self, address):
        async with self._write:
            record = self.records.get(address)
            if record is not None and record.state != "allowed":
                await self._persist(
                    {a: r for a, r in self.records.items() if a != address}
                )

    async def allow(self, address, alias):
        # Kept as a convenience for constructing test/legacy unknown grants.
        await self.begin(address, alias, None)
        await self.commit(address.upper())

    async def cleanup(self, backend, address):
        record = self.records.get(address)
        if record is None or record.state == "allowed":
            return
        if record.state == "forgetting":
            raise ReceiverError(
                "backend_unavailable",
                "Device removal must finish under the Bluetooth setup lease",
            )
        peer = backend.devices().get(address)
        if peer is not None:
            path, props = peer
            if (
                record.trust_before_grant is not None
                and props.get("Trusted") != record.trust_before_grant
            ):
                await backend.set_property(
                    BLUEZ, path, DEVICE, "Trusted", "b", record.trust_before_grant
                )
            await backend.disconnect_audio(path)
        await self.cleaned(address)

    async def _persist(self, records):
        def write():
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump(
                    {
                        "version": 2,
                        "devices": {a: r.model_dump() for a, r in records.items()},
                    },
                    out,
                )
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)

        task = asyncio.create_task(asyncio.to_thread(write))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            self.records = records
            raise
        self.records = records


class PairingWindow:
    def __init__(self, backend, access: AudioAccess, runtime: Path):
        self.backend, self.access, self.runtime = backend, access, runtime
        self.active = False
        self.window_id = ""
        self.deadline = 0.0
        self.candidates: dict[str, str] = {}
        self.completed_address = None
        self._existing_endpoints = set()
        self._lock_fd = None
        self._timer = None
        self._registered = False
        self._old: dict = {}
        self._tasks: set[asyncio.Task] = set()
        self._guard = asyncio.Lock()
        self._new_bonds: set[str] = set()
        self._existing_audio_devices: set[str] = set()
        self.notice: str | None = None

    @property
    def adapter(self):
        return f"/org/bluez/{self.backend.adapter}"

    @property
    def remaining_seconds(self):
        return max(0.0, self.deadline - monotonic()) if self.active else 0.0

    def admits(self, address):
        return self.active and self.remaining_seconds > 0 and address in self.candidates

    async def rename(self, name):
        """The supervisor temporarily owns the alias while provisioning."""
        async with self._guard:
            fd = None
            if self._lock_fd is None:
                self.runtime.mkdir(parents=True, exist_ok=True)
                fd = os.open(
                    self.runtime / f"bluetooth-{self.backend.adapter}.lock",
                    os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC,
                    0o644,
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    return False
            try:
                await self.backend.set_property(
                    BLUEZ, self.adapter, ADAPTER, "Alias", "s", name
                )
                self.backend.bluez.setdefault(self.adapter, {}).setdefault(ADAPTER, {})[
                    "Alias"
                ] = name
                return True
            finally:
                if fd is not None:
                    os.close(fd)

    async def start(self, seconds=120):
        if not 0 < seconds <= 120:
            raise ValueError("Pairing must be bounded to at most two minutes")
        async with self._guard:
            if self.active:
                return
            self.runtime.mkdir(parents=True, exist_ok=True)
            path = self.runtime / f"bluetooth-{self.backend.adapter}.lock"
            fd = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise RuntimeError(
                    "Bluetooth setup is using this adapter; try pairing after setup finishes"
                ) from None
            except BaseException:
                os.close(fd)
                raise
            self._lock_fd = fd
            try:
                self._new_bonds.clear()
                self._existing_audio_devices = self.backend.audio_devices()
                self._existing_endpoints = {
                    e.identity for e in self.backend.endpoints()
                }
                response = await self.backend.call(
                    BLUEZ,
                    self.adapter,
                    PROPERTIES,
                    "GetAll",
                    "s",
                    ["org.bluez.Adapter1"],
                )
                props = plain(response.body[0])
                if not props.get("Powered"):
                    raise RuntimeError("Bluetooth adapter is powered off")
                self._old = {
                    key: props[key]
                    for key in ("Pairable", "Discoverable", "DiscoverableTimeout")
                }
                self.backend.agent_handler = self._agent
                await self.backend.call(
                    BLUEZ,
                    "/org/bluez",
                    "org.bluez.AgentManager1",
                    "RegisterAgent",
                    "os",
                    [AGENT, "NoInputNoOutput"],
                )
                self._registered = True
                await self.backend.call(
                    BLUEZ,
                    "/org/bluez",
                    "org.bluez.AgentManager1",
                    "RequestDefaultAgent",
                    "o",
                    [AGENT],
                )
                self.active = True
                self.window_id = uuid4().hex
                self.deadline = monotonic() + seconds
                self.completed_address = None
                self.notice = None
                await self.backend.set_property(
                    BLUEZ, self.adapter, "org.bluez.Adapter1", "Pairable", "b", True
                )
                await self.backend.set_property(
                    BLUEZ,
                    self.adapter,
                    "org.bluez.Adapter1",
                    "DiscoverableTimeout",
                    "u",
                    seconds,
                )
                await self.backend.set_property(
                    BLUEZ, self.adapter, "org.bluez.Adapter1", "Discoverable", "b", True
                )
                self._timer = asyncio.create_task(self._expire(seconds))
                self.backend.changed.set()
            except BaseException:
                await self._restore()
                raise

    async def _expire(self, seconds):
        await asyncio.sleep(seconds)
        try:
            await self.close()
        except Exception:
            # Denial is durable and the admission watcher retries cleanup.
            self.backend.changed.set()

    def _cancel_requests(self):
        for task in self._tasks:
            task.cancel()
        self.candidates.clear()
        self._new_bonds.clear()
        self.backend.changed.set()

    async def _admit(self, device, props, window_id):
        if not self.active or not self.remaining_seconds or self.window_id != window_id:
            raise ReceiverError("stale_target", "Pairing window expired")
        address = props["Address"].upper()
        if address in self.access.devices:
            return address
        if self.candidates.get(address) == device:
            return address
        if self.candidates and self.candidates.get(address) != device:
            raise ReceiverError("pairing_busy", "Another device is pairing")
        self.candidates[address] = device
        if not props.get("Paired"):
            self._new_bonds.add(address)
        try:
            await self.access.begin(
                address, props.get("Alias", address), bool(props.get("Trusted"))
            )
            if (
                not self.active
                or not self.remaining_seconds
                or self.window_id != window_id
            ):
                raise ReceiverError("stale_target", "Pairing window expired")
            await self.backend.set_property(BLUEZ, device, DEVICE, "Trusted", "b", True)
        except BaseException:
            self.candidates.pop(address, None)
            self._new_bonds.discard(address)
            raise
        return address

    def _agent(self, message):
        if message.path != AGENT or message.interface != "org.bluez.Agent1":
            return None
        if message.sender != self.backend.owner:
            return Message.new_error(
                message, "org.bluez.Error.Rejected", "Only BlueZ may use this agent"
            )
        if message.member in ("Release", "Cancel"):
            self.notice = "Pairing cancelled"
            self._cancel_requests()
            if message.member == "Release":
                self._registered = False
                self.deadline = monotonic()
                if self._timer is not None:
                    self._timer.cancel()
                self._timer = asyncio.create_task(self._expire(0))
            return Message.new_method_return(message)
        if not self.active or not self.remaining_seconds:
            return Message.new_error(
                message, "org.bluez.Error.Rejected", "Audio pairing is closed"
            )
        # The explicit pairing window authorizes Just Works. Never invent a PIN
        # or silently confirm a numeric-comparison callback with this capability.
        if message.member not in ("RequestAuthorization", "AuthorizeService"):
            return Message.new_error(
                message,
                "org.bluez.Error.Rejected",
                "This receiver supports pairing without a code",
            )
        if message.member == "AuthorizeService" and (
            len(message.body) != 2 or message.body[1].lower() not in AUDIO_UUIDS
        ):
            return Message.new_error(
                message, "org.bluez.Error.Rejected", "Audio services only"
            )
        if self._tasks:
            return Message.new_error(
                message,
                "org.bluez.Error.Rejected",
                "Another pairing request is pending",
            )
        task = asyncio.create_task(self._authorize(message, self.window_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _authorize(self, message, window_id):
        try:
            device = message.body[0]
            if not device.startswith(self.adapter + "/dev_"):
                raise ValueError("Device belongs to another adapter")
            reply = await self.backend.call(
                BLUEZ, device, PROPERTIES, "GetAll", "s", [DEVICE]
            )
            await self._admit(device, plain(reply.body[0]), window_id)
            response = Message.new_method_return(message)
        except (Exception, asyncio.CancelledError) as error:
            self.notice = (
                "Pairing cancelled"
                if isinstance(error, asyncio.CancelledError)
                else str(error)
            )
            response = Message.new_error(
                message, "org.bluez.Error.Rejected", self.notice
            )
        if self.backend.bus is not None:
            await self.backend.bus.send(response)
        self.backend.changed.set()

    async def admit_connected(self):
        # A phone bonded during provisioning may reconnect without an agent
        # callback. Admit its fresh audio transport only inside this window.
        if not self.active or not self.remaining_seconds:
            return
        for address, (device, props) in self.backend.devices().items():
            record = self.access.records.get(address)
            if record and record.state in ("revoked", "forgetting"):
                continue
            if (
                device in self.backend.audio_devices() - self._existing_audio_devices
                and props.get("Paired")
                and address not in self.access.devices
                and (not self.candidates or self.candidates.get(address) == device)
            ):
                await self._admit(device, props, self.window_id)
                return

    async def complete_bond(self):
        # A new bond is a completed pairing even before the sender opens A2DP.
        # Existing provisioning bonds still need a fresh audio endpoint.
        async with self._guard:
            if self._tasks:
                return False
            for address in list(self._new_bonds):
                peer = self.backend.devices().get(address)
                if self.admits(address) and peer and peer[1].get("Paired"):
                    await self.access.commit(address)
                    self.completed_address = address
                    await self._close()
                    return True
        return False

    async def forget_device(self, address, expected_identity):
        """Durably forget a bond while excluding concurrent BLE provisioning."""
        async with self._guard:
            fd = None
            if self._lock_fd is None:
                self.runtime.mkdir(parents=True, exist_ok=True)
                fd = os.open(
                    self.runtime / f"bluetooth-{self.backend.adapter}.lock",
                    os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC,
                    0o644,
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    raise ReceiverError(
                        "adapter_busy",
                        "Bluetooth setup is using the adapter; try again when setup finishes",
                    ) from None
            try:
                if address in self.candidates:
                    raise ReceiverError(
                        "pairing_busy",
                        "Wait for this device to finish pairing, or stop pairing first",
                    )
                await self.backend.refresh()
                if self.backend.device_identity(address) != expected_identity:
                    raise ReceiverError(
                        "stale_target", "The phone's pairing has changed"
                    )
                peer = self.backend.devices().get(address)
                alias = peer[1].get("Alias", address) if peer else address
                await self.access.forget_bond(address, alias)
                if peer:
                    await self.backend.remove_device(
                        peer[0], address=address, expected_identity=expected_identity
                    )
                await self.access.cleaned(address)
                self.backend.changed.set()
            finally:
                if fd is not None:
                    os.close(fd)

    async def _restore(self):
        self.active = False
        self.backend.agent_handler = None
        try:
            if self._registered:
                try:
                    await self.backend.call(
                        BLUEZ,
                        "/org/bluez",
                        "org.bluez.AgentManager1",
                        "UnregisterAgent",
                        "o",
                        [AGENT],
                    )
                except Exception:
                    pass
                self._registered = False
            # Under the lease, restore only the properties this window changed.
            for name, value in self._old.items():
                try:
                    await self.backend.set_property(
                        BLUEZ,
                        self.adapter,
                        "org.bluez.Adapter1",
                        name,
                        "u" if name.endswith("Timeout") else "b",
                        value,
                    )
                except Exception:
                    pass
        finally:
            self._registered = False
            self._old.clear()
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None

    async def complete(self, endpoint):
        """Only a paired A2DP PCM endpoint commits a provisional grant."""
        async with self._guard:
            if (
                self._tasks
                or not self.admits(endpoint.address)
                or self.candidates[endpoint.address] != endpoint.device
                or endpoint.identity in self._existing_endpoints
            ):
                return False
            record = self.access.records.get(endpoint.address)
            if record is None or record.state != "pending":
                return False
            await self.access.commit(endpoint.address)
            self.completed_address = endpoint.address
            await self._close()
            return True

    async def close(self):
        async with self._guard:
            await self._close()

    async def _close(self):
        self.active = False
        timer, self._timer = self._timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
            for address in list(self.candidates):
                if address not in self.access.devices:
                    await self.access.forget(address)
                    await self.access.cleanup(self.backend, address)
        finally:
            self.candidates.clear()
            self._new_bonds.clear()
            await self._restore()
            self.backend.changed.set()
