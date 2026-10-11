"""Which device module owns the output controls right now.

A renderer controls its own volume and power through the built-in renderer
device unless it has been delegated to another module — an amp wired
downstream of that renderer's output. Delegation is per renderer, so the
module clients talk to follows the active renderer instead of being fixed at
startup: switching renderers switches whose volume the slider drives.

Every device module shares one event bus, so the router also gates what
reaches clients. A module that is enabled but does not own the active
renderer stays live — a MusicCast amp reports its own volume changes whether
or not Kalinka is playing — but its events are dropped instead of being
broadcast as if they described the current output. When the owner changes,
:meth:`OutputDeviceRouter.resync` re-authors the bus state from whoever is in
charge now, so clients are not left showing the previous module's numbers.

A module that is missing, not READY, or not an output device resolves to None,
which every caller reads as "no control available".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Mapping, Optional

from kalinka_plugin_sdk import ModuleHealthState
from kalinka_plugin_sdk.datamodel import DeviceVolume, PlayerStateEnum
from kalinka_plugin_sdk.ext_device import ExternalOutputDevice, SupportedFunction
from kalinka_plugin_sdk.ext_device_events import ExtDeviceEvent, ExtDeviceState

from .renderer_output_device import RendererOutputPlugin
from .renderer_prefs import RendererPreferences
from .renderer_registry import RendererRegistry
from .renderer_sessions import SessionVolumePolicy

if TYPE_CHECKING:  # avoids a cycle: player_setup builds the router
    from kalinka_eventbus import EventBus

    from .player_setup import PreparedPlugin

logger = logging.getLogger(__name__.split(".")[-1])


class RoutedDeviceEmitter:
    """The emitter one device plugin is handed. Forwards to the shared bus
    only while that plugin owns the active renderer's output."""

    def __init__(self, plugin_id: str, bus: "EventBus", router: "OutputDeviceRouter"):
        self._plugin_id = plugin_id
        self._bus = bus
        self._router = router

    def _owns_output(self) -> bool:
        return self._router.current_name() == self._plugin_id

    def dispatch(self, event: ExtDeviceEvent) -> None:
        if not self._owns_output():
            return
        self._bus.dispatch(event)

    def set_initial_state(self, state: ExtDeviceState) -> None:
        if not self._owns_output():
            return
        self._bus.set_initial_state(state)


class _FilteredStream:
    """Passes through only what the accept check lets through."""

    def __init__(self, inner, accept: Callable[[object], bool]):
        self._inner = inner
        self._stream = None
        self._accept = accept

    async def __aenter__(self):
        self._stream = await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return await self._inner.__aexit__(exc_type, exc, tb)

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            item = await self._stream.__anext__()
            if self._accept(item):
                return item


def _playback_state(item) -> Optional[PlayerStateEnum]:
    """The player state an event carries: a state change holds it directly, a
    replay through its queue snapshot. None for anything else."""
    state = getattr(item, "state", None)
    if state is None:
        return None
    return getattr(getattr(state, "playback_state", state), "state", None)


class RoutedPlaybackListener:
    """The playback-event listener one device plugin is handed.

    A module that does not own the active renderer's output hears nothing: an
    amp wired to one renderer has no business reacting to a track playing on
    another, which would otherwise have it adjusting its own volume for
    somebody else's playback.

    The one exception is the end of a playback the module was told about.
    Selecting another renderer stops playback first, but the STOPPED that
    follows travels through the bus's fan-out and worker threads and may only
    reach the module after ownership has moved. Dropping it would strand the
    module mid-track — an amp left with a ReplayGain offset applied for a
    track that is no longer playing.
    """

    def __init__(self, plugin_id: str, bus, router: "OutputDeviceRouter"):
        self._plugin_id = plugin_id
        self._bus = bus
        self._router = router
        self._playing = False

    def _owns_output(self) -> bool:
        return self._router.current_name() == self._plugin_id

    def _accept(self, item) -> bool:
        state = _playback_state(item)
        if self._owns_output():
            if state is not None:
                self._playing = state is not PlayerStateEnum.STOPPED
            return True
        if self._playing and state is PlayerStateEnum.STOPPED:
            self._playing = False
            return True
        return False

    def stream(self, event_types):
        return _FilteredStream(self._bus.stream(event_types), self._accept)

    def subscribe(self, event_types, callback=None, listener_id=None):
        if callback is None:
            return self._bus.subscribe(event_types, None, listener_id)

        def _gated(item):
            if self._accept(item):
                callback(item)

        return self._bus.subscribe(event_types, _gated, listener_id)

    def unsubscribe(self, listener_id: str) -> None:
        self._bus.unsubscribe(listener_id)

    def get_snapshot(self):
        return self._bus.get_snapshot()


