"""The queue's autoplay routes and setting, through the production app."""

import pytest

from kalinka_server import server
from kalinka_server.config_model import KalinkaConfig
from tests.app_harness import isolate_app, running

AUTOPLAY = "base_config.queue.autoplay"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    return tmp_path


async def _app(tmp_path):
    return await server.create_app(str(tmp_path / "overrides.json"), KalinkaConfig(), {})


async def test_the_server_says_its_queue_carries_on_and_takes_turn_downs(isolated):
    async with running(await _app(isolated)) as client:
        queue = (await client.get("/server/version")).json()["queue"]

    assert queue == {"autoplay": True, "similar": True, "dislike": True}


async def test_turning_autoplay_off_applies_at_once(isolated):
    async with running(await _app(isolated)) as client:
        schema_version = (await client.get("/server/config")).json()["schema_version"]
        saved = await client.put(
            "/server/config",
            json={"schema_version": schema_version, "changes": {AUTOPLAY: False}},
        )
        queue = (await client.get("/server/version")).json()["queue"]

    assert saved.status_code == 200
    assert queue["autoplay"] is False


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
