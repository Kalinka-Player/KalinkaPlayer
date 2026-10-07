"""Delegating a renderer's volume to another device module."""

import asyncio

from kalinka_eventbus import EventBus
from kalinka_plugin_sdk.datamodel import DeviceVolume, VolumeBackend
from kalinka_plugin_sdk.ext_device import SupportedFunction
from kalinka_plugin_sdk.ext_device_events import (
    ExtDeviceEvent,
    ExtDeviceEventType,
    ExtDeviceState,
    VolumeChangedEvent,
)
from kalinka_server.player_setup import (
    ModuleHealthState,
    PreparedPlugin,
    volume_control_modules,
)
from kalinka_server.output_device_router import OutputDeviceRouter
from kalinka_server.renderer_output_device import RendererVolumeDevice
from kalinka_server.renderer_prefs import RendererPreferences
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool, SessionVolumePolicy

from tests.sim_renderer import SimRenderer


class _Device:
    def __init__(self, *functions: SupportedFunction, volume: int = 0):
        self._functions = list(functions)
        self._volume = volume

    async def get_volume(self) -> DeviceVolume:
        return DeviceVolume(
            max_volume=100, current_volume=self._volume, volume_gain=0, supported=True
        )

    async def set_volume(self, volume: int) -> None:
        return None

    async def power_on(self) -> None:
        return None

    async def power_off(self) -> None:
        return None

    async def is_power_on(self) -> bool:
        return True

    def supported_functions(self) -> list[SupportedFunction]:
        return self._functions


def _prepared(interface, health=ModuleHealthState.READY) -> PreparedPlugin:
    return PreparedPlugin(
        plugin_class=type(interface) if interface else object,  # type: ignore[arg-type]
        plugin_instance=None,
        health_state=health,
        plugin_context=None,  # type: ignore[arg-type]
        interface=interface,
    )


def test_only_live_volume_capable_modules_are_candidates():
    devices = {
        "musiccast": _prepared(
            _Device(SupportedFunction.GET_VOLUME, SupportedFunction.SET_VOLUME)
        ),
        "screen-only": _prepared(_Device(SupportedFunction.POWER_ON)),
        "broken": _prepared(
            _Device(SupportedFunction.SET_VOLUME), health=ModuleHealthState.ERROR
        ),
        "disabled": _prepared(None, health=ModuleHealthState.DISABLED),
    }
    assert volume_control_modules(devices) == ["musiccast"]


def test_the_renderer_module_is_never_a_candidate():
    devices = {
        "kalinka-renderer": _prepared(_Device(SupportedFunction.SET_VOLUME)),
    }
    assert volume_control_modules(devices) == []


def _registry_with(
    *renderer_ids: str,
) -> tuple[RendererRegistry, RendererPreferences]:
    prefs = RendererPreferences()
    registry = RendererRegistry(prefs=prefs)
    for renderer_id in renderer_ids:
        registry.register(
            renderer_id=renderer_id,
            instance_id="inst-1",
            friendly_name=renderer_id,
            software_version="0.1.0",
            kind="native",
            platform={},
            session=object(),
        )
    return registry, prefs


async def test_router_follows_the_active_renderers_mapping():
    renderer_device, amp = _Device(SupportedFunction.SET_VOLUME), _Device(
        SupportedFunction.SET_VOLUME
    )
    devices = {
        "kalinka-renderer": _prepared(renderer_device),
        "musiccast": _prepared(amp),
    }
    registry, prefs = _registry_with("rid-a", "rid-b")
    router = OutputDeviceRouter(registry, prefs, lambda: devices)

    assert router.current() is renderer_device

    prefs.set_volume_control("rid-a", "musiccast")
    assert router.current() is amp

    # rid-b has no mapping, so selecting it hands control back to the renderer.
    registry.select("rid-b")
    assert router.current() is renderer_device
    await registry.shutdown()


async def test_router_yields_nothing_when_the_owning_module_is_down():
    devices = {
        "kalinka-renderer": _prepared(_Device(SupportedFunction.SET_VOLUME)),
        "musiccast": _prepared(
            _Device(SupportedFunction.SET_VOLUME), health=ModuleHealthState.ERROR
        ),
    }
    registry, prefs = _registry_with("rid-a")
    prefs.set_volume_control("rid-a", "musiccast")
    router = OutputDeviceRouter(registry, prefs, lambda: devices)

    assert router.current_name() == "musiccast"
    assert router.current() is None
    await registry.shutdown()


