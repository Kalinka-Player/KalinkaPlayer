"""The rescan endpoint is a localfiles command, acknowledged without waiting."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from kalinka_server import server
from kalinka_server.config_model import KalinkaConfig
from tests.app_harness import isolate_app, running


@pytest.fixture
async def app(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    return await server.create_app(
        str(tmp_path / "overrides.json"), KalinkaConfig(), {}
    )


async def test_rescan_targets_localfiles_and_status_keeps_scan_metadata(
    app, monkeypatch
):
    calls = []
    status = {
        "indexing": {"scan_pending": True, "scanning": False, "last_scan_at": 123.5}
    }

    async def request():
        calls.append("scan")

    async def get_status():
        return status

    module = SimpleNamespace(
        request_rescan=request,
        get_indexer_status=get_status,
        module_name=lambda: "localfiles",
    )

    def enabled(name):
        assert name == "localfiles"
        return module

    monkeypatch.setattr(server, "enabled_input_module", enabled)
    monkeypatch.setattr(server, "extract_modules", lambda _: [module])
    async with running(app) as client:
        response = await client.post("/indexer/rescan")
        assert response.status_code == 202
        assert calls == ["scan"]
        response = await client.get("/indexer/status")
        assert response.json() == {"localfiles": status}
        assert response.json()["localfiles"]["indexing"]["rescan_supported"] is True


@pytest.mark.parametrize(
    "kind, code", [("disabled", 404), ("older_plugin", 501), ("unavailable", 503)]
)
async def test_unavailable_rescan_is_not_accepted(app, monkeypatch, kind, code):
    async def unavailable():
        raise RuntimeError("Indexer stopped")

    def enabled(_):
        if kind == "disabled":
            raise HTTPException(404, "localfiles unavailable")
        return SimpleNamespace(
            **({} if kind == "older_plugin" else {"request_rescan": unavailable})
        )

    monkeypatch.setattr(server, "enabled_input_module", enabled)
    async with running(app) as client:
        response = await client.post("/indexer/rescan")
        assert response.status_code == code


async def test_older_plugin_does_not_advertise_rescan(app, monkeypatch):
    async def get_status():
        return {"indexing": {"total": 1, "pending": 1}}

    module = SimpleNamespace(
        get_indexer_status=get_status, module_name=lambda: "localfiles"
    )
    monkeypatch.setattr(server, "extract_modules", lambda _: [module])
    async with running(app) as client:
        status = (await client.get("/indexer/status")).json()
        assert status["localfiles"]["indexing"]["rescan_supported"] is False
