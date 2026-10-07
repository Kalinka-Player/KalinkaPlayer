"""The renderer volume device: cached volume while idle, pending apply at
session open, renderer events onto the device bus."""

from __future__ import annotations

import asyncio

import pytest

from kalinka_eventbus import EventBus
from kalinka_plugin_sdk.datamodel import DeviceVolume, VolumeBackend
from kalinka_plugin_sdk.ext_device import SupportedFunction
from kalinka_plugin_sdk.ext_device_events import (
    ExtDeviceEvent,
    ExtDeviceEventType,
    ExtDeviceState,
    VolumeChangedEvent,
)

from kalinka_server.renderer_output_device import RendererVolumeDevice
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool

from kalinka_server.renderer_proto import renderer_pb2 as pb

from tests.sim_renderer import SimRenderer

# Renderer echoes reach the device via detached callback tasks and the event
# bus delivers to subscribers off the dispatching task; one short yield lets
# both run.
SETTLE_S = 0.05


@pytest.fixture
def renderer():
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    sim = SimRenderer(registry, pool)
    sim.connect()
    return sim


@pytest.fixture
def bus():
    bus = EventBus[ExtDeviceState, ExtDeviceEventType, ExtDeviceEvent](  # type: ignore[type-var]
        initial_state=ExtDeviceState(power_on=False, volume=DeviceVolume())
    )
    yield bus
    bus.close()


class VolumeRecorder:
    def __init__(self, bus: EventBus):
        self.events: list[VolumeChangedEvent] = []
        bus.subscribe([ExtDeviceEventType.VolumeChanged], callback=self._on_item)

    def _on_item(self, item) -> None:
        if isinstance(item, VolumeChangedEvent):
            self.events.append(item)


def make_device(renderer: SimRenderer, bus: EventBus) -> RendererVolumeDevice:
    return RendererVolumeDevice(renderer.registry, renderer.pool, bus)


async def test_idle_volume_cached_and_applied_on_open(renderer, bus):
    recorder = VolumeRecorder(bus)
    device = await make_device(renderer, bus).start()
    try:
        await device.set_volume(25)
        assert (await device.get_volume()).current_volume == 25
        assert renderer.volume == 40  # nothing on the wire without a session
        await asyncio.sleep(SETTLE_S)
        assert recorder.events[-1].volume.current_volume == 25

        session = await renderer.pool.open(SimRenderer.RENDERER_ID)
        # Applied inside open(), before any playback command could follow.
        assert renderer.volume == 25
        await session.close()
    finally:
        await device.shutdown()


async def test_set_volume_through_open_session(renderer, bus):
    recorder = VolumeRecorder(bus)
    device = await make_device(renderer, bus).start()
    try:
        session = await renderer.pool.open(SimRenderer.RENDERER_ID)
        await device.set_volume(70)
        assert renderer.volume == 70
        await asyncio.sleep(SETTLE_S)  # the renderer's echo drives the cache
        assert (await device.get_volume()).current_volume == 70
        assert recorder.events[-1].volume.current_volume == 70
        await session.close()
    finally:
        await device.shutdown()


async def test_session_open_adopts_the_renderers_initial_volume(renderer, bus):
    """The initial snapshot can precede the device's open hook. Its volume
    must replace the unknown-state placeholder without another request."""
    renderer.volume = 64
    recorder = VolumeRecorder(bus)
    device = await make_device(renderer, bus).start()
    try:
        session = await renderer.pool.open(SimRenderer.RENDERER_ID)
        assert (await device.get_volume()).current_volume == 64
        await asyncio.sleep(SETTLE_S)
        assert recorder.events[-1].volume.current_volume == 64
        await session.close()
    finally:
        await device.shutdown()


async def test_reopen_replaces_a_stale_cached_volume_with_the_safe_start(
    renderer, bus
):
    """A renderer may lower its actual level while accepting SessionOpen.
    The UI must use that initial snapshot, not this renderer's previous level."""
    device = await make_device(renderer, bus).start()
    try:
        session = await renderer.pool.open(SimRenderer.RENDERER_ID)
        await device.set_volume(75)
        await asyncio.sleep(SETTLE_S)
        assert (await device.get_volume()).current_volume == 75
        await session.close()

        # Models the native renderer applying its 30% session-start ceiling
        # before sending the next session's unsolicited snapshot.
        renderer.volume = 30
        reopened = await renderer.pool.open(SimRenderer.RENDERER_ID)
        assert (await device.get_volume()).current_volume == 30
        await reopened.close()
    finally:
        await device.shutdown()


async def test_the_volume_mode_is_never_written_to_the_renderers_config(renderer, bus):
    """Normal mode is renderer-owned; only a downstream fixed-output override
    can ride SessionOpen."""
    device = await make_device(renderer, bus).start()
    try:
        session = await renderer.pool.open(SimRenderer.RENDERER_ID)
        assert renderer.config_updates == []
        await session.close()
    finally:
        await device.shutdown()


async def test_snapshot_syncs_cache_and_bus(renderer, bus):
    recorder = VolumeRecorder(bus)
    device = await make_device(renderer, bus).start()
    try:
        session = await renderer.pool.open(SimRenderer.RENDERER_ID)
        await asyncio.sleep(SETTLE_S)
        assert (await device.get_volume()).current_volume == 40
        assert recorder.events[-1].volume.current_volume == 40
        await session.close()
    finally:
        await device.shutdown()


