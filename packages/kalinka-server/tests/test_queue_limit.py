"""An ordinary server's queue holds a thousand tracks: an add past that is
refused whole, so what plays is never quietly less than what was asked for."""

from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from kalinka_server import server
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.queue_limit import QUEUE, QUEUE_FULL, QUEUE_LIMIT, TOO_MANY_TO_PLAY
from tests.app_harness import isolate_app, running


@dataclass
class _Page:
    total: int


class _Queue:
    def __init__(self, total: int):
        self.total = total

    async def list(self, offset: int, limit: int) -> _Page:
        return _Page(self.total)


async def test_an_add_that_fills_the_queue_exactly_passes():
    await QUEUE.refuse_past(_Queue(QUEUE_LIMIT - 10), 10)


async def test_an_add_one_past_the_limit_is_refused():
    with pytest.raises(HTTPException) as refused:
        await QUEUE.refuse_past(_Queue(QUEUE_LIMIT - 10), 11)
    assert refused.value.status_code == 409
    assert refused.value.detail == QUEUE_FULL


def test_a_replacement_that_fills_the_queue_exactly_passes():
    QUEUE.refuse_replacement(QUEUE_LIMIT)


def test_a_replacement_one_past_the_limit_is_refused():
    with pytest.raises(HTTPException) as refused:
        QUEUE.refuse_replacement(QUEUE_LIMIT + 1)
    assert refused.value.status_code == 409
    assert refused.value.detail == TOO_MANY_TO_PLAY


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    return tmp_path


async def test_a_server_refuses_more_ids_than_its_queue_holds(isolated):
    app = await server.create_app(str(isolated / "overrides.json"), KalinkaConfig(), {})
    async with running(app) as client:
        ids = [f"kalinka:jamendo:track:{n}" for n in range(QUEUE_LIMIT + 1)]
        refused = await client.post("/queue/add", json=ids)
        assert refused.status_code == 409
        assert refused.json() == {"detail": QUEUE_FULL}


async def test_a_server_refuses_one_id_that_expands_past_its_queue(
    isolated, monkeypatch
):
    async def a_big_folder(ids, *_):
        return [object()] * (QUEUE_LIMIT + 1)

    monkeypatch.setattr(server, "tracks_for", a_big_folder)
    app = await server.create_app(str(isolated / "overrides.json"), KalinkaConfig(), {})
    async with running(app) as client:
        refused = await client.post("/queue/add", json=["kalinka:localfiles:folder:1"])
        assert refused.status_code == 409
        assert refused.json() == {"detail": QUEUE_FULL}
