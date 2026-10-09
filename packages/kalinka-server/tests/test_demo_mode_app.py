"""A demo server built by the production app: what it serves, what it refuses,
and what it tells a client about itself."""

import re

import pytest
from fastapi.routing import APIWebSocketRoute

from kalinka_server import server, update_check
from kalinka_server.config_model import KalinkaConfig
from tests.app_harness import isolate_app, running

DEMO_FLAG = "base_config.server.demo_mode"
CATALOG_FLAG = "base_config.server.plugin_catalog_enabled"

# Every change a demo server lets through. A route added later that changes
# something is refused, or joins this list on purpose.
LET_THROUGH = {
    ("POST", "/queue/add"),
    ("POST", "/queue/replace"),
    ("PUT", "/queue/play"),
    ("PUT", "/queue/pause"),
    ("PUT", "/queue/next"),
    ("PUT", "/queue/prev"),
    ("PUT", "/queue/stop"),
    ("PUT", "/queue/current_track/seek"),
    ("PUT", "/queue/mode"),
    ("PUT", "/queue/clear"),
    ("POST", "/queue/remove"),
    ("PUT", "/queue/move"),
    ("PUT", "/device/set_volume"),
}
SOCKETS = {"/queue/ws", "/device/ws", "/renderer/ws"}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(update_check, "upgrade_supported", lambda: True)
    return tmp_path


async def _app(tmp_path, *, demo: bool):
    config = KalinkaConfig()
    config.server.demo_mode = demo
    return await server.create_app(str(tmp_path / "overrides.json"), config, {})


async def _change(client, changes):
    schema_version = (await client.get("/server/config")).json()["schema_version"]
    return await client.put(
        "/server/config",
        json={"schema_version": schema_version, "changes": changes},
    )


def _refused(response):
    return (
        response.status_code == 403
        and response.json()["detail"]["code"] == "demo_read_only"
    )


async def _renderer_ids(client):
    rows = (await client.get("/renderer/list")).json()["renderers"]
    return [row["renderer_id"] for row in rows]


async def _general_banner_titles(client):
    schema = (await client.get("/server/config/schema")).json()
    general = next(page for page in schema["pages"] if page["id"] == "general")
    return [banner["title"] for banner in general["banners"]]


async def test_a_demo_server_refuses_every_change_but_the_queue(isolated):
    async with running(await _app(isolated, demo=True)) as client:
        assert _refused(await _change(client, {CATALOG_FLAG: True}))
        assert _refused(await client.put("/server/restart"))
        assert _refused(await client.put("/server/upgrade", json={"version": "9"}))
        assert _refused(await client.post("/collections", json={"name": "Mine"}))
        assert _refused(await client.put("/favorite/add/track-1"))
        assert _refused(
            await client.put("/renderer/active", json={"renderer_id": None})
        )
        assert _refused(
            await client.put("/renderer/demo-output/config", json={"changes": {}})
        )
        assert _refused(await client.post("/server/logs/export"))

        assert (
            await client.put("/queue/pause", params={"paused": True})
        ).status_code == 200
        assert (await client.put("/queue/clear")).status_code == 200
        assert (
            await client.put("/device/set_volume", params={"volume": 20})
        ).status_code == 200
        schema_version = (await client.get("/server/config")).json()["schema_version"]
        dry_run = await client.post(
            "/server/config/validate",
            json={"schema_version": schema_version, "changes": {CATALOG_FLAG: True}},
        )
        assert _refused(dry_run)


def _changes(app) -> set[tuple[str, str]]:
    """Every (method, path) the app serves that is not a read."""
    app.openapi_schema = None
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method not in ("get", "head", "options")
    }


async def test_whatever_the_client_a_demo_server_changes_only_the_queue(isolated):
    app = await _app(isolated, demo=True)
    async with running(app) as client:
        changes = _changes(app)
        assert LET_THROUGH <= changes
        for method, path in sorted(changes - LET_THROUGH):
            concrete = re.sub(r"\{[^}]+\}", "x", path)
            response = await client.request(method, concrete)
            assert _refused(response), (method, path, response.status_code)
        sockets = {r.path for r in app.routes if isinstance(r, APIWebSocketRoute)}
        assert sockets == SOCKETS


async def test_a_demo_server_says_what_it_is(isolated):
    async with running(await _app(isolated, demo=True)) as client:
        assert (await client.get("/server/config")).json()["values"][DEMO_FLAG] is True
        assert (await client.get("/server/update")).json()["upgrade_supported"] is False
        assert "Demo server" in await _general_banner_titles(client)
        assert await _renderer_ids(client) == ["demo-output"]
        config = (await client.get("/renderer/demo-output/config")).json()
        assert all(s["path"].startswith("core.") for s in config["sections"])


async def test_an_ordinary_server_keeps_the_flag_out_of_reach(isolated):
    async with running(await _app(isolated, demo=False)) as client:
        assert (await _change(client, {CATALOG_FLAG: True})).status_code == 200
        refused = await _change(client, {DEMO_FLAG: True})
        assert refused.status_code == 400
        assert "read-only" in refused.json()["detail"]
        assert (await client.get("/server/config")).json()["values"][DEMO_FLAG] is False
        assert (await client.get("/server/update")).json()["upgrade_supported"] is True
        assert "Demo server" not in await _general_banner_titles(client)
        assert await _renderer_ids(client) == []


async def test_the_flag_is_shown_read_only(isolated):
    async with running(await _app(isolated, demo=False)) as client:
        schema = (await client.get("/server/config/schema")).json()
        field = next(f for f in schema["expert_fields"] if f["path"] == DEMO_FLAG)
        assert field["readonly"] is True
