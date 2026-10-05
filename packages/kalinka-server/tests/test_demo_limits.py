"""What a demo server lets through is bounded: each visitor's changes are
rate-limited, and the shared queue has a fixed size."""

from dataclasses import dataclass

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from kalinka_server import demo_mode, server
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.demo_mode import (
    QUEUE_FULL,
    QUEUE_LIMIT,
    THROTTLED,
    ClientRate,
    DemoWriteThrottle,
    refuse_beyond_queue_limit,
)
from tests.app_harness import isolate_app, running


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_client_spends_its_burst_then_waits_for_each_token():
    clock = Clock()
    rate = ClientRate(burst=3, interval_s=2.0, now=clock)
    assert [rate.take("a") for _ in range(3)] == [0, 0, 0]
    assert rate.take("a") == pytest.approx(2.0)

    clock.now = 1.5
    assert rate.take("a") == pytest.approx(0.5)
    clock.now = 2.0
    assert rate.take("a") == 0
    assert rate.take("a") == pytest.approx(2.0)


def test_clients_are_limited_apart():
    rate = ClientRate(burst=1, interval_s=60.0, now=Clock())
    assert rate.take("a") == 0
    assert rate.take("a") > 0
    assert rate.take("b") == 0


def test_a_full_bucket_is_forgotten_once_the_table_is_long(monkeypatch):
    monkeypatch.setattr(demo_mode, "_MAX_TRACKED_CLIENTS", 2)
    clock = Clock()
    rate = ClientRate(burst=2, interval_s=1.0, now=clock)
    rate.take("a")
    rate.take("b")
    clock.now = 10.0
    rate.take("c")
    assert set(rate._buckets) == {"c"}


def _throttled_client(enabled: dict, burst: int = 2) -> TestClient:
    app = FastAPI()
    rate = ClientRate(burst=burst, interval_s=30.0, now=Clock())
    app.add_middleware(DemoWriteThrottle, enabled=lambda: enabled["on"], rate=rate)

    @app.get("/queue/list")
    def read():
        return {"ok": True}

    @app.put("/queue/next")
    def write():
        return {"ok": True}

    return TestClient(app)


def test_changes_past_the_limit_are_told_when_to_retry():
    client = _throttled_client({"on": True})
    assert [client.put("/queue/next").status_code for _ in range(2)] == [200, 200]
    refused = client.put("/queue/next")
    assert refused.status_code == 429
    assert refused.json() == {"detail": THROTTLED}
    assert refused.headers["Retry-After"] == "30"
    assert all(client.get("/queue/list").status_code == 200 for _ in range(10))


def test_the_throttle_follows_the_live_flag():
    enabled = {"on": False}
    client = _throttled_client(enabled, burst=1)
    assert all(client.put("/queue/next").status_code == 200 for _ in range(5))
    enabled["on"] = True
    assert client.put("/queue/next").status_code == 200
    assert client.put("/queue/next").status_code == 429


@dataclass
class _Page:
    total: int


class _Queue:
    def __init__(self, total: int):
        self.total = total

    async def list(self, offset: int, limit: int) -> _Page:
        return _Page(self.total)


async def test_an_add_that_fits_the_queue_passes():
    await refuse_beyond_queue_limit(True, _Queue(QUEUE_LIMIT - 5), 5)


async def test_an_add_past_the_queue_limit_is_refused():
    with pytest.raises(HTTPException) as refused:
        await refuse_beyond_queue_limit(True, _Queue(QUEUE_LIMIT - 5), 6)
    assert refused.value.status_code == 409
    assert refused.value.detail == QUEUE_FULL


async def test_an_ordinary_server_has_no_queue_limit():
    class Unasked:
        async def list(self, offset, limit):
            raise AssertionError("an ordinary server never counts its queue")

    await refuse_beyond_queue_limit(False, Unasked(), QUEUE_LIMIT * 10)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    return tmp_path


async def _demo_app(tmp_path):
    config = KalinkaConfig()
    config.server.demo_mode = True
    return await server.create_app(str(tmp_path / "overrides.json"), config, {})


async def test_a_demo_server_refuses_more_ids_than_its_queue_holds(isolated):
    async with running(await _demo_app(isolated)) as client:
        ids = [f"jamendo:track:{n}" for n in range(QUEUE_LIMIT + 1)]
        refused = await client.post("/queue/add", json=ids)
        assert refused.status_code == 409
        assert refused.json() == {"detail": QUEUE_FULL}


async def test_a_demo_server_limits_each_visitors_changes(isolated):
    async with running(await _demo_app(isolated)) as client:
        statuses = [
            (await client.put("/queue/pause", params={"paused": True})).status_code
            for _ in range(demo_mode._WRITE_BURST + 1)
        ]
        assert statuses[:-1] == [200] * demo_mode._WRITE_BURST
        assert statuses[-1] == 429
