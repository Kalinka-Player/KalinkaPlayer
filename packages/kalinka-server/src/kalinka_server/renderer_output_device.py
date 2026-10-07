"""Volume control for the active renderer, exposed as a local output device.

The wire only carries volume inside a playback session (``set_volume`` command,
``VolumeChanged`` state), and sessions exist only while something plays. The
device bridges that gap: it caches the last volume the renderer reported, so
clients always have numbers to show, and a level set while idle is kept pending
and pushed when the next session opens — pre-setting volume before play works.

The renderer owns its normal volume mode. When Core maps it to a downstream
device module, SessionOpen temporarily forces fixed unity output before the
first playback command and the renderer restores its configured mode when the
session ends.
"""

from __future__ import annotations

import logging
from typing import ClassVar, Optional

from pydantic import Field

from kalinka_plugin_sdk.datamodel import DeviceVolume, VolumeBackend
from kalinka_plugin_sdk.ext_device import ExternalOutputDevice, SupportedFunction
from kalinka_plugin_sdk.ext_device_events import (
    ExtDeviceEventType,
    ExtDeviceState,
    VolumeChangedEvent,
)
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import OutputDevicePlugin, OutputDevicePluginContext

from .renderer_registry import RendererRegistry
from .renderer_state import StateChange
from .renderer_sessions import (
    PlaybackSession,
    SessionPool,
    SessionState,
)

logger = logging.getLogger(__name__.split(".")[-1])

# Deliberately not full scale: before the first snapshot, a conservative UI
# placeholder is safer to show than 100%.
UNKNOWN_VOLUME_PLACEHOLDER = 30


class RendererOutputConfig(ModuleConfig):
    __module_icon__: ClassVar[str] = "speaker_outlined"
    name: str = Field(
        default="kalinka-renderer",
        title="Kalinka Renderer",
        description=(
            "Volume for Kalinka renderers — the speakers this server plays "
            "to directly, rather than a separate amplifier."
        ),
        frozen=True,
        exclude=True,
    )


_BACKENDS = {
    "none": VolumeBackend.NONE,
    "hardware": VolumeBackend.HARDWARE,
    "software": VolumeBackend.SOFTWARE,
}


