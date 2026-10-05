"""A demo server built by the production app: what it serves, what it refuses,
and what it tells a client about itself."""

import pytest

from kalinka_server import server, update_check
from kalinka_server.config_model import KalinkaConfig
from tests.app_harness import isolate_app, running

DEMO_FLAG = "base_config.server.demo_mode"
CATALOG_FLAG = "base_config.server.plugin_catalog_enabled"


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
        assert dry_run.status_code == 200


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
