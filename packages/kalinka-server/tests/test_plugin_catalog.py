"""Public browsing never confers plugin identity or installation permission."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json

from fastapi import FastAPI
import httpx
import pytest

from kalinka_server.plugin_management.catalog import (
    BASE_URL_ENV,
    CATALOG_ID,
    DEFAULT_BASE_URL,
    MAX_CATALOG_BYTES,
    STALE_SECONDS,
    CatalogError,
    PublicCatalog,
    catalog_url,
    parse_public_catalog,
)
from kalinka_server.plugin_management.inventory import (
    Discovery,
    EntryPoint,
    Installation,
    PluginInventory,
)
from kalinka_server.plugin_management.route import register_plugin_routes


@pytest.fixture
def document():
    return {
        "schema_version": 1,
        "catalog_id": "kalinka",
        "revision": "test-public-revision",
        "plugins": [
            {
                "schema_version": 1,
                "id": "demo",
                "name": "Demo input",
                "description": "Example input source",
                "distribution": "kalinka-plugin-demo",
                "entry_point": "kalinka_plugin_demo",
                "creator": {"name": "Creator", "url": "https://example.org/creator"},
                "maintainers": [
                    {"name": "Maintainer", "url": "https://example.org/team"}
                ],
                "license": "MIT",
                "source": {
                    "repository": "https://example.org/source",
                    "subdirectory": "",
                },
                "type": "input_module",
                "tier": "unofficial",
                "maturity": "experimental",
                "categories": ["radio"],
                "delivery": "independent",
                "releases": [
                    {
                        "version": "1.0.0",
                        "channel": "stable",
                        "published_at": "2026-10-01T00:00:00Z",
                        "source_tag": "v1.0.0",
                        "source_commit": "c" * 40,
                        "release_notes": "https://example.org/releases/v1.0.0",
                        "requires": {
                            "server": ">=5.3,<6",
                            "sdk": ">=3.5,<4",
                            "python": ">=3.11",
                            "platforms": ["linux"],
                            "architectures": ["all"],
                            "capabilities": [],
                            "notes": [],
                        },
                        "data_rollback": "manual",
                        "withdrawn": False,
                        "artifacts": [
                            {
                                "format": "deb",
                                "filename": "kalinka-plugin-demo_1.0.0_all.deb",
                                "platform": "linux",
                                "architectures": ["all"],
                                "package": {
                                    "name": "kalinka-plugin-demo",
                                    "version": "1.0.0",
                                    "architecture": "all",
                                },
                                "targets": [{"id": "debian", "versions": ["13"]}],
                                "url": "https://example.org/releases/v1.0.0/kalinka-plugin-demo_1.0.0_all.deb",
                                "sha256": "a" * 64,
                                "size_bytes": 1024,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def encode(document):
    return json.dumps(document).encode()


def service(handler, **kwargs):
    return PublicCatalog(transport=httpx.MockTransport(handler), **kwargs)


def test_default_url_and_environment_override(monkeypatch):
    monkeypatch.delenv(BASE_URL_ENV, raising=False)
    assert (
        PublicCatalog.from_environment().snapshot()["source_url"]
        == DEFAULT_BASE_URL + "catalog.json"
    )
    monkeypatch.setenv(BASE_URL_ENV, "https://kalinkaplayer.com/plugins")
    result = PublicCatalog.from_environment().snapshot()
    assert result["source_url"] == "https://kalinkaplayer.com/plugins/catalog.json"
    assert result["catalog_id"] == CATALOG_ID
    monkeypatch.setenv(BASE_URL_ENV, "")
    assert PublicCatalog.from_environment().snapshot()["status"] == "disabled"


@pytest.mark.parametrize(
    "base",
    [
        "http://example.org/",
        "file:///tmp/",
        "https://user:password@example.org/",
        "https://example.org/?token=secret",
        "https://example.org/#fragment",
        "https://example.org:8443/",
        "https://example.org/../private",
        "https://example.org/%2e%2e/private",
        "https://example.org/\\private",
        "https://example.org/\nprivate",
        "https:///missing-host",
        " https://example.org/",
    ],
)
def test_invalid_base_url_is_not_used_or_exposed(base):
    with pytest.raises(ValueError):
        catalog_url(base)
    result = PublicCatalog(base).snapshot()
    assert result["status"] == "unavailable"
    assert result["error"] == "invalid_configuration"
    assert result["source_url"] is None


def test_parse_preserves_browsing_metadata(document):
    plugin = document["plugins"][0]
    plugin["type"] = "output_device"
    plugin["device_support"] = {
        "models": ["Example AVR 100"],
        "families": ["Example network AVR"],
        "notes": ["Provides volume and power controls via REST."],
    }
    assert parse_public_catalog(encode(document)) == document


@pytest.mark.parametrize(
    "published_at", ["not-a-date", "2026-10-01", "2026-10-01T12:00:00"]
)
def test_release_timestamp_requires_timezone(document, published_at):
    document["plugins"][0]["releases"][0]["published_at"] = published_at
    with pytest.raises(CatalogError):
        parse_public_catalog(encode(document))


def test_lone_unicode_surrogate_cannot_poison_rest_cache(document):
    document["plugins"][0]["description"] = "bad UTF-8: \ud800"
    with pytest.raises(CatalogError):
        parse_public_catalog(encode(document))


@pytest.mark.asyncio
async def test_overall_deadline_cancels_stalled_response(monkeypatch):
    from kalinka_server.plugin_management import catalog as catalog_module

    monkeypatch.setattr(catalog_module, "REQUEST_DEADLINE_SECONDS", 0.01)

    async def handler(request):
        await asyncio.Event().wait()

    catalog = service(handler)
    await catalog.refresh()
    assert catalog.snapshot()["error"] == "timeout"
    assert catalog.snapshot()["refreshing"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(schema_version=True),
        lambda d: d.update(schema_version=2),
        lambda d: d.update(catalog_id="another-publisher"),
        lambda d: d.update(verified=True),
        lambda d: d.update(plugins=[]),
        lambda d: d.update(revision=""),
        lambda d: d["plugins"].append(deepcopy(d["plugins"][0])),
        lambda d: d["plugins"][0].update(schema_version=True),
        lambda d: d["plugins"][0].update(type="output_device"),
        lambda d: d["plugins"][0].update(verified=True),
        lambda d: d["plugins"][0]["creator"].update(
            url="https://user:secret@example.org/"
        ),
        lambda d: d["plugins"][0]["releases"][0].update(version="commit-abc"),
        lambda d: d["plugins"][0]["releases"][0].update(version="1.0rc1"),
        lambda d: d["plugins"][0]["releases"][0]["requires"].update(
            server="some-commit"
        ),
        lambda d: d["plugins"][0]["releases"][0]["requires"].update(
            architectures=["all", "aarch64"]
        ),
        lambda d: d["plugins"][0]["releases"][0]["artifacts"][0].update(
            sha256="invalid"
        ),
        lambda d: d["plugins"][0]["releases"][0]["artifacts"][0].update(platform="all"),
    ],
)
def test_malformed_or_self_attested_metadata_rejected(document, change):
    change(document)
    with pytest.raises(CatalogError):
        parse_public_catalog(encode(document))


@pytest.mark.parametrize(
    "body",
    [
        b'{"catalog_id":"kalinka","catalog_id":"impostor"}',
        b"[]",
        b"null",
        b'{"schema_version":NaN}',
        b"\xff",
        b"[" * 2000 + b"]" * 2000,
        b" " * (MAX_CATALOG_BYTES + 1),
    ],
)
def test_invalid_json_and_size_limits(body):
    with pytest.raises(CatalogError):
        parse_public_catalog(body)


@pytest.mark.asyncio
async def test_anonymous_fetch_and_cache_only_reads(document, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leave-this-process")
    requests = []
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def handler(request):
        requests.append(request)
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.url == DEFAULT_BASE_URL + "catalog.json"
        return httpx.Response(
            200,
            content=encode(document),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    catalog = service(handler, clock=lambda: now)
    assert catalog.snapshot()["status"] == "unavailable"
    assert requests == []
    await catalog.refresh()
    result = catalog.snapshot()
    assert result["status"] == "available"
    assert result["last_successful_check"] == now.isoformat()
    assert result["plugins"] == document["plugins"]
    assert result["content_sha256"] == hashlib.sha256(encode(document)).hexdigest()
    assert result["trust"]["status"] == "unverified"
    assert result["compatibility"]["status"] == "not_evaluated"
    assert result["installation_allowed"] is False
    assert result["automatic_updates_enabled"] is False
    result["plugins"].clear()
    assert catalog.snapshot()["plugins"] == document["plugins"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_conditional_refresh_and_stale_cache(document):
    tick = [0]
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json=document,
                headers={"ETag": '"r1"', "Set-Cookie": "session=private"},
            )
        assert request.headers["if-none-match"] == '"r1"'
        assert "cookie" not in request.headers
        return httpx.Response(304)

    catalog = service(handler, monotonic=lambda: tick[0])
    await catalog.refresh()
    tick[0] += STALE_SECONDS
    assert catalog.snapshot()["status"] == "stale"
    await catalog.refresh()
    assert catalog.snapshot()["status"] == "available"
    assert catalog.snapshot()["plugins"] == document["plugins"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,code",
    [
        (httpx.Response(404), "http_status_404"),
        (
            httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}),
            "redirect_refused",
        ),
        (
            httpx.Response(
                200, text="bad JSON", headers={"Content-Type": "application/json"}
            ),
            "invalid_catalog",
        ),
        (
            httpx.Response(
                200,
                text="<html>wrong feed</html>",
                headers={"Content-Type": "text/html"},
            ),
            "invalid_content_type",
        ),
        (
            httpx.Response(
                200, content=gzip.compress(b"{}"), headers={"Content-Encoding": "gzip"}
            ),
            "unsupported_encoding",
        ),
        (
            httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(MAX_CATALOG_BYTES + 1),
                },
            ),
            "response_too_large",
        ),
    ],
)
async def test_failed_refresh_retains_last_valid_document(document, failure, code):
    replies = [httpx.Response(200, json=document, headers={"ETag": '"good"'}), failure]
    catalog = service(lambda request: replies.pop(0))
    await catalog.refresh()
    before = catalog.snapshot()
    await catalog.refresh()
    after = catalog.snapshot()
    assert after["status"] == "stale"
    assert after["error"] == code
    assert after["last_successful_check"] == before["last_successful_check"]
    assert after["plugins"] == before["plugins"]
    assert after["content_sha256"] == before["content_sha256"]


class OversizedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(33):
            yield b" " * 65536


@pytest.mark.asyncio
async def test_stream_size_limit_without_content_length():
    catalog = service(
        lambda request: httpx.Response(
            200, stream=OversizedStream(), headers={"Content-Type": "application/json"}
        )
    )
    await catalog.refresh()
    assert catalog.snapshot()["error"] == "response_too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code",
    [
        (httpx.ConnectError("https://private:secret@host/body"), "network_error"),
        (httpx.ReadTimeout("private body"), "timeout"),
    ],
)
async def test_safe_network_errors(error, code):
    def handler(request):
        raise error

    catalog = service(handler)
    await catalog.refresh()
    result = catalog.snapshot()
    assert result["status"] == "unavailable"
    assert result["error"] == code
    assert "secret" not in json.dumps(result)
    assert "private body" not in json.dumps(result)


@pytest.mark.asyncio
async def test_unexpected_304_is_not_success():
    catalog = service(lambda request: httpx.Response(304))
    await catalog.refresh()
    assert catalog.snapshot()["error"] == "unexpected_not_modified"
    assert catalog.snapshot()["last_successful_check"] is None


@pytest.mark.asyncio
async def test_refreshes_are_coalesced(document):
    entered = asyncio.Event()
    release = asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=document)

    catalog = service(handler)
    first = asyncio.create_task(catalog.refresh())
    await entered.wait()
    assert catalog.snapshot()["refreshing"] is True
    second = asyncio.create_task(catalog.refresh())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert len(requests) == 1
    assert catalog.snapshot()["refreshing"] is False


@pytest.mark.asyncio
async def test_background_refresh_cancels_cleanly(document):
    completed = asyncio.Event()

    def handler(request):
        completed.set()
        return httpx.Response(200, json=document)

    catalog = service(handler)
    task = asyncio.create_task(catalog.run())
    await completed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert catalog.snapshot()["status"] == "available"
    assert catalog.snapshot()["refreshing"] is False


@pytest.mark.asyncio
async def test_disabled_service_never_contacts_upstream():
    def handler(request):
        pytest.fail("Disabled catalog made a request")

    catalog = service(handler, base_url="")
    await catalog.refresh()
    await catalog.run()
    assert catalog.snapshot()["status"] == "disabled"


@pytest.mark.asyncio
async def test_preview_flag_blocks_fetch_and_hides_cached_entries(document):
    enabled = [False]
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=document)

    catalog = service(handler, enabled=lambda: enabled[0])
    await catalog.refresh()
    assert requests == []
    assert catalog.snapshot()["status"] == "disabled"
    enabled[0] = True
    await catalog.refresh()
    assert len(requests) == 1
    assert catalog.snapshot()["plugins"] == document["plugins"]
    enabled[0] = False
    await catalog.refresh()
    assert len(requests) == 1
    assert catalog.snapshot()["status"] == "disabled"
    assert catalog.snapshot()["plugins"] == []


@pytest.mark.asyncio
async def test_polling_observes_live_opt_in_without_restart(document, monkeypatch):
    from kalinka_server.plugin_management import catalog as catalog_module

    monkeypatch.setattr(catalog_module, "FLAG_POLL_SECONDS", 0.001)
    enabled = [False]
    fetched = asyncio.Event()
    requests = []

    def handler(request):
        requests.append(request)
        fetched.set()
        return httpx.Response(200, json=document)

    catalog = service(handler, enabled=lambda: enabled[0])
    task = asyncio.create_task(catalog.run())
    try:
        await asyncio.sleep(0.005)
        assert requests == []
        enabled[0] = True
        await asyncio.wait_for(fetched.wait(), timeout=1)
        assert len(requests) == 1
        enabled[0] = False
        await asyncio.sleep(0.005)
        assert len(requests) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_disabling_during_fetch_does_not_publish_response(document):
    enabled = [True]
    entered, finish = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        await finish.wait()
        return httpx.Response(200, json=document)

    catalog = service(handler, enabled=lambda: enabled[0])
    task = asyncio.create_task(catalog.refresh())
    await entered.wait()
    enabled[0] = False
    finish.set()
    await task
    enabled[0] = True
    assert catalog.snapshot()["plugins"] == []
    assert catalog.snapshot()["last_successful_check"] is None


def test_routes_are_disabled_by_default():
    app = FastAPI()
    register_plugin_routes(app, PluginInventory(lambda: Discovery((), True)))
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        for path in (
            "/server/plugins",
            "/server/plugins/catalog",
            "/server/plugins/updates",
        ):
            response = client.get(path)
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "plugin_catalog_disabled"


@pytest.mark.asyncio
async def test_migration_keeps_identity_and_does_not_follow_feed_urls(document):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json=document)

    before = service(handler)
    after = service(handler, base_url="https://kalinkaplayer.com/plugins/")
    await before.refresh()
    await after.refresh()
    assert (
        before.snapshot()["catalog_id"] == after.snapshot()["catalog_id"] == "kalinka"
    )
    assert before.snapshot()["content_sha256"] == after.snapshot()["content_sha256"]
    assert urls == [
        DEFAULT_BASE_URL + "catalog.json",
        "https://kalinkaplayer.com/plugins/catalog.json",
    ]


@pytest.mark.asyncio
async def test_rest_browsing_does_not_promote_installed_identity(document):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=document)

    catalog = service(handler)
    inventory = PluginInventory(
        lambda: Discovery(
            (
                Installation(
                    "kalinka-plugin-demo",
                    "1.0.0",
                    (EntryPoint("kalinka_plugin_demo", "demo:Plugin"),),
                    "/private/site-packages",
                    origin="manual",
                ),
            ),
            True,
        )
    )
    app = FastAPI()
    register_plugin_routes(app, inventory, catalog, enabled=lambda: True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/server/plugins/catalog")).status_code == 503
        assert requests == []
        await catalog.refresh()
        response = await client.get("/server/plugins/catalog")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["plugins"] == document["plugins"]
        installed = (await client.get("/server/plugins")).json()
        assert installed["capabilities"]["catalog_browsing"] is True
        assert installed["plugins"][0]["catalog_binding"] is None
        assert installed["plugins"][0]["catalog_match"] == "unverified"
        updates = (await client.get("/server/plugins/updates")).json()
        assert updates["status"] == "unavailable"
        assert updates["automatic_updates_enabled"] is False
        assert len(requests) == 1
