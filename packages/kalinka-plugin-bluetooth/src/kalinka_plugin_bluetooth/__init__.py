from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from time import monotonic
from typing import ClassVar
from uuid import uuid4

from pydantic import Field, model_validator

from kalinka_plugin_sdk import ConfigIssue, paths
from kalinka_plugin_sdk.datamodel import EmptyList
from kalinka_plugin_sdk.dynamic_fields import DynamicFieldDecl
from kalinka_plugin_sdk.inputmodule import InputModule
from kalinka_plugin_sdk.live_audio import LivePolicy
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.module_health import ModuleHealthState, ModuleState
from kalinka_plugin_sdk.plugin import InputModulePlugin

from kalinka_plugin_sdk.receiver_control import (
    ReceiverDescriptor,
    ReceiverSnapshot,
    ReceiverAction,
    ReceiverPeer,
    ReceiverSession,
    PairingState,
    ReceiverError,
)
from .backend import ADAPTER, BLUEZ, BlueAlsaBackend, DEVICE, PCM, volume_percent
from .pairing import AudioAccess, PairingWindow
from .service import BluetoothService

logger = logging.getLogger(__name__)
_SIMPLE = {"importance": "simple"}
# Taken in by config_changed; any other setting is read when the backend connects.
_LIVE = frozenset({"enabled", "device_name"})


class BluetoothConfig(ModuleConfig):
    __module_icon__: ClassVar[str] = "bluetooth"

    name: str = Field(
        default="bluetooth",
        title="Bluetooth",
        description=(
            "Play from a phone or computer over Bluetooth A2DP. Kalinka "
            "receives the audio and routes it to the renderer you've selected."
        ),
        frozen=True,
        exclude=True,
    )
    enabled: bool = Field(
        default=False,
        title="Bluetooth audio input",
        json_schema_extra=_SIMPLE,
    )
    adapter: str = Field(
        default="hci0", pattern=r"^hci[0-9]+$", title="Bluetooth adapter"
    )
    bluealsa_service: str = Field(
        default="org.bluealsa", pattern=r"^org\.bluealsa(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
    )
    device_name: str = Field(
        default="",
        max_length=50,
        title="Bluetooth name",
        description="Leave empty to use the streamer name.",
        json_schema_extra=_SIMPLE,
    )
    latency_ms: int = Field(default=60, ge=20, le=500, title="Live latency target (ms)")
    max_backlog_ms: int = Field(
        default=250, ge=21, le=2000, title="Maximum live backlog (ms)"
    )

    @model_validator(mode="after")
    def policy_is_valid(self):
        LivePolicy(self.latency_ms, self.max_backlog_ms)
        return self


class BluetoothInput(InputModule):
    def module_name(self):
        return "bluetooth"

    def display_name(self):
        return "Bluetooth"

    async def browse(self, entity_id, offset=0, limit=50, filter=None):
        return EmptyList(offset, limit)

    async def search(self, type, query, offset=0, limit=50):
        return EmptyList(offset, limit)

    async def get_track_info(self, track_ids):
        return []

    async def get_content_info(self, asset_id):
        return None


