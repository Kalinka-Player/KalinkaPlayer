"""PUT /server/upgrade pressed while a renderer that is behind holds music.

The press is someone asking for the upgrade, so a paused queue no longer
refuses it: playback there stops, as clients are told, the renderer is asked,
and the server follows after the renderer confirms its target version. The
renderer beside the server is left to the installer the press fires. Driven
through the real session pool and play
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
from kalinka_server.renderer_sessions import RendererBusy, SessionPool
from kalinka_server.renderer_upgrade import RendererUpgradeService
from kalinka_server.upgrade_route import register_upgrade_routes

from tests.sim_renderer import DURATION_MS, SimRenderer
from tests.fake_track_sources import FakeTrackSources

SETTLE_S = 0.2
RELEASE = "0.5.0"


class UpgradableRenderer(SimRenderer):
    """A renderer behind the release that takes an upgrade as the binary
    does: never while it holds a playback session."""

    def __init__(self, core, renderer_id: str, name: str, *, local: bool = False):
        super().__init__(core.registry, core.pool, renderer_id)
        self.name = name
        self.local = local
        self.version = "0.4.0"
        self.service: RendererUpgradeService = core.service
        self.upgrades: list[str] = []
        self.refused: list[str] = []

    def connect(self, compatible: bool = True) -> None:
        self.registry.register(
            renderer_id=self.RENDERER_ID,
            instance_id=f"{self.RENDERER_ID}-instance",
            friendly_name=self.name,
            software_version=self.version,
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
async def core(monkeypatch, emitter):
    """The pool and upgrade service wired as create_app() wires them."""
    registry = RendererRegistry(offline_timeout_s=30.0)
    pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    monkeypatch.setattr(upgrade_route, "RECHECK_INTERVAL_S", 0.01)
    arbiter = PlaybackArbiter(emitter)

    async def stop_playback(renderer_id):
        await arbiter.vacate(renderer_id)
        session = pool.get(renderer_id)
        if session is not None:
            await session.close()

    service = RendererUpgradeService(
        registry,
        lambda renderer_id: pool.get(renderer_id) is not None,
        stop_playback,
    )
    pool.set_upgrade_guard(service.is_upgrading)
    app = FastAPI()
    register_upgrade_routes(app, service)
    yield SimpleNamespace(
        registry=registry, pool=pool, service=service, app=app, arbiter=arbiter
    )
    if app.state.upgrade_task is not None:
        app.state.upgrade_task.cancel()
        await asyncio.gather(app.state.upgrade_task, return_exceptions=True)
    await pool.shutdown()
    await registry.shutdown()


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
        arbiter=core.arbiter,
        sources=FakeTrackSources(default=lambda _: _track("1").source_retriever()),
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
    assert fired == []
    attic.version = RELEASE
    attic.connect()
    await asyncio.sleep(0.05)
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
    assert fired == []
    here.version = RELEASE
    here.connect()
    await asyncio.sleep(0.05)
    assert fired == [True]


async def test_a_renderer_that_refuses_holds_the_press_and_is_named(core, fired):
    attic = _renderer(core, "attic-id", "Attic")
    # Another Core's session: invisible to this one, refused by the renderer.
    attic.session_id = "another-cores-session"

    response = await _press(core.app)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Renderers upgrade first: Attic refused: a playback session is running"
    )
    assert fired == []


async def test_repeated_presses_wait_for_every_accepted_renderer(core, fired):
    attic = _renderer(core, "attic-id", "Attic")
    kitchen = _renderer(core, "kitchen-id", "Kitchen")
    assert (await _press(core.app)).status_code == 200
    core.registry.disconnect("attic-id", attic, clean=True)
    kitchen.version = RELEASE
    kitchen.connect()
    await asyncio.sleep(0.05)
    assert (await _press(core.app)).status_code == 200
    assert fired == []
    assert attic.upgrades == kitchen.upgrades == [RELEASE]
    attic.version = RELEASE
    attic.connect()
    await asyncio.sleep(0.05)
    assert fired == [True]
    assert (await _press(core.app)).status_code == 200
    assert fired == [True]


async def test_an_old_local_renderer_finishes_before_the_host_installer(
    core, checker, fired
):
    checker._installed_renderer = "0.4.0"
    # An older Hello cannot prove locality, including when it dials localhost.
    here = _renderer(core, "here-id", "Living room", local=False)
    assert (await _press(core.app)).status_code == 200
    core.registry.disconnect("here-id", here, clean=True)
    await asyncio.sleep(0.05)
    assert fired == []
    here.version = RELEASE
    here.connect()
    await asyncio.sleep(0.05)
    assert fired == [True]


async def test_playback_cannot_reclaim_an_upgrading_renderer(core, queue, fired):
    attic = _renderer(core, "attic-id", "Attic")
    await _pause_on(queue, attic)
    stopping = asyncio.Event()
    proceed = asyncio.Event()
    vacate = core.arbiter.vacate

    async def slow_vacate(renderer_id):
        await vacate(renderer_id)
        stopping.set()
        await proceed.wait()

    core.arbiter.vacate = slow_vacate
    press = asyncio.create_task(_press(core.app))
    await asyncio.wait_for(stopping.wait(), 1)
    try:
        with pytest.raises(RendererBusy, match="upgrading"):
            await core.pool.open("attic-id")
    finally:
        proceed.set()
    assert (await press).status_code == 200
    with pytest.raises(RendererBusy, match="upgrading"):
        await core.pool.open("attic-id")
    attic.version = RELEASE
    attic.connect()
    session = await core.pool.open("attic-id")
    await session.close()


async def test_a_supervisor_only_update_is_preserved(core, checker, fired, monkeypatch):
    monkeypatch.setattr(upgrade_route, "get_version", lambda: RELEASE)
    checker._installed_supervisor = "0.1.0"
    checker._latest_supervisor = "0.2.0"
    checker._supervisor_stale = True
    async with AsyncClient(
        transport=ASGITransport(app=core.app), base_url="http://kalinka"
    ) as client:
        info = (await client.get("/server/update")).json()
    assert info["supervisor_current_version"] == "0.1.0"
    assert info["supervisor_latest_version"] == "0.2.0"
    assert info["supervisor_update_available"] is True
    assert (await _press(core.app)).status_code == 200
    assert fired == [True]


async def test_the_demo_does_not_offer_or_start_an_upgrade(core, fired):
    app = FastAPI()
    register_upgrade_routes(app, core.service, demo_mode=lambda: True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://kalinka"
    ) as client:
        info = (await client.get("/server/update")).json()
    assert info["upgrade_supported"] is False
    assert (await _press(app)).status_code == 403
    assert fired == []


async def test_a_speaker_test_session_is_also_closed(core, queue, fired):
    attic = _renderer(core, "attic-id", "Attic")
    session = await core.pool.open("attic-id")
    assert (await _press(core.app)).status_code == 200
    assert session.close_reason is not None
    assert attic.session_id is None
    assert attic.upgrades == [RELEASE]


async def test_a_renderer_that_never_returns_does_not_start_the_installer(
    core, fired, monkeypatch, caplog
):
    monkeypatch.setattr(upgrade_route, "INSTALL_TIMEOUT_S", 0.03)
    _renderer(core, "attic-id", "Attic")
    assert (await _press(core.app)).status_code == 200
    await asyncio.wait_for(core.app.state.upgrade_task, 1)
    assert fired == []
    assert "Server upgrade could not finish" in caplog.text


async def test_a_failed_host_install_can_be_retried_after_the_deadline(
    core, fired, monkeypatch
):
    monkeypatch.setattr(upgrade_route, "INSTALL_TIMEOUT_S", 0)
    assert (await _press(core.app)).status_code == 200
    assert (await _press(core.app)).status_code == 200
    assert fired == [True, True]
