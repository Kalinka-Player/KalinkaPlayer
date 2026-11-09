"""CrossOriginIsolationMiddleware: COOP/COEP land on every response, so the
web UI's WASM build can cross-origin-isolate and run skwasm multi-threaded."""

from kalinka_server import server
from kalinka_server.config_model import KalinkaConfig
from tests.app_harness import isolate_app, running


async def _app(tmp_path):
    config = KalinkaConfig()
    return await server.create_app(str(tmp_path / "overrides.json"), config, {})


async def test_api_response_carries_coi_headers(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    async with running(await _app(tmp_path)) as client:
        response = await client.get("/server/config")
        assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert response.headers["Cross-Origin-Embedder-Policy"] == "credentialless"


async def test_web_ui_install_page_carries_coi_headers(monkeypatch, tmp_path):
    # No kalinka-web bundle installed — WebUiStaticFiles' own fallback path.
    isolate_app(monkeypatch, tmp_path)
    async with running(await _app(tmp_path)) as client:
        response = await client.get("/")
        assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert response.headers["Cross-Origin-Embedder-Policy"] == "credentialless"
