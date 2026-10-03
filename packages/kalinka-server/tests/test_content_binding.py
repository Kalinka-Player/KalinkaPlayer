"""Binding a module's asset to a URL, per renderer.

Which address a content link carries cannot be decided when the module hands
the source over: the same resolved source may be played on one renderer and
then re-played on another, reachable on a different interface. So it is decided
here, at the moment a source is handed to a particular renderer, against the
address that renderer itself dialed.
"""

import asyncio
from unittest.mock import Mock

import pytest

from kalinka_plugin_sdk import EventEmitter, PlaybackStateChangedEvent
from kalinka_plugin_sdk.datamodel import (
    Album,
    EntityId,
    EntityType,
    PlayerStateEnum,
    Track,
)
from kalinka_plugin_sdk.inputmodule import (
    DirectUrl,
    ModuleAsset,
    TrackSource,
)
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.playqueue import PlayQueueImpl
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool

from tests.fake_track_sources import FakeTrackSources
from tests.sim_renderer import SimRenderer


async def _asset(track_id: EntityId) -> TrackSource:
    return TrackSource(
        source=ModuleAsset(module="localfiles", asset_id=track_id.id), format="FLAC"
    )


def _asset_track(track_id: str = "1") -> Track:
    entity = EntityId(id=track_id, type=EntityType.TRACK, source="localfiles")
    return Track(
        id=entity,
        title=f"track{track_id}",
        duration=10,
        album=Album(id=entity, title="album"),
    )


def _cdn_track(sources: FakeTrackSources) -> Track:
    track = _asset_track()

    async def cdn() -> TrackSource:
        return TrackSource(source=DirectUrl(url="https://cdn.test/1.mp3"), format="MP3")

    sources.serve(track.id, cdn)
    return track


@pytest.fixture
def renderers():
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    first = SimRenderer(registry, pool, renderer_id="rid-a")
    first.server_addr = ("10.0.0.1", 8000)
    second = SimRenderer(registry, pool, renderer_id="rid-b")
    second.server_addr = ("192.168.5.4", 8000)
    first.connect()
    second.connect()
    return registry, pool, first, second


@pytest.fixture
def emitter():
    return Mock(spec=EventEmitter)


@pytest.fixture
def sources():
    return FakeTrackSources(default=_asset)


@pytest.fixture
async def queue(renderers, emitter, sources):
    registry, pool, _, _ = renderers
    playqueue = PlayQueueImpl(KalinkaConfig(), emitter, registry, pool, sources=sources)
    await playqueue.__aenter__()
    yield playqueue
    await playqueue.__aexit__(None, None, None)


def _sources(renderer):
    return [
        c.enqueue_source.source
        for c in renderer.commands
        if c.WhichOneof("op") == "enqueue_source"
    ]


async def test_asset_is_addressed_on_the_interface_the_renderer_dialed(
    renderers, queue
):
    _registry, _pool, first, _second = renderers
    await queue.add([_asset_track()])
    await queue.play()
    await asyncio.sleep(0.2)

    assert _sources(first)[0].uri == "http://10.0.0.1:8000/content/localfiles/1"


async def test_the_same_asset_follows_the_renderer_it_moves_to(renderers, queue):
    """The resolved source is reused across renderers, so the host must not be
    baked into it — a link minted for rid-a is unreachable from rid-b."""
    _registry, _pool, first, second = renderers
    await queue.add([_asset_track()])
    await queue.play()
    await asyncio.sleep(0.2)
    await queue.switch_renderer("rid-b")
    await asyncio.sleep(0.2)

    assert _sources(first)[0].uri.startswith("http://10.0.0.1:8000/")
    assert _sources(second)[0].uri == "http://192.168.5.4:8000/content/localfiles/1"


async def test_a_direct_url_reaches_the_renderer_untouched(renderers, queue, sources):
    _registry, _pool, first, _second = renderers
    await queue.add([_cdn_track(sources)])
    await queue.play()
    await asyncio.sleep(0.2)

    assert _sources(first)[0].uri == "https://cdn.test/1.mp3"


async def test_the_renderer_is_told_the_format(renderers, queue):
    _registry, _pool, first, _second = renderers
    await queue.add([_asset_track()])
    await queue.play()
    await asyncio.sleep(0.2)

    assert _sources(first)[0].mime_type == "FLAC"


async def test_a_renderer_with_no_known_address_cannot_be_given_content(
    renderers, queue, emitter
):
    """Rather than mint a link on a guessed interface, refuse — the queue
    surfaces it as a playback error."""
    _registry, _pool, first, _second = renderers
    first.server_addr = None
    first.connect()

    await queue.add([_asset_track()])
    await queue.play()
    await asyncio.sleep(0.2)

    assert _sources(first) == []
    assert PlayerStateEnum.ERROR in [
        call.args[0].state.state
        for call in emitter.dispatch.call_args_list
        if isinstance(call.args[0], PlaybackStateChangedEvent)
    ]


async def test_the_state_reports_the_url_the_renderer_is_fetching(renderers, queue):
    """Which link a track resolved to is not knowable from the queue alone —
    it is minted per renderer — so the state carries what was handed over."""
    _registry, _pool, _first, _second = renderers
    await queue.add([_asset_track()])
    await queue.play()
    await asyncio.sleep(0.2)

    state = await queue.get_playback_state()
    assert state.stream_url == "http://10.0.0.1:8000/content/localfiles/1"


async def test_the_reported_url_follows_a_renderer_switch(renderers, queue):
    _registry, _pool, _first, _second = renderers
    await queue.add([_asset_track()])
    await queue.play()
    await asyncio.sleep(0.2)
    await queue.switch_renderer("rid-b")
    await asyncio.sleep(0.2)

    state = await queue.get_playback_state()
    assert state.stream_url == "http://192.168.5.4:8000/content/localfiles/1"


async def test_a_direct_url_is_reported_as_the_module_gave_it(
    renderers, queue, sources
):
    _registry, _pool, _first, _second = renderers
    await queue.add([_cdn_track(sources)])
    await queue.play()
    await asyncio.sleep(0.2)

    state = await queue.get_playback_state()
    assert state.stream_url == "https://cdn.test/1.mp3"


async def test_nothing_playing_reports_no_url(renderers, queue):
    _registry, _pool, _first, _second = renderers
    await queue.add([_asset_track()])
    await queue.play()
    await asyncio.sleep(0.2)
    await queue.stop()
    await asyncio.sleep(0.2)

    state = await queue.get_playback_state()
    assert state.stream_url is None
