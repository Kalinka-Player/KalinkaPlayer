"""What the renderer playback would go to can play, as watchers are told it."""

import pytest
from kalinka_plugin_sdk.direct_playback import OutputCapabilities
from kalinka_server.output_capabilities import OutputCapabilityTracker
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool

from tests.sim_renderer import SimRenderer

DSD = OutputCapabilities(dsd=True)
NO_DSD = OutputCapabilities(dsd=False)


class Watcher(list):
    """The ``dsd`` of everything it was told, in order."""

    def __call__(self, capabilities):
        self.append(capabilities.dsd)


@pytest.fixture
def registry():
    return RendererRegistry(offline_timeout_s=30.0)


@pytest.fixture
def tracker(registry):
    return OutputCapabilityTracker(registry)


def _renderer(registry, renderer_id=None, capabilities=NO_DSD):
    sim = SimRenderer(registry, SessionPool(registry, "test-server-id"), renderer_id)
    sim.capabilities = capabilities
    return sim


async def test_a_watcher_is_told_what_the_renderer_said_in_its_hello(
    registry, tracker
):
    _renderer(registry, capabilities=DSD).connect()
    watcher = Watcher()

    tracker.watch(watcher)

    assert watcher == [True]


@pytest.mark.parametrize("capabilities", [None, NO_DSD])
async def test_dsd_is_not_known_or_not_taken(registry, tracker, capabilities):
    _renderer(registry, capabilities=capabilities).connect()
    watcher = Watcher()

    tracker.watch(watcher)

    assert watcher == [None if capabilities is None else False]


async def test_without_a_renderer_nothing_is_known(tracker):
    watcher = Watcher()
    tracker.watch(watcher)
    assert watcher == [None]


async def test_a_watcher_hears_each_change_once(registry, tracker):
    renderer = _renderer(registry)
    renderer.connect()
    watcher = Watcher()
    tracker.watch(watcher)

    renderer.announce_capabilities(DSD)
    renderer.announce_capabilities(DSD)
    renderer.announce_capabilities(NO_DSD)

    assert watcher == [False, True, False]


async def test_a_renderer_that_connects_is_told_of(registry, tracker):
    watcher = Watcher()
    tracker.watch(watcher)

    _renderer(registry, capabilities=DSD).connect()

    assert watcher == [None, True]


async def test_a_renderer_that_goes_away_is_no_longer_known(registry, tracker):
    renderer = _renderer(registry, capabilities=DSD)
    renderer.connect()
    watcher = Watcher()
    tracker.watch(watcher)

    registry.disconnect(renderer.RENDERER_ID, renderer, clean=False)

    assert watcher == [True, None]
    await registry.shutdown()


async def test_a_renderer_dropped_mid_playback_is_not_known_until_it_returns(
    registry, tracker
):
    renderer = _renderer(registry, capabilities=DSD)
    renderer.connect()
    registry.session_claimed(renderer.RENDERER_ID)
    watcher = Watcher()
    tracker.watch(watcher)

    registry.disconnect(renderer.RENDERER_ID, renderer, clean=False)
    renderer.connect()

    assert watcher == [True, None, True]
    await registry.shutdown()


async def test_a_restarted_renderer_is_believed_as_its_new_hello_says(
    registry, tracker
):
    _renderer(registry, capabilities=DSD).connect()
    watcher = Watcher()
    tracker.watch(watcher)

    _renderer(registry, capabilities=None).connect()

    assert watcher == [True, None]


async def test_a_replaced_link_is_not_believed(registry, tracker):
    replaced = _renderer(registry)
    replaced.connect()
    _renderer(registry).connect()
    watcher = Watcher()
    tracker.watch(watcher)

    replaced.announce_capabilities(DSD)

    assert watcher == [False]


async def test_playback_moving_to_another_renderer_tells_what_that_one_plays(
    registry, tracker
):
    dsd = _renderer(registry, "dsd-renderer", capabilities=DSD)
    dsd.connect()
    pcm = _renderer(registry, "pcm-renderer")
    pcm.connect()
    watcher = Watcher()
    tracker.watch(watcher)

    registry.select(pcm.RENDERER_ID)
    pcm.announce_capabilities(DSD)
    dsd.announce_capabilities(NO_DSD)

    assert watcher == [True, False, True]


async def test_a_watcher_that_stopped_is_told_nothing_more(registry, tracker):
    renderer = _renderer(registry)
    renderer.connect()
    watcher = Watcher()
    stop = tracker.watch(watcher)

    stop()
    stop()
    renderer.announce_capabilities(DSD)

    assert watcher == [False]