async def test_unsupported_volume_reported(renderer, bus):
    renderer.volume_supported = False
    device = await make_device(renderer, bus).start()
    try:
        session = await renderer.pool.open(SimRenderer.RENDERER_ID)
        await asyncio.sleep(SETTLE_S)
        assert device.supported_functions() == [SupportedFunction.GET_VOLUME]
        assert (await device.get_volume()).supported is False
        await session.close()
    finally:
        await device.shutdown()


async def test_the_volume_names_the_backend_that_applies_it(renderer, bus):
    """Only software attenuation touches the samples, so a client weighing
    whether playback is bit-perfect has to know which backend is at work."""
    renderer.volume_backend = pb.VOLUME_BACKEND_SOFTWARE
    device = await make_device(renderer, bus).start()
    try:
        session = await renderer.pool.open(SimRenderer.RENDERER_ID)
        await asyncio.sleep(SETTLE_S)
        assert (await device.get_volume()).backend is VolumeBackend.SOFTWARE
        await session.close()
    finally:
        await device.shutdown()


async def test_a_renderer_yet_to_report_claims_no_backend(renderer, bus):
    """The placeholder level is a guess; naming a backend would be one too."""
    device = await make_device(renderer, bus).start()
    try:
        assert (await device.get_volume()).backend is VolumeBackend.UNKNOWN
    finally:
        await device.shutdown()


async def test_each_renderer_keeps_its_own_volume(renderer, bus):
    """Selecting another renderer must show and drive that one's level, not
    whatever the previous renderer was at."""
    other = SimRenderer(renderer.registry, renderer.pool, renderer_id="sim-other")
    other.connect()
    device = await make_device(renderer, bus).start()
    try:
        renderer.registry.select(SimRenderer.RENDERER_ID)
        await device.set_volume(25)
        assert (await device.get_volume()).current_volume == 25

        renderer.registry.select("sim-other")
        # Nothing known about it yet: the default level, not the other's 25.
        assert (await device.get_volume()).current_volume == 30
        await device.set_volume(80)

        renderer.registry.select(SimRenderer.RENDERER_ID)
        assert (await device.get_volume()).current_volume == 25
        renderer.registry.select("sim-other")
        assert (await device.get_volume()).current_volume == 80
    finally:
        await device.shutdown()


async def test_cache_survives_session_close(renderer, bus):
    device = await make_device(renderer, bus).start()
    try:
        session = await renderer.pool.open(SimRenderer.RENDERER_ID)
        await device.set_volume(55)
        await asyncio.sleep(SETTLE_S)
        await session.close()
        assert (await device.get_volume()).current_volume == 55
        assert device.supported_functions() == [
            SupportedFunction.GET_VOLUME,
            SupportedFunction.SET_VOLUME,
        ]
    finally:
        await device.shutdown()


async def test_a_new_renderer_reporting_the_same_level_is_still_news(bus):
    """Switching renderers re-authors the bus state (router resync), so the
    new renderer's first echo must dispatch even when it equals the last
    event dispatched for the previous renderer — or the slider keeps showing
    whatever the resync published from the stale cache."""
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    first = SimRenderer(registry, pool, renderer_id="renderer-a")
    second = SimRenderer(registry, pool, renderer_id="renderer-b")
    first.connect()
    second.connect()
    # Both happen to report the same level.
    first.volume = 30
    second.volume = 30

    recorder = VolumeRecorder(bus)
    device = await RendererVolumeDevice(registry, pool, bus).start()
    try:
        registry.select("renderer-a")
        session = await pool.open("renderer-a")
        await asyncio.sleep(SETTLE_S)
        assert recorder.events[-1].volume.current_volume == 30
        seen = len(recorder.events)

        await session.close()
        registry.select("renderer-b")
        second_session = await pool.open("renderer-b")
        await asyncio.sleep(SETTLE_S)

        assert len(recorder.events) > seen, "renderer-b's echo was swallowed"
        assert recorder.events[-1].volume.current_volume == 30
        await second_session.close()
    finally:
        await device.shutdown()


async def test_switch_adopts_a_provisionally_opened_renderers_snapshot(bus):
    """Renderer switching opens the target before selecting or announcing it.
    Its initial snapshot must replace that target's cached previous level when
    the switch commits."""
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    first = SimRenderer(registry, pool, renderer_id="renderer-a")
    second = SimRenderer(registry, pool, renderer_id="renderer-b")
    first.connect()
    second.connect()

    recorder = VolumeRecorder(bus)
    device = await RendererVolumeDevice(registry, pool, bus).start()
    try:
        # Establish renderer-b's stale cached level from its previous session.
        registry.select("renderer-b")
        previous = await pool.open("renderer-b")
        await previous.set_volume(75)
        await asyncio.sleep(SETTLE_S)
        await previous.close()

        registry.select("renderer-a")
        current = await pool.open("renderer-a")

        # This is the switch_renderer ordering: open and receive the snapshot
        # while renderer-a still owns the output, then select and announce b.
        second.volume = 30
        target = await pool.open("renderer-b", announce=False)
        registry.select("renderer-b")
        await current.close()
        await pool.announce(target)
        await asyncio.sleep(SETTLE_S)

        assert (await device.get_volume()).current_volume == 30
        assert recorder.events[-1].volume.current_volume == 30
        await target.close()
    finally:
        await device.shutdown()
