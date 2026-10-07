"""The bundled receiver through the real direct-playback service and renderer wire."""

import asyncio

import pytest

pytest.importorskip("kalinka_plugin_upnp")

from kalinka_plugin_sdk.direct_playback import (
    OutputCapabilities,
    TransportKind,
    TransportRequest,
)
from kalinka_plugin_upnp.media import Media, UpnpError
from kalinka_plugin_upnp.playback import Playback
from kalinka_plugin_upnp.services import Services
from kalinka_server import renderer_player
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.direct_playback import DirectPlaybackService
from kalinka_server.renderer_output_device import RendererVolumeDevice

from tests.test_direct_playback import _enqueued, _play_queue

pytest_plugins = ["tests.test_direct_playback"]


@pytest.fixture
async def upnp(renderer, capabilities, arbiter, router, device_bus, queue):
    device = await RendererVolumeDevice(
        renderer.registry, renderer.pool, device_bus
    ).start()
    router.device = device
    direct = DirectPlaybackService(
        "upnp",
        config=KalinkaConfig(),
        registry=renderer.registry,
        capabilities=capabilities,
        pool=renderer.pool,
        arbiter=arbiter,
        device_router=lambda: router,
        device_events=device_bus,
    )
    playback = Playback(direct, lambda service: None)
    playback.start()
    yield playback
    await playback.close()
    await device.shutdown()


