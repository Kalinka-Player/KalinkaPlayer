from typing import Optional
from kalinka_plugin_sdk.plugin import (
    OutputDevicePluginContext,
    OutputDevicePlugin,
)
from kalinka_plugin_sdk.events import PlayQueueEventType
from kalinka_plugin_sdk.ext_device import ExternalOutputDevice

from .config_model import KalinkaPluginMusiccastConfig
from .musiccast import KalinkaPluginMusiccastDevice


class KalinkaPluginMusiccast(OutputDevicePlugin):
    REQUIRES_SDK = ">=3.14,<4"
    PLUGIN_ID = "musiccast"
    CONFIG_MODEL = KalinkaPluginMusiccastConfig

    def __init__(self):
        self._device = None  #
        self._device_subscriptions = []

    def get_interface(self) -> Optional[ExternalOutputDevice]:
        return self._device

    async def setup(self, context: OutputDevicePluginContext) -> None:
        config = KalinkaPluginMusiccastConfig(**context.config.model_dump())
        self._device = KalinkaPluginMusiccastDevice(
            config, context.emitter, context.listener
        )
        # Start the device and its async tasks
        await self._device.start()

    async def shutdown(self) -> None:
        for subscription in self._device_subscriptions:
            subscription.unsubscribe()

        self._device_subscriptions.clear()

        # Terminate the device and all its async tasks
        if self._device:
            await self._device.terminate()

        self._device = None
