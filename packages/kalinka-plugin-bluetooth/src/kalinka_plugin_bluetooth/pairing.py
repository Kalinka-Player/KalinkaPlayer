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
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel
from kalinka_plugin_sdk.receiver_control import PairingRequest, ReceiverError

from dbus_fast import Message

from .backend import ADAPTER, AUDIO_UUIDS, BLUEZ, DEVICE, PROPERTIES, plain

AGENT = "/org/kalinka/bluetooth_agent"


@dataclass
class PendingPairing:
    view: PairingRequest
    device: str
    deadline: float
    answer: asyncio.Future | None = None


class AccessRecord(BaseModel):
    alias: str
    trust_before_grant: bool | None = None
    state: Literal["pending", "allowed", "revoked"] = "pending"


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
                if existing.state == "revoked":
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
        self.pending: PendingPairing | None = None
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
                    [AGENT, "DisplayYesNo"],
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

    @property
    def request(self):
        pending = self.pending
        if pending is None or not self.active:
            return None
        return pending.view.model_copy(
            update={
                "remaining_seconds": max(
                    0.0, min(self.remaining_seconds, pending.deadline - monotonic())
                )
            }
        )

    def _cancel_requests(self):
        self.pending = None
        for task in self._tasks:
            task.cancel()
        # A cancelled handshake must not leave provisional audio permission.
        self.candidates.clear()
        self.backend.changed.set()

    async def expire_request(self):
        pending = self.pending
        if (
            pending is not None
            and pending.answer is None
            and monotonic() >= pending.deadline
        ):
            self.pending = None
            self.notice = "Pairing approval timed out"
            self.backend.changed.set()
            await self.backend.call(BLUEZ, pending.device, DEVICE, "CancelPairing")

    async def answer(self, request_id, accept, code=None):
        pending = self.pending
        if (
            pending is None
            or pending.view.request_id != request_id
            or not self.active
            or not self.remaining_seconds
            or monotonic() >= pending.deadline
        ):
            raise ReceiverError(
                "stale_target", "This pairing request has already ended"
            )
        method = pending.view.method
        if accept:
            if pending.answer is None:
                raise ReceiverError(
                    "invalid_answer", "Enter the displayed code on your device"
                )
            if method == "passkey":
                if (
                    code is None
                    or len(code) != 6
                    or not code.isascii()
                    or not code.isdigit()
                ):
                    raise ReceiverError(
                        "invalid_answer", "Enter the six-digit code from your device"
                    )
            elif method == "pin":
                if (
                    not code
                    or len(code) > 16
                    or any(ord(c) < 32 or ord(c) == 127 for c in code)
                ):
                    raise ReceiverError(
                        "invalid_answer",
                        "Enter the PIN from your device (1–16 characters)",
                    )
            elif code is not None:
                raise ReceiverError(
                    "invalid_answer", "This request does not accept a code"
                )
        # Consume the token before yielding: a second client can never answer it.
        self.pending = None
        self.notice = None if accept else "Pairing rejected"
        if pending.answer is not None:
            pending.answer.set_result((accept, code))
        elif not accept:
            await self.backend.call(BLUEZ, pending.device, DEVICE, "CancelPairing")
        self.backend.changed.set()

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
                # End the window and restore the provisioning lease promptly.
                self.deadline = monotonic()
                if self._timer is not None:
                    self._timer.cancel()
                self._timer = asyncio.create_task(self._expire(0))
            return Message.new_method_return(message)
        methods = {
            "RequestConfirmation": "confirmation",
            "RequestAuthorization": "authorization",
            "RequestPinCode": "pin",
            "RequestPasskey": "passkey",
            "DisplayPinCode": "display_pin",
            "DisplayPasskey": "display_passkey",
            "AuthorizeService": "authorization",
        }
        if (
            not self.active
            or not self.remaining_seconds
            or message.member not in methods
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
        if self._tasks:
            return Message.new_error(
                message,
                "org.bluez.Error.Rejected",
                "Another pairing request is pending",
            )
        task = asyncio.create_task(
            self._authorize(message, methods[message.member], self.window_id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _authorize(self, message, method, window_id):
        pending = None
        address = None
        try:
            device = message.body[0]
            if not device.startswith(self.adapter + "/dev_"):
                raise ValueError("Device belongs to another adapter")
            reply = await self.backend.call(
                BLUEZ, device, PROPERTIES, "GetAll", "s", [DEVICE]
            )
            props = plain(reply.body[0])
            if (
                not self.active
                or not self.remaining_seconds
                or self.window_id != window_id
            ):
                raise ValueError("Pairing window expired")
            address = props["Address"].upper()
            approved = message.member == "AuthorizeService" and (
                address in self.access.devices or self.admits(address)
            )
            answer_code = None
            if not approved:
                code = None
                if method in ("confirmation", "display_passkey"):
                    value = message.body[1]
                    if type(value) is not int or not 0 <= value <= 999999:
                        raise ValueError("Invalid BlueZ comparison code")
                    code = f"{value:06d}"
                elif method == "display_pin":
                    code = str(message.body[1])
                display = method.startswith("display_")
                current = self.pending
                if current is not None and not (
                    current.device == device
                    and current.view.method.startswith("display_")
                ):
                    raise ValueError("Another pairing request is pending")
                pending = PendingPairing(
                    PairingRequest(
                        request_id=(
                            current.view.request_id
                            if current and display
                            else uuid4().hex
                        ),
                        device_name=props.get("Alias", address),
                        method=method,
                        code=code,
                        entered=message.body[2]
                        if method == "display_passkey"
                        else None,
                        remaining_seconds=min(60, self.remaining_seconds),
                    ),
                    device,
                    monotonic() + min(60, self.remaining_seconds),
                    None if display else asyncio.get_running_loop().create_future(),
                )
                self.pending = pending
                self.notice = None
                self.backend.changed.set()
                if not display:
                    accepted, answer_code = await asyncio.wait_for(
                        pending.answer, min(60, self.remaining_seconds)
                    )
                    if not accepted:
                        raise ValueError("Pairing rejected")
                    if (
                        not self.active
                        or not self.remaining_seconds
                        or self.window_id != window_id
                    ):
                        raise ValueError("Pairing window expired")
            if not method.startswith("display_"):
                if address not in self.access.devices:
                    self.candidates[address] = device
                    await self.access.begin(
                        address, props.get("Alias", address), bool(props.get("Trusted"))
                    )
                    if not self.active or not self.remaining_seconds:
                        raise ValueError("Pairing window expired")
                await self.backend.set_property(
                    BLUEZ, device, DEVICE, "Trusted", "b", True
                )
            signature, body = (
                ("s", [answer_code])
                if method == "pin"
                else ("u", [int(answer_code)])
                if method == "passkey"
                else ("", [])
            )
            response = Message.new_method_return(message, signature, body)
        except (Exception, asyncio.CancelledError) as error:
            if address is not None:
                self.candidates.pop(address, None)
            self.notice = (
                "Pairing approval timed out"
                if isinstance(error, TimeoutError)
                else "Pairing cancelled"
                if isinstance(error, asyncio.CancelledError)
                else str(error)
            )
            response = Message.new_error(
                message, "org.bluez.Error.Rejected", self.notice
            )
        finally:
            if (
                pending is not None
                and pending.answer is not None
                and self.pending is pending
            ):
                self.pending = None
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

    async def complete(self, endpoint):
        """Only a paired A2DP PCM endpoint commits a provisional grant."""
        async with self._guard:
            if (
                not self.admits(endpoint.address)
                or self.candidates[endpoint.address] != endpoint.device
                or endpoint.identity in self._existing_endpoints
            ):
                return False
            record = self.access.records.get(endpoint.address)
            if record is None or record.state != "pending":
                return False
            await self.access.commit(endpoint.address)
            self.completed_address = endpoint.address
            await self._close(cancel=False)
            return True

    async def close(self):
        async with self._guard:
            await self._close(cancel=True)

    async def _close(self, *, cancel):
        self.active = False
        timer, self._timer = self._timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        self.pending = None
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
            await self._restore()
            self.backend.changed.set()