async def settled(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_upnp_controls_the_renderer_and_yields_to_queue(
    upnp, queue, renderer, arbiter
):
    await _play_queue(queue, renderer, "queued")
    media = Media.parse("http://media.test/song.flac", "")
    await upnp.command("set_uri", media)
    await upnp.command("play")
    await settled(lambda: upnp.state == "PLAYING")
    assert arbiter.control.plugin_id == "upnp"
    assert _enqueued(renderer)[-1] == media.uri

    renderer.report_position(123000)
    await settled(lambda: upnp.position_ms == 123000)
    assert Services(upnp).snapshot("AVTransport")["RelativeTimePosition"] == "0:02:03"
    assert Services(upnp).snapshot("AVTransport")["CurrentTrackDuration"] == "0:05:00"

    assert arbiter.forward(TransportRequest(TransportKind.PAUSE))
    await settled(lambda: upnp.state == "PAUSED_PLAYBACK")
    assert arbiter.forward(TransportRequest(TransportKind.SEEK, 45000))
    await settled(lambda: renderer.position_ms == 45000)
    assert arbiter.forward(TransportRequest(TransportKind.RESUME))
    await settled(lambda: upnp.state == "PLAYING")

    await upnp.command("set_volume", 25)
    await settled(lambda: renderer.volume == 25)
    await settled(lambda: upnp.volume == 25)
    listener = upnp.listener
    await queue.play()
    await settled(lambda: not upnp.active and upnp.state == "STOPPED")
    listener.on_finished()
    listener.on_command(TransportRequest(TransportKind.RESUME))
    await asyncio.sleep(0.05)
    assert not arbiter.control.is_exclusive
    assert _enqueued(renderer)[-1] == "http://example/queued.flac"


async def test_dsd_waits_for_the_renderer_to_take_it(upnp, renderer, arbiter):
    services = Services(upnp)
    stop = upnp.direct.watch_output_capabilities(services)
    dsf = {
        "InstanceID": "0",
        "CurrentURI": "http://media.test/song.dsf",
        "CurrentURIMetaData": "",
    }

    async def sink():
        protocols = await services.dispatch("ConnectionManager", "GetProtocolInfo", {})
        return protocols["Sink"]

    assert "audio/x-dsf" not in await sink()
    with pytest.raises(UpnpError) as refused:
        await services.dispatch("AVTransport", "SetAVTransportURI", dsf)
    assert refused.value.code == 714
    assert upnp.current is None

    renderer.announce_capabilities(OutputCapabilities(dsd=True))
    await settled(lambda: services.dsd)
    assert "http-get:*:audio/x-dsf:*" in await sink()
    await services.dispatch("AVTransport", "SetAVTransportURI", dsf)
    assert upnp.current.source.format == "audio/x-dsf"
    assert not arbiter.control.is_exclusive
    stop()


async def test_upnp_next_track_is_queued_on_renderer_and_idle_releases(
    upnp, renderer, arbiter, monkeypatch
):
    await upnp.command("set_uri", Media.parse("http://media.test/one.mp3", ""))
    await upnp.command("set_next", Media.parse("http://media.test/two.flac", ""))
    await upnp.command("play")
    await settled(lambda: upnp.state == "PLAYING" and len(renderer.queued) == 1)
    assert renderer.position_ms == 0
    session = renderer.session_id
    await upnp.command("set_volume", 25)
    await settled(lambda: renderer.volume == 25)
    renderer.finish_current()
    await settled(
        lambda: upnp.current.uri.endswith("two.flac") and upnp.state == "PLAYING"
    )
    assert len(_enqueued(renderer)) == 2
    assert _enqueued(renderer)[-1] == "http://media.test/two.flac"
    assert renderer.session_id == session
    assert renderer.volume == 25
    monkeypatch.setattr(renderer_player, "IDLE_RELEASE_TIMEOUT_S", 0.15)
    renderer.finish_current()
    await settled(lambda: upnp.state == "STOPPED")
    assert upnp.active and renderer.session_id == session
    await settled(lambda: not arbiter.control.is_exclusive)
    await settled(lambda: not upnp.active)
    assert not upnp.active
    assert renderer.session_id is None


@pytest.mark.parametrize("transition", ["stop", "finished"])
async def test_controller_track_changes_reuse_session_and_volume(
    upnp, renderer, transition
):
    await upnp.command("set_uri", Media.parse("http://media.test/one.flac", ""))
    await upnp.command("play")
    await settled(lambda: upnp.state == "PLAYING")
    session = renderer.session_id
    await upnp.command("set_volume", 25)
    await settled(lambda: renderer.volume == 25)
    if transition == "stop":
        await upnp.command("stop")
    else:
        renderer.finish_current()
    await settled(lambda: upnp.state == "STOPPED")
    await upnp.command("set_uri", Media.parse("http://media.test/two.flac", ""))
    await upnp.command("play")
    await settled(lambda: upnp.state == "PLAYING" and len(_enqueued(renderer)) == 2)
    assert renderer.session_id == session
    assert renderer.volume == 25
    assert upnp.volume == 25


async def test_kalinka_previous_and_next_follow_controller_history(
    upnp, renderer, queue
):
    for title in ("one", "two", "three"):
        await upnp.command(
            "set_uri", Media.parse(f"http://media.test/{title}.flac", "")
        )
        await upnp.command("play")
        await settled(lambda: upnp.state == "PLAYING")
    session = renderer.session_id
    for title in ("two", "one"):
        await queue.prev()
        await settled(
            lambda title=title: (
                upnp.current.uri.endswith(f"{title}.flac") and upnp.state == "PLAYING"
            )
        )
    for title in ("two", "three"):
        await queue.next()
        await settled(
            lambda title=title: (
                upnp.current.uri.endswith(f"{title}.flac") and upnp.state == "PLAYING"
            )
        )
    await upnp.command("set_next", Media.parse("http://media.test/four.flac", ""))
    await queue.next()
    await settled(
        lambda: upnp.current.uri.endswith("four.flac") and upnp.state == "PLAYING"
    )
    assert renderer.session_id == session
    await queue.next()
    await asyncio.sleep(0.05)
    assert upnp.current.uri.endswith("four.flac")
    assert upnp.state == "PLAYING" and upnp.status == "OK"


@pytest.mark.parametrize("delivered", [False, True])
@pytest.mark.parametrize("jump", ["previous", "next", "uri"])
async def test_track_jump_cancels_old_next_and_queues_correct_successor(
    upnp, renderer, queue, delivered, jump
):
    for name in ("previous", "current"):
        await upnp.command("set_uri", Media.parse(f"http://media.test/{name}.flac", ""))
        await upnp.command("play")
        await settled(lambda: upnp.state == "PLAYING")
    await upnp.command("set_next", Media.parse("http://media.test/next.flac", ""))
    if delivered:
        await settled(lambda: len(renderer.queued) == 1)
    expected = jump if jump != "uri" else "replacement"
    if jump == "previous":
        await queue.prev()
    elif jump == "next":
        await queue.next()
    else:
        await upnp.command(
            "set_uri", Media.parse("http://media.test/replacement.flac", "")
        )
    await settled(
        lambda: (
            upnp.current.uri.endswith(f"{expected}.flac") and upnp.state == "PLAYING"
        )
    )
    assert upnp.current.uri.endswith(f"{expected}.flac")
    if jump == "previous":
        await settled(lambda: len(renderer.queued) == 1)
        assert _enqueued(renderer)[-1] == "http://media.test/current.flac"
        renderer.finish_current()
        await settled(
            lambda: (
                upnp.current.uri.endswith("current.flac") and upnp.state == "PLAYING"
            )
        )
        await settled(lambda: len(renderer.queued) == 1)
        assert _enqueued(renderer)[-1] == "http://media.test/next.flac"
    else:
        assert renderer.queued == []
