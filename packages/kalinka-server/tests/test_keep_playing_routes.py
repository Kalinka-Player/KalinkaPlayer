"""The queue's Keep playing routes and setting, through the production app."""

import pytest

from kalinka_server import server
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.keep_playing import SETTING
from tests.app_harness import isolate_app, running


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    return tmp_path


async def _app(tmp_path):
    return await server.create_app(str(tmp_path / "overrides.json"), KalinkaConfig(), {})


def _queue_state(app):
    return app.state.player_context.playqueue_eventbus.get_snapshot()


async def test_the_server_says_its_queue_carries_on_and_takes_turn_downs(isolated):
    app = await _app(isolated)
    async with running(app) as client:
        queue = (await client.get("/server/version")).json()["queue"]
        announced = _queue_state(app).keep_playing

    assert queue == {"keep_playing": True, "similar": True, "dislike": True}
    assert announced is True


async def test_saving_the_setting_applies_at_once_and_tells_queue_clients(isolated):
    app = await _app(isolated)
    async with running(app) as client:
        schema_version = (await client.get("/server/config")).json()["schema_version"]
        saved = await client.put(
            "/server/config",
            json={"schema_version": schema_version, "changes": {SETTING: False}},
        )
        queue = (await client.get("/server/version")).json()["queue"]
        announced = _queue_state(app).keep_playing

    assert saved.status_code == 200
    assert queue["keep_playing"] is False
    assert announced is False


async def test_the_queue_route_saves_the_setting_and_tells_queue_clients(isolated):
    app = await _app(isolated)
    async with running(app) as client:
        off = await client.put("/queue/keep_playing", json={"enabled": False})
        announced_off = _queue_state(app).keep_playing
        saved_off = (await client.get("/server/config")).json()
        on = await client.put("/queue/keep_playing", json={"enabled": True})
        announced_on = _queue_state(app).keep_playing

    assert off.json() == {"enabled": False, "count": 0}
    assert announced_off is False
    assert _saved_value(saved_off, SETTING) is False
    # An empty queue has nothing to carry on from.
    assert on.json() == {"enabled": True, "count": 0}
    assert announced_on is True


async def test_the_queue_route_wants_a_switch(isolated):
    async with running(await _app(isolated)) as client:
        response = await client.put("/queue/keep_playing", json={})

    assert response.status_code == 422


async def test_turning_down_with_nothing_playing_is_refused(isolated):
    async with running(await _app(isolated)) as client:
        response = await client.post("/queue/dislike")

    assert response.status_code == 409


async def test_more_like_this_on_an_empty_queue_adds_nothing(isolated):
    async with running(await _app(isolated)) as client:
        response = await client.post("/queue/similar")
        too_many = await client.post("/queue/similar", params={"count": 0})

    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert too_many.status_code == 422


def _saved_value(config_answer, path):
    return config_answer["values"][path]
