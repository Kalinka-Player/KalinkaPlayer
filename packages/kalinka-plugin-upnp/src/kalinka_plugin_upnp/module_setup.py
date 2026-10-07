"""Lifecycle and health reporting for the bundled UPnP input plugin."""

import asyncio
import uuid
from pathlib import Path
from typing import ClassVar

from kalinka_plugin_sdk import paths
from kalinka_plugin_sdk.dynamic_fields import DynamicFieldDecl
from kalinka_plugin_sdk.module_health import ModuleHealthState, ModuleState
from kalinka_plugin_sdk.plugin import InputModulePlugin, PluginSetupException

from .config_model import UpnpConfig
from .discovery import local_address
from .server import Receiver
from .upnp import UpnpInput


def persistent_udn(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "uuid"
    try:
        with path.open("x") as stream:
            stream.write(str(uuid.uuid4()))
    except FileExistsError:
        pass
    return f"uuid:{uuid.UUID(path.read_text().strip())}"


class KalinkaPluginUpnp(InputModulePlugin):
    """Start the receiver when enabled and release its hold when unloaded."""

    PLUGIN_ID = "upnp"
    REQUIRES_SDK = ">=3.9,<4"
    CONFIG_MODEL = UpnpConfig
    DYNAMIC_FIELDS: ClassVar[dict[str, DynamicFieldDecl]] = {
        "receiver_status": DynamicFieldDecl(
            section_id="", label="Receiver status", widget="text"
        ),
    }

    def __init__(self):
        self.interface = UpnpInput()
        self.receiver = None
        self.enabled = False

    def get_interface(self):
        return self.interface

    async def setup(self, context):
        config = UpnpConfig(**context.config.model_dump())
        self.enabled = config.enabled
        if not config.enabled:
            return
        if context.direct_playback is None:
            raise PluginSetupException(
                "UPnP requires Kalinka direct playback and SDK 3.9 or later"
            )
        try:
            udn = await asyncio.to_thread(
                persistent_udn, Path(paths.state_dir()) / "upnp"
            )
            host = local_address(config.interface_address)
            self.receiver = Receiver(
                context.direct_playback, config.device_name, host, config.port, udn
            )
            await self.receiver.start()
        except (OSError, ValueError):
            self.receiver = None
            raise PluginSetupException(
                "Cannot start UPnP. Check the network interface, HTTP port, and state directory."
            ) from None

    async def resolve_dynamic_field(self, path):
        if path != "receiver_status":
            raise KeyError(path)
        if not self.enabled:
            return "Disabled"
        if not self.receiver:
            return "Receiver unavailable"
        playback = self.receiver.playback
        if playback.status == "ERROR_OCCURRED":
            return "Playback failed; check the selected renderer and media URL"
        if playback.active:
            return (
                "Paused"
                if playback.state == "PAUSED_PLAYBACK"
                else "Playing through the selected renderer"
            )
        return "Waiting for a UPnP controller"

    async def get_state(self):
        if not self.enabled:
            state = ModuleHealthState.DISABLED
        elif not self.receiver:
            state = ModuleHealthState.ERROR
        elif self.receiver.playback.status == "ERROR_OCCURRED":
            state = ModuleHealthState.WARNING
        else:
            state = ModuleHealthState.READY
        return ModuleState(
            state=state, message=await self.resolve_dynamic_field("receiver_status")
        )

    async def shutdown(self):
        receiver, self.receiver = self.receiver, None
        if receiver:
            await receiver.close()