async def test_router_defaults_to_the_renderer_device_with_no_renderers():
    renderer_device = _Device(SupportedFunction.SET_VOLUME)
    registry, prefs = _registry_with()
    router = OutputDeviceRouter(
        registry, prefs, lambda: {"kalinka-renderer": _prepared(renderer_device)}
    )
    assert router.current() is renderer_device


async def test_mapped_renderers_request_fixed_unity():
    """Attenuating in the renderer *and* the amp would stack up, costing
    headroom and, in software mode, resolution."""
    registry, prefs = _registry_with("rid-a")
    prefs.set_volume_control("rid-a", "musiccast")
    router = OutputDeviceRouter(registry, prefs, lambda: {})
    assert router.session_volume_policy("rid-a") == SessionVolumePolicy(
        force_fixed_output=True
    )
    await registry.shutdown()


async def test_unmapped_renderers_keep_their_own_volume_mode():
    registry, prefs = _registry_with("rid-a")
    router = OutputDeviceRouter(registry, prefs, lambda: {})
    assert router.session_volume_policy("rid-a") == SessionVolumePolicy()
    await registry.shutdown()


async def test_an_unavailable_mapped_module_still_requests_fixed_unity():
    """The physical wiring does not change when its control plugin is down."""
    registry, prefs = _registry_with("rid-a")
    prefs.set_volume_control("rid-a", "musiccast")
    router = OutputDeviceRouter(
        registry,
        prefs,
        lambda: {
            "musiccast": _prepared(
                _Device(SupportedFunction.SET_VOLUME),
                health=ModuleHealthState.ERROR,
            )
        },
    )
    assert router.session_volume_policy("rid-a") == SessionVolumePolicy(
        force_fixed_output=True
    )
    await registry.shutdown()


def _bus() -> EventBus:
    return EventBus[ExtDeviceState, ExtDeviceEventType, ExtDeviceEvent](
        initial_state=ExtDeviceState(power_on=False, volume=DeviceVolume())
    )


def _volume_event(level: int) -> VolumeChangedEvent:
    return VolumeChangedEvent(
        volume=DeviceVolume(
            max_volume=100, current_volume=level, volume_gain=0, supported=True
        )
    )


async def test_only_the_owning_modules_events_reach_clients():
    """A live MusicCast reports its own volume changes whether or not it drives
    the current output; those must not be broadcast as the output's state."""
    devices = {
        "kalinka-renderer": _prepared(_Device(SupportedFunction.GET_VOLUME)),
        "musiccast": _prepared(_Device(SupportedFunction.GET_VOLUME)),
    }
    registry, prefs = _registry_with("rid-a")
    bus = _bus()
    router = OutputDeviceRouter(registry, prefs, lambda: devices, bus)
    renderer_emitter = router.emitter_for("kalinka-renderer")
    amp_emitter = router.emitter_for("musiccast")

    amp_emitter.dispatch(_volume_event(11))
    renderer_emitter.dispatch(_volume_event(22))
    assert bus.get_snapshot().volume.current_volume == 22

    prefs.set_volume_control("rid-a", "musiccast")
    renderer_emitter.dispatch(_volume_event(33))
    assert bus.get_snapshot().volume.current_volume == 22
    amp_emitter.dispatch(_volume_event(44))
    assert bus.get_snapshot().volume.current_volume == 44
    await registry.shutdown()


async def test_only_the_owning_module_hears_playback_events():
    """A MusicCast wired to one renderer must not react to a track playing on
    another — it would adjust its own volume for somebody else's playback."""
    from kalinka_plugin_sdk.datamodel import PlaybackMode, PlaybackState
    from kalinka_plugin_sdk.events import (
        PlayQueueEvent,
        PlayQueueEventType,
        PlayQueueState,
        PlaybackStateChangedEvent,
    )
    from kalinka_plugin_sdk.datamodel import PlayerStateEnum

    bus = EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent](
        initial_state=PlayQueueState(
            playback_state=PlaybackState(),
            track_list=[],
            playback_mode=PlaybackMode(
                shuffle=False, repeat_single=False, repeat_all=False
            ),
        )
    )
    registry, prefs = _registry_with("rid-a")
    router = OutputDeviceRouter(registry, prefs, lambda: {})
    heard: list = []
    listener = router.listener_for("musiccast", bus)
    listener.subscribe([PlayQueueEventType.PlaybackStateChanged], heard.append)

    def play():
        bus.dispatch(
            PlaybackStateChangedEvent(
                state=PlaybackState(state=PlayerStateEnum.PLAYING)
            )
        )

    play()
    await asyncio.sleep(0.05)
    assert heard == []  # the renderer owns the output, not musiccast

    prefs.set_volume_control("rid-a", "musiccast")
    play()
    await asyncio.sleep(0.05)
    assert len(heard) == 1
    bus.close()
    await registry.shutdown()


