from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pydantic import Field, model_validator

from kalinka_plugin_sdk import paths
from kalinka_plugin_sdk.config_feedback import ConfigOption
from kalinka_plugin_sdk.datamodel import EmptyList
from kalinka_plugin_sdk.dynamic_fields import DynamicFieldDecl
from kalinka_plugin_sdk.inputmodule import InputModule
from kalinka_plugin_sdk.live_audio import LivePolicy
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.module_health import ModuleHealthState, ModuleState
from kalinka_plugin_sdk.plugin import InputModulePlugin

from .backend import BLUEZ, BlueAlsaBackend, DEVICE, PCM, volume_percent
from .pairing import AudioAccess, PairingWindow
from .service import BluetoothService

logger = logging.getLogger(__name__)
_ACTION = {"importance": "simple", "one_shot": True, "apply_immediately": True}


class BluetoothConfig(ModuleConfig):
    name: str = Field(default="bluetooth", title="Bluetooth", frozen=True, exclude=True)
    enabled: bool = Field(
        default=False,
        title="Bluetooth audio input",
        json_schema_extra={"importance": "simple"},
    )
    adapter: str = Field(
        default="hci0", pattern=r"^hci[0-9]+$", title="Bluetooth adapter"
    )
    bluealsa_service: str = Field(
        default="org.bluealsa", pattern=r"^org\.bluealsa(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
    )
    pair_phone: bool = Field(
        default=False, title="Pair a phone for two minutes", json_schema_extra=_ACTION
    )
    authorize_device: str = Field(
        default="",
        title="Allow audio from a paired phone",
        json_schema_extra={**_ACTION, "dynamic_options": True},
    )
    forget_device: str = Field(
        default="",
        title="Revoke a phone's audio access",
        json_schema_extra={
            **_ACTION,
            "dynamic_options": True,
            "help": "Keeps its Bluetooth bond and Wi-Fi setup access.",
        },
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
        self.actions = {}

    def get_interface(self):
        return self.interface

    async def setup(self, context):
        self.context = context
        await self.access.load()
        if context.config.enabled:
            if context.live_audio is None or context.direct_playback is None:
                raise RuntimeError(
                    "Bluetooth needs a server with live PCM playback support"
                )
            self._queue_actions({"pair_phone", "authorize_device", "forget_device"})
            self.task = asyncio.create_task(self._run())

    def _queue_actions(self, changed):
        for key in ("pair_phone", "authorize_device", "forget_device"):
            value = getattr(self.context.config, key)
            if key in changed and value:
                self.actions[key] = value
        if self.backend:
            self.backend.changed.set()

    async def config_changed(self, changed):
        if self.context is None:
            return  # First enable is applied by the server's plugin restart.
        if "enabled" in changed:
            if not self.context.config.enabled:
                await self.shutdown()
                return
            if self.task is None:
                self.task = asyncio.create_task(self._run())
        self._queue_actions(changed)

    async def _apply_actions(self):
        actions, self.actions = self.actions, {}
        try:
            if actions.get("pair_phone"):
                await self.pairing.start()
            address = actions.get("authorize_device", "").upper()
            if address:
                peer = next(
                    (
                        (path, p[DEVICE])
                        for path, p in self.backend.bluez.items()
                        if DEVICE in p
                        and p[DEVICE].get("Paired")
                        and p[DEVICE].get("Address", "").upper() == address
                    ),
                    None,
                )
                if peer is None:
                    raise ValueError("Select a phone that is already paired")
                path, props = peer
                # Use BlueZ's object path: privacy addresses can differ from
                # the resolved address stored in Device1.Address.
                await self.backend.set_property(
                    BLUEZ, path, DEVICE, "Trusted", "b", True
                )
                await self.access.allow(address, props.get("Alias", address))
            if actions.get("forget_device"):
                await self.access.forget(actions["forget_device"])
            if actions:
                self.error = None
        except Exception as error:
            self.error = str(error)
            logger.warning("Bluetooth action failed: %s", error)

    async def _run(self):
        while True:
            backend = BlueAlsaBackend(
                self.context.config.adapter, self.context.config.bluealsa_service
            )
            reader = None
            pipe = None
            self.backend = backend
            try:
                await backend.connect()
                self.error = None
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
                    await self._apply_actions()
                    endpoints = [
                        e
                        for e in backend.endpoints()
                        if e.address in self.access.devices
                    ]
                    current = self.service.endpoint
                    selected = next(
                        (
                            e
                            for e in endpoints
                            if current
                            and e.path == current.path
                            and (e.running or not any(p.running for p in endpoints))
                        ),
                        None,
                    )
                    selected = selected or (endpoints[0] if endpoints else None)
                    if (
                        selected is None
                        or current is None
                        or selected.path != current.path
                        or selected.format != current.format
                        or reader is None
                        or reader.done()
                    ):
                        if reader is not None:
                            reader.cancel()
                            ended = reader.done()
                            await asyncio.gather(reader, return_exceptions=True)
                            reader = None
                            if ended:
                                # A closed PCM endpoint may linger in D-Bus
                                # briefly. Bound retries instead of spinning.
                                await asyncio.sleep(0.25)
                        if pipe is not None:
                            pipe.close()
                            pipe = None
                        same_capture = (
                            selected is not None
                            and current is not None
                            and selected.path == current.path
                            and selected.format != current.format
                        )
                        if not same_capture:
                            await self.service.disconnected()
                        if selected is not None:
                            await self.service.connected(selected)
                            pipe = await backend.open(selected)
                            reader = asyncio.create_task(
                                self._drain(pipe, self.service, backend)
                            )
                    elif selected is not None:
                        await self.service.connected(selected)
                    if selected is not None:
                        await self.service.update_metadata(backend.metadata(selected))
                        volume = (
                            backend.bluealsa.get(selected.path, {})
                            .get(PCM, {})
                            .get("Volume")
                        )
                        if volume is not None:
                            await self.service.input_volume(volume_percent(volume))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.error = f"Bluetooth receiver unavailable: {error}"
                logger.warning("%s", self.error)
            finally:
                if reader is not None:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                if pipe is not None:
                    pipe.close()
                pairing, self.pairing = self.pairing, None
                service, self.service = self.service, None
                try:
                    if pairing is not None:
                        await pairing.close()
                finally:
                    try:
                        if service is not None:
                            await service.close()
                    finally:
                        backend.close()
                        self.backend = None
            await asyncio.sleep(2)

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
        self.actions.clear()

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

    async def resolve_options(self, path):
        if path == "forget_device":
            return [
                ConfigOption(value=address, label=alias)
                for address, alias in self.access.devices.items()
            ]
        if path == "authorize_device":
            return [
                ConfigOption(
                    value=p[DEVICE]["Address"],
                    label=p[DEVICE].get("Alias", p[DEVICE]["Address"]),
                )
                for p in (self.backend.bluez.values() if self.backend else [])
                if DEVICE in p and p[DEVICE].get("Paired")
            ]
        raise KeyError(path)