class RendererVolumeDevice(ExternalOutputDevice):
    def __init__(
        self,
        registry: RendererRegistry,
        pool: SessionPool,
        event_emitter,
    ):
        self._registry = registry
        self._pool = pool
        self._event_emitter = event_emitter
        self._session: Optional[PlaybackSession] = None
        # Last volume each renderer reported, and levels set while one was
        # idle. Both keyed by renderer: selecting another renderer must show
        # and drive that one's volume, not the previous one's.
        self._volumes: dict[str, DeviceVolume] = {}
        self._pending: dict[str, int] = {}
        self._last_sent: Optional[tuple[Optional[str], DeviceVolume]] = None

    def _volume_for(self, renderer_id: Optional[str]) -> DeviceVolume:
        """A renderer nothing is known about reads as a conservative UI
        placeholder. Its actual session-start level is renderer-owned."""
        if renderer_id is not None and renderer_id in self._volumes:
            return self._volumes[renderer_id]
        return DeviceVolume(
            max_volume=100,
            current_volume=UNKNOWN_VOLUME_PLACEHOLDER,
            volume_gain=0,
            supported=True,
        )

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> "RendererVolumeDevice":
        self._pool.add_open_hook(self._on_session_open)
        self._event_emitter.set_initial_state(
            ExtDeviceState(
                power_on=True, volume=self._volume_for(self._registry.active_id())
            )
        )
        # A session may already be running (module reconfigured mid-play).
        renderer_id = self._registry.active_id()
        session = self._pool.get(renderer_id) if renderer_id else None
        if session is not None and session.state is SessionState.ACTIVE:
            await self._on_session_open(session)
        return self

    async def shutdown(self) -> None:
        self._pool.remove_open_hook(self._on_session_open)

    # --------------------------------------------------- ExternalOutputDevice API
    async def get_volume(self) -> DeviceVolume:
        return self._volume_for(self._registry.active_id()).model_copy()

    async def set_volume(self, volume: int) -> None:
        renderer_id = self._registry.active_id()
        current = self._volume_for(renderer_id)
        volume = max(0, min(int(volume), current.max_volume))
        session = self._session
        if (
            session is not None
            and session.state is SessionState.ACTIVE
            and session.renderer_id == renderer_id
        ):
            try:
                # The renderer echoes VolumeChanged; that drives cache + event.
                await session.set_volume(volume)
                return
            except Exception as e:
                logger.warning("Could not set renderer volume: %s", e)
        if renderer_id is not None:
            self._pending[renderer_id] = volume
            self._volumes[renderer_id] = current.model_copy(
                update={"current_volume": volume}
            )
        self._dispatch()

    async def power_on(self) -> None:
        return None

    async def is_power_on(self) -> bool:
        return True

    async def power_off(self) -> None:
        return None

    def supported_functions(self) -> list[SupportedFunction]:
        # Fixed output still reads: backend none is how clients know it is bit-perfect.
        if self._volume_for(self._registry.active_id()).supported:
            return [SupportedFunction.GET_VOLUME, SupportedFunction.SET_VOLUME]
        return [SupportedFunction.GET_VOLUME]

    # ------------------------------------------------------------------ internals
    async def _on_session_open(self, session: PlaybackSession) -> None:
        self._session = session
        session.on_state(self._on_session_state)
        session.on_closed(self._on_session_closed)
        # SessionOpenResult and the renderer's unsolicited StateSnapshot are
        # separate frames. The snapshot may already have reached the session
        # before this open hook adopts it (especially during a renderer
        # switch), in which case on_state() cannot replay it. Seed the cache
        # from the session's merged state; if the snapshot arrives later, the
        # callback above handles it normally.
        self._update_volume(session, session.snapshot)
        # A downstream mapping's fixed-output override rides SessionOpen, so it
        # is already in effect here. The renderer's own mode was never written.
        await self._apply_pending(session)

    async def _apply_pending(self, session: PlaybackSession) -> None:
        pending = self._pending.pop(session.renderer_id, None)
        if pending is None:
            return
        try:
            await session.set_volume(pending)
        except Exception as e:
            logger.warning("Could not apply pending volume: %s", e)

    async def _on_session_state(
        self, session: PlaybackSession, change: StateChange, snapshot: dict
    ) -> None:
        if session is not self._session:
            return
        if change not in (StateChange.VOLUME, StateChange.SNAPSHOT):
            return
        self._update_volume(session, snapshot)
        # A resumed session never re-runs the open hook; settle a level set
        # while the renderer was away.
        if (
            session.renderer_id in self._pending
            and session.state is SessionState.ACTIVE
        ):
            await self._apply_pending(session)

    def _update_volume(self, session: PlaybackSession, snapshot: dict) -> None:
        """Cache and publish the volume in a session's merged state, if any."""
        volume = snapshot.get("volume")
        if volume is None:
            return
        self._volumes[session.renderer_id] = DeviceVolume(
            max_volume=volume["max"] or 100,
            current_volume=volume["current"],
            volume_gain=0,
            supported=volume["supported"],
            backend=_BACKENDS.get(volume.get("backend") or "", VolumeBackend.UNKNOWN),
        )
        self._dispatch()

    def _on_session_closed(self, session: PlaybackSession, reason) -> None:
        if session is self._session:
            self._session = None

    def _dispatch(self) -> None:
        """Put the active renderer's cached volume on the device bus, skipping
        exact repeats (a snapshot restating the level must not re-notify every
        client). Repeats are per renderer: a newly active renderer reporting
        the level its predecessor last dispatched is news, not a repeat — the
        switch re-authored the bus state behind this dedup's back."""
        renderer_id = self._registry.active_id()
        volume = self._volume_for(renderer_id)
        if (renderer_id, volume) == self._last_sent:
            return
        self._last_sent = (renderer_id, volume)
        self._event_emitter.dispatch(
            VolumeChangedEvent.model_construct(
                event_type=ExtDeviceEventType.VolumeChanged,
                volume=volume,
            )
        )


class RendererOutputPlugin(OutputDevicePlugin):
    """Built-in output device for the connected renderer.

    Not loaded via entry points — ``player_setup`` constructs it and calls
    :meth:`bind` to inject the renderer services (which the plugin context
    can't carry), then runs the normal ``setup()``/``shutdown()`` lifecycle so
    it participates in the device registry and settings page.
    """

    REQUIRES_SDK = ">=3,<4"
    PLUGIN_ID = "kalinka-renderer"
    CONFIG_MODEL = RendererOutputConfig

    def __init__(self):
        self._device: Optional[RendererVolumeDevice] = None
        self._registry: Optional[RendererRegistry] = None
        self._pool: Optional[SessionPool] = None

    def bind(self, registry: RendererRegistry, pool: SessionPool) -> None:
        self._registry = registry
        self._pool = pool

    async def setup(self, context: OutputDevicePluginContext) -> None:
        if self._registry is None or self._pool is None:
            raise RuntimeError(
                "RendererOutputPlugin.bind() must be called before setup()"
            )
        self._device = RendererVolumeDevice(
            self._registry,
            self._pool,
            context.emitter,
        )
        await self._device.start()

    async def shutdown(self) -> None:
        if self._device is not None:
            await self._device.shutdown()
        self._device = None

    def get_interface(self) -> Optional[ExternalOutputDevice]:
        return self._device