async def test_a_module_still_hears_the_end_of_the_playback_it_was_given():
    """Switching renderers stops playback first, but the STOPPED crosses the
    bus's threads and may land after ownership has moved. The module that was
    playing must still get it, or it stays set up for a track that ended."""
    from kalinka_plugin_sdk.datamodel import PlaybackMode, PlaybackState
    from kalinka_plugin_sdk.events import (
        PlayQueueEvent,
        PlayQueueEventType,
        PlayQueueState,
        PlaybackStateChangedEvent,
    )
    from kalinka_plugin_sdk.datamodel import PlayerStateEnum

    bus = EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent](
        initial_state=PlayQueueState(
            playback_state=PlaybackState(),
            track_list=[],
            playback_mode=PlaybackMode(
                shuffle=False, repeat_single=False, repeat_all=False
            ),
        )
    )
    registry, prefs = _registry_with("rid-a")
    prefs.set_volume_control("rid-a", "musiccast")
    router = OutputDeviceRouter(registry, prefs, lambda: {})
    heard: list = []
    listener = router.listener_for("musiccast", bus)
    listener.subscribe(
        [PlayQueueEventType.PlaybackStateChanged],
        lambda e: isinstance(e, PlaybackStateChangedEvent) and heard.append(e),
    )

    def dispatch(state):
        bus.dispatch(PlaybackStateChangedEvent(state=PlaybackState(state=state)))

    dispatch(PlayerStateEnum.PLAYING)
    await asyncio.sleep(0.05)
    assert [e.state.state for e in heard] == [PlayerStateEnum.PLAYING]

    # The switch: ownership moves, and the STOPPED for the playback that was
    # just ended arrives afterwards.
    prefs.set_volume_control("rid-a", None)
    dispatch(PlayerStateEnum.STOPPED)
    await asyncio.sleep(0.05)
    assert [e.state.state for e in heard] == [
        PlayerStateEnum.PLAYING,
        PlayerStateEnum.STOPPED,
    ]

    # And nothing from the playback that follows on the other renderer.
    dispatch(PlayerStateEnum.PLAYING)
    dispatch(PlayerStateEnum.STOPPED)
    await asyncio.sleep(0.05)
    assert len(heard) == 2
    bus.close()
    await registry.shutdown()


async def test_resync_publishes_the_new_owners_state():
    devices = {
        "kalinka-renderer": _prepared(
            _Device(SupportedFunction.GET_VOLUME, volume=10)
        ),
        "musiccast": _prepared(_Device(SupportedFunction.GET_VOLUME, volume=70)),
    }
    registry, prefs = _registry_with("rid-a")
    bus = _bus()
    router = OutputDeviceRouter(registry, prefs, lambda: devices, bus)

    await router.resync()
    assert bus.get_snapshot().volume.current_volume == 10

    prefs.set_volume_control("rid-a", "musiccast")
    await router.resync()
    assert bus.get_snapshot().volume.current_volume == 70
    await registry.shutdown()


async def test_resync_keeps_a_fixed_renderers_untouched_volume():
    """Fixed output has no level to set, yet backend none is what tells a client
    the samples are untouched; a switch's resync must not blank it to unknown."""
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    renderer = SimRenderer(registry, pool)
    renderer.volume = 100
    renderer.volume_supported = False
    renderer.connect()
    registry.select(SimRenderer.RENDERER_ID)
    bus = _bus()
    device = await RendererVolumeDevice(registry, pool, bus).start()
    router = OutputDeviceRouter(
        registry,
        RendererPreferences(),
        lambda: {"kalinka-renderer": _prepared(device)},
        bus,
    )
    try:
        session = await pool.open(SimRenderer.RENDERER_ID)
        await asyncio.sleep(0.05)
        await router.resync()

        volume = bus.get_snapshot().volume
        assert volume.backend is VolumeBackend.NONE
        assert volume.supported is False
        await session.close()
    finally:
        await device.shutdown()
        bus.close()
        await registry.shutdown()


async def test_resync_reports_no_volume_when_the_owner_is_down():
    devices = {
        "musiccast": _prepared(
            _Device(SupportedFunction.GET_VOLUME), health=ModuleHealthState.ERROR
        )
    }
    registry, prefs = _registry_with("rid-a")
    prefs.set_volume_control("rid-a", "musiccast")
    bus = _bus()
    router = OutputDeviceRouter(registry, prefs, lambda: devices, bus)

    await router.resync()
    assert bus.get_snapshot().volume.supported is False
    await registry.shutdown()