class KalinkaPluginBluetooth(InputModulePlugin):
    PLUGIN_ID = "bluetooth"
    REQUIRES_SDK = ">=3.21,<4"
    SETUP_WHEN_DISABLED = True
    RECEIVER = ReceiverDescriptor(
        title="Bluetooth",
        kind="bluetooth",
        instructions="Select this name in your device's Bluetooth settings.",
        commands=(
            "open_pairing",
            "close_pairing",
            "grant_audio",
            "revoke_audio",
            "disconnect_audio",
            "forget_device",
        ),
    )
    CONFIG_MODEL = BluetoothConfig
    DYNAMIC_FIELDS = {
        "receiver_status": DynamicFieldDecl(section_id="", label="Bluetooth input"),
        "pairing_status": DynamicFieldDecl(section_id="", label="Pairing"),
        "paired_devices": DynamicFieldDecl(
            section_id="", label="Paired phones", value_type="list[str]"
        ),
    }

    def __init__(self):
        self.context = None
        self.interface = BluetoothInput()
        self.task = None
        self.backend = self.service = self.pairing = None
        self.access = AudioAccess(
            Path(paths.state_dir()) / "bluetooth" / "audio-access.json"
        )
        self.error = None
        self._lock = asyncio.Lock()
        self._reader = self._pipe = None
        self._connected = False
        self._peer_ids = {}
        self._peer_versions = {}
        self._sessions = {}
        self._cleanup_retry = {}
        self._name_pending = False
        self._retry_wake = asyncio.Event()

    def get_interface(self):
        return self.interface

    def get_receiver_control(self):
        return self

    async def validate_config(
        self, candidate: ModuleConfig, changed: frozenset[str]
    ) -> list[ConfigIssue]:
        # Staged settings are assigned to a copy without model validators.
        if candidate.latency_ms < candidate.max_backlog_ms:
            return []
        if "latency_ms" in changed:
            return [
                ConfigIssue(
                    path="latency_ms",
                    message="Keep the latency target below the maximum live backlog.",
                )
            ]
        return [
            ConfigIssue(
                path="max_backlog_ms",
                message="Set the maximum live backlog above the latency target.",
            )
        ]

    def _notify(self):
        callback = getattr(self.context, "receiver_changed", None)
        if callback:
            callback()

    def _peer_id(self, address):
        peer = (
            self.backend.devices().get(address)
            if self.backend and self._connected
            else None
        )
        if peer:
            version = self.backend.device_identity(address)
            if self._peer_versions.get(address) != version:
                self._peer_ids.pop(address, None)
                self._peer_versions[address] = version
        return self._peer_ids.setdefault(address, uuid4().hex)

    def _name(self):
        read = getattr(self.context, "server_name", None)
        value = self.context.config.device_name.strip() or (
            read() if read else "Kalinka"
        )
        return (
            "".join(c for c in value if ord(c) >= 32 and ord(c) != 127)[:50]
            or "Kalinka"
        )

    async def setup(self, context):
        self.context = context
        await self.access.load()
        # Observe admission even while disabled if the installed backend exposes
        # an A2DP service. No PCM is opened and no renderer is acquired then.
        self.task = asyncio.create_task(self._run())

    async def config_changed(self, changed):
        async with self._lock:
            if not self.context.config.enabled:
                if self.pairing:
                    await self.pairing.close()
                await self._stop_capture()
            if self.backend and self._connected:
                await self._reconcile(self.backend)
        if "enabled" in changed and self.context.config.enabled and not self._connected:
            self._retry_wake.set()
        self._notify()
        return _LIVE.issuperset(changed) or None

    async def server_name_changed(self):
        if not self.context.config.device_name.strip():
            await self.config_changed(frozenset({"device_name"}))

    def _require_backend(self):
        if not self.backend or not self._connected:
            raise ReceiverError(
                "backend_unavailable", self.error or "Bluetooth receiver is starting"
            )
        return self.backend

    async def _update_name(self):
        name = self._name()
        props = self.backend.bluez.get(self.pairing.adapter, {}).get(ADAPTER, {})
        if props.get("Alias") == name:
            self._name_pending = False
        elif props:
            self._name_pending = not await self.pairing.rename(name)
        else:
            raise ReceiverError(
                "backend_unavailable", "Bluetooth adapter is unavailable"
            )

    def _sync_sessions(self, endpoints):
        old = {e.identity: token for token, e in self._sessions.items()}
        self._sessions = {old.get(e.identity, uuid4().hex): e for e in endpoints}

    async def _reconcile(self, backend):
        if not self.context.config.enabled:
            # Also reconcile here if Core timed out a config callback while
            # it was waiting for protocol cleanup or the receiver lock.
            if self.pairing.active:
                await self.pairing.close()
            await self._stop_capture()
        if self.context.config.enabled:
            await self._update_name()
            await self.pairing.admit_connected()
            await self.pairing.complete_bond()
        endpoints = backend.endpoints()
        if self.context.config.enabled:
            for endpoint in endpoints:
                if await self.pairing.complete(endpoint):
                    break
        # Roll back interrupted/provisional grants unless the live window owns
        # them. A failed disconnect remains denied and is retried with a bound.
        for address, record in list(self.access.records.items()):
            if record.state != "allowed" and not self.pairing.admits(address):
                if monotonic() >= self._cleanup_retry.get(address, 0):
                    try:
                        if record.state == "forgetting":
                            peer = backend.devices().get(address)
                            if peer and peer[1].get("Paired"):
                                # Never replay a destructive RemoveDevice after
                                # a timeout/restart: this may now be a new bond.
                                raise ReceiverError(
                                    "backend_timeout",
                                    "Device removal has not completed. Try Forget again.",
                                )
                            await self.access.cleaned(address)
                            self._peer_ids.pop(address, None)
                            self._peer_versions.pop(address, None)
                        else:
                            await self.access.cleanup(backend, address)
                        self._cleanup_retry.pop(address, None)
                    except Exception as error:
                        self.error = str(error)
                        self._cleanup_retry[address] = monotonic() + 5
        peers = backend.devices()
        for device in backend.audio_devices():
            address = next((a for a, (p, _) in peers.items() if p == device), "")
            allowed = self.context.config.enabled and (
                address in self.access.devices or self.pairing.admits(address)
            )
            if not allowed and monotonic() >= self._cleanup_retry.get(device, 0):
                try:
                    await backend.disconnect_audio(device)
                    self._cleanup_retry.pop(device, None)
                except Exception as error:
                    self.error = str(error)
                    self._cleanup_retry[device] = monotonic() + 5
        active_cleanup = set(self.access.records) | set(backend.audio_devices())
        self._cleanup_retry = {
            k: v for k, v in self._cleanup_retry.items() if k in active_cleanup
        }
        if not self._cleanup_retry:
            self.error = None
        self._sync_sessions(backend.endpoints())
        self._notify()

    async def snapshot(self):
        enabled = bool(self.context and self.context.config.enabled)
        state = (
            "error"
            if not enabled and self._cleanup_retry
            else (
                "disabled"
                if not enabled
                else (
                    "error"
                    if self.error
                    else "ready"
                    if self._connected
                    else "starting"
                )
            )
        )
        backend = self.backend if self._connected else None
        devices = backend.devices() if backend else {}
        peers = []
        for address in sorted(set(devices) | set(self.access.records)):
            props = devices.get(address, (None, {}))[1]
            record = self.access.records.get(address)
            if not props.get("Paired"):
                continue
            peers.append(
                ReceiverPeer(
                    peer_id=self._peer_id(address),
                    name=props.get("Alias") or (record.alias if record else address),
                    paired=bool(props.get("Paired", record is not None)),
                    audio_allowed=address in self.access.devices,
                    connected=any(
                        e.address == address for e in self._sessions.values()
                    ),
                )
            )
        pairing = self.pairing
        active = pairing is not None and pairing.active
        recent = pairing.completed_address if pairing else None
        available = enabled and backend is not None
        availability = {
            "open_pairing": available,
            "close_pairing": backend is not None and active,
            "grant_audio": available and bool(peers),
            "revoke_audio": bool(peers),
            "disconnect_audio": backend is not None and bool(self._sessions),
            "forget_device": backend is not None and bool(peers),
        }
        actions = [
            ReceiverAction(
                command=name,
                available=availability[name],
                reason=(
                    None
                    if availability[name]
                    else "This action is not available in the current receiver state"
                ),
            )
            for name in self.RECEIVER.commands
            # Retain old commands for older API clients, but do not offer a
            # separate audio-permission workflow in the connection UI.
            if name not in ("grant_audio", "revoke_audio")
        ]
        effective = (
            backend.bluez.get(f"/org/bluez/{backend.adapter}", {})
            .get(ADAPTER, {})
            .get("Alias")
            if backend
            else None
        )
        return ReceiverSnapshot(
            enabled=enabled,
            state=state,
            message=(self.error if enabled or self._cleanup_retry else None)
            or (pairing.notice if pairing else None)
            or (
                "Name will update after Bluetooth setup finishes"
                if self._name_pending
                else None
            ),
            effective_name=effective or self._name(),
            name_inherited=not bool(self.context.config.device_name.strip()),
            visibility="advertising" if active else "hidden" if backend else "unknown",
            pairing=(
                PairingState(
                    window_id=pairing.window_id,
                    remaining_seconds=min(120, pairing.remaining_seconds),
                )
                if active
                else None
            ),
            recent_peer_id=self._peer_id(recent) if recent else None,
            pairing_mode="just_works",
            peers=peers,
            actions=actions,
            sessions=[
                ReceiverSession(
                    session_id=token,
                    peer_id=self._peer_id(e.address),
                    name=e.alias,
                    playing=e.running,
                )
                for token, e in self._sessions.items()
            ],
        )

    async def execute(self, command):
        try:
            async with self._lock:
                if (
                    command.command in ("open_pairing", "grant_audio")
                    and not self.context.config.enabled
                ):
                    raise ReceiverError("plugin_disabled", "Enable Bluetooth first")
                if command.command in ("grant_audio", "revoke_audio"):
                    address = next(
                        (
                            a
                            for a, token in self._peer_ids.items()
                            if token == command.peer_id
                        ),
                        None,
                    )
                    if address is None:
                        raise ReceiverError("stale_target", "Refresh the device list")
                    if command.command == "revoke_audio":
                        await self.access.forget(address)
                        if self.pairing:
                            self.pairing.candidates.pop(address, None)
                        if (
                            self.service
                            and self.service.endpoint
                            and self.service.endpoint.address == address
                        ):
                            await self._stop_capture()
                        backend = self._require_backend()
                        await self.access.cleanup(backend, address)
                        peer = backend.devices().get(address)
                        if peer:
                            await backend.disconnect_audio(peer[0])
                    else:
                        backend = self._require_backend()
                        await backend.refresh()
                        peer = backend.devices().get(address)
                        if peer is None or not peer[1].get("Paired"):
                            raise ReceiverError(
                                "stale_target", "The phone is no longer paired"
                            )
                        path, props = peer
                        await self.access.begin(
                            address,
                            props.get("Alias", address),
                            bool(props.get("Trusted")),
                        )
                        await backend.set_property(
                            BLUEZ, path, DEVICE, "Trusted", "b", True
                        )
                        await self.access.commit(address)
                elif command.command == "open_pairing":
                    self._require_backend()
                    await self._update_name()
                    if self._name_pending:
                        raise ReceiverError(
                            "adapter_busy", "Bluetooth setup is using the adapter"
                        )
                    try:
                        await self.pairing.start()
                    except RuntimeError as error:
                        raise ReceiverError("adapter_busy", str(error)) from error
                elif command.command == "forget_device":
                    backend = self._require_backend()
                    address = next(
                        (
                            a
                            for a, token in self._peer_ids.items()
                            if token == command.peer_id
                        ),
                        None,
                    )
                    if address is None:
                        raise ReceiverError("stale_target", "Refresh the device list")
                    await backend.refresh()
                    if self._peer_id(address) != command.peer_id:
                        raise ReceiverError(
                            "stale_target",
                            "The phone's pairing has changed; refresh the device list",
                        )
                    await self.pairing.forget_device(
                        address, backend.device_identity(address)
                    )
                    self._peer_ids.pop(address, None)
                    self._peer_versions.pop(address, None)
                    if (
                        self.service
                        and self.service.endpoint
                        and self.service.endpoint.address == address
                    ):
                        await self._stop_capture()
                elif command.command == "close_pairing":
                    if self.pairing and self.pairing.active:
                        if command.window_id != self.pairing.window_id:
                            raise ReceiverError(
                                "stale_target", "This pairing window has already ended"
                            )
                        await self.pairing.close()
                elif command.command == "disconnect_audio":
                    backend = self._require_backend()
                    await backend.refresh()
                    self._sync_sessions(backend.endpoints())
                    endpoint = self._sessions.get(command.session_id)
                    if endpoint is None and self._sessions:
                        raise ReceiverError(
                            "stale_target", "That audio session has ended"
                        )
                    if endpoint is not None:
                        if (
                            self.service
                            and self.service.endpoint
                            and self.service.endpoint.device == endpoint.device
                        ):
                            await self._stop_capture()
                        await backend.disconnect_audio(endpoint.device)
                else:
                    raise ReceiverError(
                        "unsupported_operation", "Unsupported Bluetooth action"
                    )
                if self.backend:
                    self._sync_sessions(self.backend.endpoints())
                    self.backend.changed.set()
                self.error = None
                return await self.snapshot()
        except Exception as error:
            if not isinstance(error, ReceiverError) or error.code.startswith("backend"):
                self.error = str(error)
            raise
        finally:
            self._notify()

    async def _stop_capture(self):
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        pipe, self._pipe = self._pipe, None
        if pipe is not None:
            pipe.close()
        if self.service:
            await self.service.disconnected()

    async def _run(self):
        retry_delay = 2.0
        last_error = None
        while True:
            backend = BlueAlsaBackend(
                self.context.config.adapter, self.context.config.bluealsa_service
            )
            self.backend = backend
            started = monotonic()
            try:
                await backend.connect()
                self.error = None
                self._connected = True
                self.service = BluetoothService(
                    self.context.live_audio,
                    self.context.direct_playback,
                    backend,
                    policy=LivePolicy(
                        self.context.config.latency_ms,
                        self.context.config.max_backlog_ms,
                    ),
                )
                self.pairing = PairingWindow(
                    backend,
                    self.access,
                    Path(paths.prefix()) / "run/lock/kalinka-bluetooth",
                )
                async for _ in backend.events():
                    async with self._lock:
                        await self._reconcile(backend)
                        endpoints = [
                            e
                            for e in backend.endpoints()
                            if self.context.config.enabled
                            and e.address in self.access.devices
                        ]
                        current = self.service.endpoint
                        selected = next(
                            (
                                e
                                for e in endpoints
                                if current
                                and e.identity == current.identity
                                and (e.running or not any(p.running for p in endpoints))
                            ),
                            None,
                        )
                        selected = selected or (endpoints[0] if endpoints else None)
                        if (
                            selected is None
                            or current is None
                            or selected.identity != current.identity
                            or selected.format != current.format
                            or self._reader is None
                            or self._reader.done()
                        ):
                            if self._reader is not None:
                                self._reader.cancel()
                                ended = self._reader.done()
                                await asyncio.gather(
                                    self._reader, return_exceptions=True
                                )
                                self._reader = None
                                if ended:
                                    # A closed PCM endpoint may linger in D-Bus
                                    # briefly. Bound retries instead of spinning.
                                    await asyncio.sleep(0.25)
                            if self._pipe is not None:
                                self._pipe.close()
                                self._pipe = None
                            same_capture = (
                                selected is not None
                                and current is not None
                                and selected.identity == current.identity
                                and selected.format != current.format
                            )
                            if not same_capture:
                                await self.service.disconnected()
                            if selected is not None:
                                await self.service.connected(selected)
                                self._pipe = await backend.open(selected)
                                self._reader = asyncio.create_task(
                                    self._drain(self._pipe, self.service, backend)
                                )
                        elif selected is not None:
                            await self.service.connected(selected)
                        if selected is not None:
                            await self.service.update_metadata(
                                backend.metadata(selected)
                            )
                            await self.service.update_activity(
                                backend.player_status(selected)
                            )
                            volume = (
                                backend.bluealsa.get(selected.path, {})
                                .get(PCM, {})
                                .get("Volume")
                            )
                            if volume is not None:
                                await self.service.input_volume(volume_percent(volume))
                        if last_error is not None:
                            logger.info("Bluetooth receiver available again")
                            last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.error = f"Bluetooth receiver unavailable: {error}"
                # Keep the failure visible in plugin status without filling
                # the log when an optional backend is absent for hours.
                if self.context.config.enabled and self.error != last_error:
                    logger.warning("%s", self.error)
                    last_error = self.error
            finally:
                self._connected = False
                self._sessions.clear()
                self._notify()
                if self._reader is not None:
                    self._reader.cancel()
                    await asyncio.gather(self._reader, return_exceptions=True)
                if self._pipe is not None:
                    self._pipe.close()
                self._reader = self._pipe = None
                pairing, self.pairing = self.pairing, None
                service, self.service = self.service, None
                try:
                    if pairing is not None:
                        try:
                            await pairing.close()
                        except Exception as error:
                            # The durable denied record retries trust/profile
                            # cleanup after the bus becomes available again.
                            logger.debug(
                                "Bluetooth pairing cleanup deferred: %s", error
                            )
                finally:
                    try:
                        if service is not None:
                            await service.close()
                    finally:
                        backend.close()
                        self.backend = None
            # A backend that repeatedly starts and immediately fails should
            # also back off. Reset only after a stable run, not just connect().
            if monotonic() - started >= 30:
                retry_delay = 2.0
            await self._backoff(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)

    async def _backoff(self, delay):
        sleeping = asyncio.create_task(asyncio.sleep(delay))
        waking = asyncio.create_task(self._retry_wake.wait())
        try:
            done, _ = await asyncio.wait(
                (sleeping, waking), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        finally:
            self._retry_wake.clear()
            for task in (sleeping, waking):
                task.cancel()
            await asyncio.gather(sleeping, waking, return_exceptions=True)

    async def _drain(self, pipe, service, backend):
        try:
            while pcm := await pipe.read():
                service.audio(pcm)
        except OSError as error:
            self.error = f"Bluetooth audio disconnected: {error}"
        finally:
            backend.changed.set()

    async def shutdown(self):
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._notify()

    async def get_state(self):
        if not self.context or not self.context.config.enabled:
            return ModuleState(state=ModuleHealthState.DISABLED)
        error = self.error or (self.service.output_error if self.service else None)
        return ModuleState(
            state=ModuleHealthState.WARNING if error else ModuleHealthState.READY,
            message=error
            or (self.service.status if self.service else "Connecting to BlueALSA"),
        )

    async def resolve_dynamic_field(self, path):
        if path == "receiver_status":
            return (await self.get_state()).message or "Disabled"
        if path == "pairing_status":
            return (
                "Pairing is open for two minutes"
                if self.pairing and self.pairing.active
                else "Pairing is closed"
            )
        if path == "paired_devices":
            return [
                f"{alias} ({address}) — audio allowed"
                for address, alias in self.access.devices.items()
            ]
        raise KeyError(path)
