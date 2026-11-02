"""Time-limited audio pairing, coordinated with BLE provisioning.

Both Kalinka clients take the same advisory lock before changing adapter-global
properties or the default agent. Capture does not hold the lease. Audio access
is remembered separately from BlueZ bonds, so forgetting audio never deletes a
Wi-Fi provisioning bond.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path

from dbus_fast import Message

from .backend import AUDIO_UUIDS, BLUEZ, DEVICE, PROPERTIES, plain

AGENT = "/org/kalinka/bluetooth_agent"


class AudioAccess:
    def __init__(self, path: Path):
        self.path = path
        self.devices: dict[str, str] = {}
        self._write = asyncio.Lock()

    async def load(self):
        def read():
            if not self.path.exists():
                return {}
            if self.path.stat().st_size > 65536:
                raise ValueError("Bluetooth authorization file is too large")
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict) or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in value.items()
            ):
                raise ValueError("Invalid Bluetooth authorization file")
            return {k.upper(): v for k, v in value.items()}

        self.devices = await asyncio.to_thread(read)

    async def allow(self, address: str, alias: str):
        async with self._write:
            updated = {**self.devices, address.upper(): alias}
            await self._persist(updated)
            self.devices = updated

    async def forget(self, address: str):
        async with self._write:
            updated = dict(self.devices)
            updated.pop(address.upper(), None)
            await self._persist(updated)
            self.devices = updated

    async def _persist(self, devices):
        def write():
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump(devices, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)

        await asyncio.to_thread(write)


class PairingWindow:
    def __init__(self, backend, access: AudioAccess, runtime: Path):
        self.backend, self.access, self.runtime = backend, access, runtime
        self.active = False
        self._lock_fd = None
        self._timer = None
        self._registered = False
        self._old: dict = {}
        self._tasks: set[asyncio.Task] = set()
        self._guard = asyncio.Lock()

    @property
    def adapter(self):
        return f"/org/bluez/{self.backend.adapter}"

    async def start(self, seconds=120):
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
            except BaseException:
                await self._restore()
                raise

    async def _expire(self, seconds):
        await asyncio.sleep(seconds)
        await self.close()

    def _agent(self, message):
        if message.path != AGENT or message.interface != "org.bluez.Agent1":
            return None
        if message.sender != self.backend.owner:
            return Message.new_error(
                message, "org.bluez.Error.Rejected", "Only BlueZ may use this agent"
            )
        if message.member in ("Release", "Cancel"):
            return Message.new_method_return(message)
        if not self.active or message.member not in (
            "RequestAuthorization",
            "RequestConfirmation",
            "AuthorizeService",
        ):
            return Message.new_error(
                message, "org.bluez.Error.Rejected", "Audio pairing is closed"
            )
        if message.member == "AuthorizeService" and (
            len(message.body) != 2 or message.body[1].lower() not in AUDIO_UUIDS
        ):
            return Message.new_error(
                message, "org.bluez.Error.Rejected", "Audio services only"
            )
        task = asyncio.create_task(self._authorize(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _authorize(self, message):
        try:
            device = message.body[0]
            if not device.startswith(self.adapter + "/dev_"):
                raise ValueError("Device belongs to another adapter")
            response = await self.backend.call(
                BLUEZ, device, PROPERTIES, "GetAll", "s", [DEVICE]
            )
            props = plain(response.body[0])
            if not self.active:
                raise ValueError("Pairing window expired")
            await self.access.allow(
                props["Address"], props.get("Alias", props["Address"])
            )
            # Trust permits reconnect after the temporary agent is removed.
            # Admission to Kalinka audio is still checked against AudioAccess.
            await self.backend.set_property(BLUEZ, device, DEVICE, "Trusted", "b", True)
            response = Message.new_method_return(message)
        except Exception as error:
            response = Message.new_error(
                message, "org.bluez.Error.Rejected", str(error)
            )
        if self.backend.bus is not None:
            await self.backend.bus.send(response)
        self.backend.changed.set()

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

    async def close(self):
        async with self._guard:
            timer, self._timer = self._timer, None
            if timer is not None and timer is not asyncio.current_task():
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
            self.active = False
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await self._restore()
