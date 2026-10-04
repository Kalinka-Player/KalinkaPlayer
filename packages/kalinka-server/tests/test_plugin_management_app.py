"""The production app wires plugin management to the saved preview flag.

Only the outside world is faked: installed plugins, mDNS, the release check
and the catalog's upstream HTTP. Everything else is what ``create_app`` builds.
"""

import asyncio
from contextlib import asynccontextmanager
import json

import httpx
import pytest

from kalinka_server import server, update_check
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.player_setup import modules
from kalinka_server.plugin_management import catalog
from kalinka_server.plugin_management.inventory import CAPABILITIES
from tests.plugin_catalog_fakes import catalog_document

FLAG = "base_config.server.plugin_catalog_enabled"


class _NoServiceDiscovery:
    def __init__(self, *_args, **_kwargs):
        pass

    async def register_service(self):
        pass

    async def unregister_service(self):
        pass


async def _no_release_check(*_args, **_kwargs):
    await asyncio.Event().wait()


@pytest.fixture
def upstream(monkeypatch, tmp_path):
    """An isolated server; holds the requests sent to the catalog upstream."""
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    # create_app rebinds these process globals; restore them afterwards.
    for name, value in list(vars(modules).items()):
        monkeypatch.setattr(modules, name, value)
    monkeypatch.setattr(server, "_browse_registry", server._browse_registry)
    monkeypatch.setattr(modules, "_scan_entry_points", lambda: iter(()))
    monkeypatch.setattr(server, "ServiceDiscovery", _NoServiceDiscovery)
    monkeypatch.setattr(update_check.checker, "run", _no_release_check)
    monkeypatch.setattr(catalog, "FLAG_POLL_SECONDS", 0.01)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=catalog_document())

    def from_environment(cls, *, enabled):
        return cls(transport=httpx.MockTransport(handler), enabled=enabled)

    monkeypatch.setattr(
        catalog.PublicCatalog, "from_environment", classmethod(from_environment)
    )
    return requests


@asynccontextmanager
async def running(app):
    # The lifespan swallows exceptions raised into it, so a failed assertion
    # must not be thrown through it.
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        await lifespan.__aexit__(None, None, None)


async def _save_flag(client, enabled):
    schema_version = (await client.get("/server/config")).json()["schema_version"]
    response = await client.put(
        "/server/config",
        json={"schema_version": schema_version, "changes": {FLAG: enabled}},
    )
    assert response.status_code == 200


async def _assert_disabled(client):
    version = (await client.get("/server/version")).json()
    assert version["plugin_management"] == CAPABILITIES | {"enabled": False}
    for path in ("/server/plugins", "/server/plugins/catalog"):
        response = await client.get(path)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "plugin_catalog_disabled"


async def _until(condition):
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


async def test_saved_flag_drives_routes_capabilities_and_refresh(upstream, tmp_path):
    overrides = tmp_path / "overrides.json"
    app = await server.create_app(str(overrides), KalinkaConfig(), {})
    async with running(app) as client:
        await _assert_disabled(client)
        await asyncio.sleep(0.05)
        assert upstream == []

        await _save_flag(client, True)
        assert json.loads(overrides.read_text())[FLAG] is True

        await _until(lambda: upstream)
        await _until(
            lambda: app.state.plugin_catalog.snapshot()["status"] == "available"
        )
        version = (await client.get("/server/version")).json()
        assert version["plugin_management"]["enabled"] is True
        assert (await client.get("/server/plugins")).status_code == 200
        response = await client.get("/server/plugins/catalog")
        assert response.status_code == 200
        assert [plugin["id"] for plugin in response.json()["plugins"]] == ["demo"]

        await _save_flag(client, False)
        await _assert_disabled(client)
        refresh = app.state.plugin_catalog_task
        assert not refresh.done()

    assert refresh.cancelled()
