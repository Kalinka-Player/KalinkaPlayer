"""An external engine's volume, scoped to its playback hold."""

import asyncio

from kalinka_plugin_sdk.datamodel import DeviceVolume
from kalinka_plugin_sdk.direct_playback import HoldEnded
from kalinka_plugin_sdk.ext_device import SupportedFunction
from kalinka_plugin_sdk.ext_device_events import VolumeChangedEvent

from .module_timeout import PLUGIN_CALL_TIMEOUT_S

_CANCEL_GRACE_S = 0.2


class ExternalVolumeDevice:
    """
    @brief The output device clients drive while an external engine's hold owns volume.

    Answers from the volume the plugin last reported and forwards changes to
    its ExternalVolumeControl one at a time. Changes queued behind one in
    flight are skipped once a newer one arrives, so a slider drag forwards
    only its latest level. close() cancels pending changes when the hold ends.

    @note Not thread-safe: use from the event loop that created it.
    """

    def __init__(self, session, controller, router):
        self._session = session
        self._controller = controller
        self._router = router
        self._volume = DeviceVolume(supported=False)
        self._emitter = router.emitter_for(session.control.plugin_id)
        self._lock = asyncio.Lock()
        self._latest = 0
        self._pending: set[asyncio.Task] = set()

    def report(self, volume: DeviceVolume) -> None:
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
        self._latest += 1
        task = asyncio.create_task(self._set_volume(volume, self._latest))
        self._pending.add(task)
        try:
            await asyncio.wait_for(task, PLUGIN_CALL_TIMEOUT_S)
        except asyncio.CancelledError:
            if task.cancelled() and not asyncio.current_task().cancelling():
                raise HoldEnded("external playback ended during the change") from None
            raise
        finally:
            self._pending.discard(task)

    async def _set_volume(self, volume: int, ticket: int) -> None:
        async with self._lock:
            if ticket != self._latest:
                return
            if not self._session.active or self._router.current() is not self:
                raise HoldEnded("external playback no longer owns volume")
            await self._controller.set_volume(volume)

    async def close(self) -> None:
        """Cancel pending changes, waiting briefly for a plugin slow to stop."""
        pending = list(self._pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=_CANCEL_GRACE_S)

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
