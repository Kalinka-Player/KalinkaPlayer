"""The production app wires plugin management to the saved preview flag.

Only the outside world is faked: installed plugins, mDNS, the release check
and the catalog's upstream HTTP. Everything else is what ``create_app`` builds.
"""

import asyncio
import json

import httpx
import pytest

from kalinka_server import server
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.plugin_management import catalog
from kalinka_server.plugin_management.compatibility import (
    CatalogCompatibility,
    HostEnvironment,
)
from kalinka_server.plugin_management.inventory import CAPABILITIES
from kalinka_server.plugin_management.operations import REQUEST_FILE
from tests.app_harness import isolate_app, running
from tests.plugin_catalog_fakes import catalog_document

FLAG = "base_config.server.plugin_catalog_enabled"


@pytest.fixture
def upstream(monkeypatch, tmp_path):
    """An isolated server; holds the requests sent to the catalog upstream."""
    isolate_app(monkeypatch, tmp_path)
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
    # Switched off, as a server whose owner turned the preview off.
    config = KalinkaConfig()
    config.server.plugin_catalog_enabled = False
    app = await server.create_app(str(overrides), config, {})
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


async def test_install_request_restarts_through_the_packaged_helper(
    upstream, tmp_path, monkeypatch
):
    """The production app reports whether it can install at all, and when it
    can, writes the helper's request and fires the restart trigger."""
    overrides = tmp_path / "overrides.json"
    app = await server.create_app(str(overrides), KalinkaConfig(), {})
    async with running(app) as client:
        await _save_flag(client, True)
        await _until(
            lambda: app.state.plugin_catalog.snapshot()["status"] == "available"
        )
        # A source checkout has no root-side restart service.
        version = (await client.get("/server/version")).json()
        assert version["plugin_management"]["installation"] is False
        response = await client.post("/server/plugins/demo/install")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "installer_unavailable"
        inventory = (await client.get("/server/plugins")).json()
        assert inventory["capabilities"]["installation"] is False
        assert inventory["last_operation"] is None

    # A packaged server on a Debian box the fake catalog's release targets.
    monkeypatch.setattr(server, "installation_supported", lambda: True)
    monkeypatch.setattr(
        server,
        "CatalogCompatibility",
        lambda: CatalogCompatibility(
            lambda: HostEnvironment(
                "linux", "x86_64", "deb", "debian", "13", "5.3.0", "3.5.0", "3.11.0"
            )
        ),
    )
    app = await server.create_app(
        str(overrides), KalinkaConfig(), json.loads(overrides.read_text())
    )
    async with running(app) as client:
        await _until(
            lambda: app.state.plugin_catalog.snapshot()["status"] == "available"
        )
        version = (await client.get("/server/version")).json()
        assert version["plugin_management"]["installation"] is True

        response = await client.post("/server/plugins/demo/install")
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "restarting"
        request = json.loads((tmp_path / "var/lib/kalinka" / REQUEST_FILE).read_text())
        assert request["action"] == "install" and request["version"] == "1.0.0"
        assert (tmp_path / "run/kalinka/restart-request").exists()

        # One request at a time: the restart it fired carries this one out.
        response = await client.post("/server/plugins/demo/install")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "operation_in_progress"
        # Its helper has taken the request, but the restart is not over yet.
        (tmp_path / "var/lib/kalinka" / REQUEST_FILE).unlink()
        response = await client.post("/server/plugins/demo/install")
        assert response.status_code == 409
        assert "already restarting." in response.json()["detail"]["message"]
        assert not (tmp_path / "var/lib/kalinka" / REQUEST_FILE).exists()
        response = await client.post("/server/plugins/demo/uninstall")
        assert response.json()["detail"]["code"] == "not_installed"

        await _save_flag(client, False)
        response = await client.post("/server/plugins/demo/install")
        assert response.status_code == 403
