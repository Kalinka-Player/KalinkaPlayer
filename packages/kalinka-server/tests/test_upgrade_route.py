"""PUT /server/upgrade pressed while a renderer that is behind holds music.

The press is someone asking for the upgrade, so a paused queue no longer
refuses it: playback there stops, as clients are told, the renderer is asked,
and the server goes ahead. The renderer beside the server is left to the
installer the press fires. Driven through the real session pool and play
queue, against simulated renderers that refuse an upgrade while they hold a
session, as the binary does.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from kalinka_plugin_sdk import EventEmitter
from kalinka_plugin_sdk.datamodel import Album, EntityId, EntityType, PlayerStateEnum
from kalinka_plugin_sdk.events import PlaybackStateChangedEvent
from kalinka_plugin_sdk.inputmodule import DirectUrl, Track, TrackInfo, TrackSource

from kalinka_server import update_check, upgrade_route
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.playback_arbiter import PlaybackArbiter
from kalinka_server.playqueue import PlayQueueImpl
from kalinka_server.renderer_proto import renderer_pb2 as pb
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import CloseReason, SessionPool
from kalinka_server.renderer_upgrade import RendererUpgradeService
from kalinka_server.upgrade_route import register_upgrade_routes

from tests.sim_renderer import DURATION_MS, SimRenderer

SETTLE_S = 0.2
RELEASE = "0.5.0"


class UpgradableRenderer(SimRenderer):
    """A renderer behind the release that takes an upgrade as the binary
    does: never while it holds a playback session."""

    def __init__(self, core, renderer_id: str, name: str, *, local: bool = False):
        super().__init__(core.registry, core.pool, renderer_id)
        self.name = name
        self.local = local
        self.service: RendererUpgradeService = core.service
        self.upgrades: list[str] = []
        self.refused: list[str] = []

    def connect(self, compatible: bool = True) -> None:
        self.registry.register(
            renderer_id=self.RENDERER_ID,
            instance_id=f"{self.RENDERER_ID}-instance",
            friendly_name=self.name,
            software_version="0.4.0",
            kind="native",
            platform={},
            session=self,
            compatible=compatible,
            server_addr=self.server_addr,
            upgrade_supported=True,
            local=self.local,
        )

    async def send_upgrade(self, message_id: int, target_version: str) -> None:
        result = pb.UpgradeResult()
        if self.session_id is not None:
            result.detail = "a playback session is running"
            self.refused.append(target_version)
        else:
            result.accepted = True
            result.detail = "upgrading"
            self.upgrades.append(target_version)
        self.service.handle_reply(self.RENDERER_ID, self, message_id, result)


def _track(track_id: str) -> TrackInfo:
    entity = EntityId(id=track_id, type=EntityType.TRACK, source="test_source")

    async def source_retriever() -> TrackSource:
        return TrackSource(
            source=DirectUrl(url=f"http://example/{track_id}.flac"), format="FLAC"
        )

    return TrackInfo(
        id=entity,
        metadata=Track(
            id=entity,
            title=f"track{track_id}",
            duration=300,
            album=Album(id=entity, title="album"),
        ),
        source_retriever=source_retriever,
    )


@pytest.fixture
def checker(monkeypatch):
    """A published release, on a machine with no renderer package installed
    until a test installs one."""
    checker = update_check.UpdateChecker()
    checker._latest = RELEASE
    checker._latest_renderer = RELEASE
    monkeypatch.setattr(update_check, "checker", checker)
    return checker


@pytest.fixture
def fired(checker, monkeypatch):
    """An install that can upgrade to that release; collects the installer
    runs the press fires."""
    monkeypatch.setattr(update_check, "upgrade_supported", lambda: True)
    monkeypatch.setattr(upgrade_route, "get_version", lambda: "0.4.0")
    runs: list[bool] = []
    monkeypatch.setattr(update_check, "request_upgrade", lambda: runs.append(True))
    return runs


@pytest.fixture
def core():
    """The pool and upgrade service wired as create_app() wires them."""
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    service = RendererUpgradeService(
        registry,
        lambda renderer_id: pool.get(renderer_id) is not None,
        lambda renderer_id: pool.interrupt(renderer_id, CloseReason.UPGRADING),
    )
    app = FastAPI()
    register_upgrade_routes(app, service)
    return SimpleNamespace(registry=registry, pool=pool, service=service, app=app)


@pytest.fixture
def emitter():
    return Mock(spec=EventEmitter)


@pytest.fixture
async def queue(core, emitter):
    playqueue = PlayQueueImpl(
        KalinkaConfig(),
        emitter,
        core.registry,
        core.pool,
        arbiter=PlaybackArbiter(emitter),
    )
    await playqueue.__aenter__()
    yield playqueue
    await playqueue.__aexit__(None, None, None)


def _renderer(core, renderer_id: str, name: str, **kwargs) -> UpgradableRenderer:
    renderer = UpgradableRenderer(core, renderer_id, name, **kwargs)
    renderer.connect()
    return renderer


async def _pause_on(queue, renderer) -> None:
    await queue.add([_track("1"), _track("2")])
    await queue.play()
    await asyncio.sleep(SETTLE_S)
    await queue.pause(True)
    await asyncio.sleep(SETTLE_S)
    assert (await queue.get_playback_state()).state == PlayerStateEnum.PAUSED
    assert renderer.session_id is not None, "a paused queue holds its session"


async def _press(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://kalinka"
    ) as client:
        return await client.put("/server/upgrade", json={"version": RELEASE})


def _published(emitter) -> list:
    return [
        call.args[0].state
        for call in emitter.dispatch.call_args_list
        if isinstance(call.args[0], PlaybackStateChangedEvent)
    ]


async def test_a_paused_renderer_is_stopped_and_upgraded_not_refused(
    core, queue, emitter, fired
):
    attic = _renderer(core, "attic-id", "Attic")
    await _pause_on(queue, attic)
    emitter.reset_mock()

    response = await _press(core.app)
    await asyncio.sleep(SETTLE_S)

    assert response.status_code == 200
    assert response.json() == {"message": "upgrading"}
    assert attic.upgrades == [RELEASE]
    assert attic.refused == []
    assert core.pool.get("attic-id") is None
    assert fired == [True]
    # Clients see the music stop rather than a pause that no longer holds.
    assert (await queue.get_playback_state()).state == PlayerStateEnum.STOPPED
    assert PlayerStateEnum.STOPPED in [state.state for state in _published(emitter)]


async def test_the_stop_is_not_reported_as_a_fault(core, queue, emitter, fired):
    """Someone asked for it, so clients are told of no error."""
    attic = _renderer(core, "attic-id", "Attic")
    await _pause_on(queue, attic)
    emitter.reset_mock()

    await _press(core.app)
    await asyncio.sleep(SETTLE_S)

    assert (await queue.get_playback_state()).message is None
    assert [state.message for state in _published(emitter)] == [None]


async def test_a_press_in_a_tracks_last_seconds_does_not_start_the_next(
    core, queue, fired
):
    """The next track is already queued on the renderer by then; the stop
    must not read as the current one running out, or the queue claims the
    renderer again just as it restarts into its upgrade."""
    attic = _renderer(core, "attic-id", "Attic")
    await queue.add([_track("1"), _track("2")])
    await queue.play()
    await asyncio.sleep(SETTLE_S)
    attic.report_position(DURATION_MS - 2000)
    await asyncio.sleep(SETTLE_S)
    assert attic.queued, "the next track is prefetched in the last seconds"

    response = await _press(core.app)
    await asyncio.sleep(SETTLE_S)

    assert response.status_code == 200
    assert attic.upgrades == [RELEASE]
    assert attic.session_id is None
    assert core.pool.get("attic-id") is None
    assert (await queue.get_playback_state()).state == PlayerStateEnum.STOPPED


async def test_only_this_machines_renderer_behind_is_left_to_the_installer(
    core, queue, checker, fired
):
    checker._installed_renderer = "0.4.0"
    here = _renderer(core, "here-id", "Living room", local=True)
    await _pause_on(queue, here)

    response = await _press(core.app)

    assert response.status_code == 200
    assert here.upgrades == [] and here.refused == []
    assert here.session_id is not None, "its music plays on until the installer"
    assert fired == [True]


async def test_this_machines_renderer_from_elsewhere_is_asked_like_the_rest(
    core, queue, fired
):
    """With no renderer package here, the installer run does not touch it."""
    here = _renderer(core, "here-id", "Living room", local=True)
    await _pause_on(queue, here)

    response = await _press(core.app)

    assert response.status_code == 200
    assert here.upgrades == [RELEASE]
    assert here.session_id is None
    assert fired == [True]


async def test_a_renderer_that_refuses_holds_the_press_and_is_named(
    core, fired
):
    attic = _renderer(core, "attic-id", "Attic")
    # Another Core's session: invisible to this one, refused by the renderer.
    attic.session_id = "another-cores-session"

    response = await _press(core.app)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Renderers upgrade first: Attic refused: a playback session is running"
    )
    assert fired == []
