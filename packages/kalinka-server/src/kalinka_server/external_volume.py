"""An external engine's volume, scoped to its playback hold."""

import asyncio

from kalinka_plugin_sdk.datamodel import DeviceVolume
from kalinka_plugin_sdk.direct_playback import HoldEnded
from kalinka_plugin_sdk.ext_device import SupportedFunction
from kalinka_plugin_sdk.ext_device_events import VolumeChangedEvent

from .module_timeout import PLUGIN_CALL_TIMEOUT_S


class ExternalVolumeDevice:
    def __init__(self, session, controller, router):
        self._session = session
        self._controller = controller
        self._router = router
        self._volume = DeviceVolume(supported=False)
        self._emitter = router.emitter_for(session.control.plugin_id)
        self._lock = asyncio.Lock()

    def report(self, volume: DeviceVolume) -> None:
        if not self._session.active:
            raise HoldEnded("external playback has ended")
        volume = volume.model_copy(deep=True)
        if volume != self._volume:
            self._volume = volume
            if self._emitter is not None and self._router.current() is self:
                self._emitter.dispatch(VolumeChangedEvent(volume=volume))

    async def get_volume(self) -> DeviceVolume:
        return self._volume.model_copy(deep=True)

    async def set_volume(self, volume: int) -> None:
        if not self._session.active or self._router.current() is not self:
            raise HoldEnded("external playback no longer owns volume")
        if not self._volume.supported:
            raise NotImplementedError("external volume is fixed or unavailable")
        if not 0 <= volume <= self._volume.max_volume:
            raise ValueError("volume is outside the reported range")
        # Serialize changes so the final slider position wins even if network
        # replies arrive slowly. Cancellation on teardown prevents stale writes.
        task = asyncio.create_task(self._set_volume(volume))
        self._session._volume_tasks.add(task)
        try:
            await asyncio.wait_for(task, PLUGIN_CALL_TIMEOUT_S)
        finally:
            self._session._volume_tasks.discard(task)

    async def _set_volume(self, volume: int) -> None:
        async with self._lock:
            if not self._session.active or self._router.current() is not self:
                raise HoldEnded("external playback no longer owns volume")
            await self._controller.set_volume(volume)

    def supported_functions(self) -> list[SupportedFunction]:
        return (
            [SupportedFunction.GET_VOLUME, SupportedFunction.SET_VOLUME]
            if self._volume.supported
            else []
        )

    async def is_power_on(self) -> bool:
        return True

    async def power_on(self) -> None:
        raise NotImplementedError("external playback has no power control")

    async def power_off(self) -> None:
        raise NotImplementedError("external playback has no power control")