class OutputDeviceRouter:
    def __init__(
        self,
        registry: RendererRegistry,
        prefs: RendererPreferences,
        devices: Callable[[], Mapping[str, "PreparedPlugin"]],
        bus: Optional["EventBus"] = None,
    ):
        self._registry = registry
        self._prefs = prefs
        self._devices = devices
        self._bus = bus
        self._external = None

    def set_external(self, owner, plugin_id: str, device: ExternalOutputDevice) -> None:
        """Use an external engine's controls for the lifetime of its hold."""
        self._external = (owner, plugin_id, device)

    def clear_external(self, owner) -> None:
        if self._external is not None and self._external[0] is owner:
            self._external = None

    def current_name(self) -> str:
        """Plugin id of the module in charge, delegated or not."""
        active = self._registry.active_id()
        delegate = self._prefs.volume_control(active) if active else None
        if delegate:
            return delegate
        if self._external is not None and self._external[0].active:
            return self._external[1]
        return RendererOutputPlugin.PLUGIN_ID

    def current(self) -> Optional[ExternalOutputDevice]:
        if self._external is not None and self.current_name() == self._external[1]:
            return self._external[2]
        prepared = self._devices().get(self.current_name())
        if prepared is None or prepared.health_state is not ModuleHealthState.READY:
            return None
        interface = prepared.interface
        return interface if isinstance(interface, ExternalOutputDevice) else None

    def session_volume_policy(self, renderer_id: str) -> SessionVolumePolicy:
        """What SessionOpen should carry for this renderer.

        A downstream mapping fixes the renderer at unity for the session so
        attenuation happens exactly once. Without one, the renderer keeps its
        own mode, including persistent fixed output for a physical amp knob.
        """
        return SessionVolumePolicy(
            force_fixed_output=self._prefs.volume_control(renderer_id) is not None
        )

    def emitter_for(self, plugin_id: str) -> Optional[RoutedDeviceEmitter]:
        if self._bus is None:
            return None
        return RoutedDeviceEmitter(plugin_id, self._bus, self)

    def listener_for(self, plugin_id: str, bus) -> RoutedPlaybackListener:
        return RoutedPlaybackListener(plugin_id, bus, self)

    async def resync(self) -> None:
        """Publish the current owner's state, so clients stop showing the
        numbers of a module that no longer drives the output."""
        if self._bus is None:
            return
        device = self.current()
        if device is None:
            state = ExtDeviceState(
                power_on=False, volume=DeviceVolume(supported=False)
            )
        else:
            functions = device.supported_functions()
            try:
                volume = (
                    await device.get_volume()
                    if SupportedFunction.GET_VOLUME in functions
                    else DeviceVolume(supported=False)
                )
                power_on = (
                    await device.is_power_on()
                    if SupportedFunction.IS_POWER_ON in functions
                    else True
                )
            except Exception as e:
                logger.warning(
                    "Could not read state from '%s': %s", self.current_name(), e
                )
                return
            state = ExtDeviceState(power_on=power_on, volume=volume)
        self._bus.set_initial_state(state)
