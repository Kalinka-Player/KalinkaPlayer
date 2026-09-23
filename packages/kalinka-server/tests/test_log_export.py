"""``/server/logs/export``: one export per server, prepared in the background.

A client starts it, polls it, downloads it once ready, and may withdraw it at
any point. A withdrawn export never turns ready, a running download survives
the archive being deleted underneath it, and the server stays responsive
while an export is prepared.
"""

import asyncio
import io
import os
import stat
import threading
import time
import zipfile

import httpx
import pytest
from fastapi import FastAPI

from kalinka_server import log_export_service
from kalinka_server.log_archive import ExportCancelled, write_archive
from kalinka_server.log_export_route import (
    _CheckedOutFileResponse,
    register_log_export_routes,
)
from kalinka_server.log_export_service import ExportManager
from tests.log_export_fakes import FixedCatalog, ListSource, records

URL = "/server/logs/export"
DAY = 86400


def _server(*pairs, **kwargs):
    now = time.time()
    return ListSource(
        "server",
        records(*pairs) if pairs else records((now - 10, "a server line")),
        **kwargs,
    )


@pytest.fixture
def export_dir(tmp_path):
    return tmp_path / "log-export"


def _manager(export_dir, *sources, **kwargs):
    manager = ExportManager(
        str(export_dir), FixedCatalog(*sources), lambda: ["hunter2"], **kwargs
    )
    manager.open()
    return manager


def _client(manager):
    app = FastAPI()
    register_log_export_routes(app, manager)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://kalinka"
    )


async def _until_state(client, state, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        body = (await client.get(URL)).json()
        if body["state"] == state or time.monotonic() > deadline:
            return body
        await asyncio.sleep(0.01)


async def test_no_export_reads_as_none_with_the_sources_on_offer(export_dir):
    manager = _manager(export_dir, _server(), ListSource("local_renderer"))
    async with _client(manager) as client:
        response = await client.get(URL)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "state": "none",
        "available_sources": ["server", "local_renderer"],
        "download": None,
        "warnings": [],
        "error": None,
    }


async def test_prepare_poll_and_download_end_to_end(export_dir):
    manager = _manager(export_dir, _server())
    async with _client(manager) as client:
        started = await client.post(URL, json={"lookback_seconds": DAY})
        ready = await _until_state(client, "ready")
        download = await client.get(f"{URL}/download")
        head = await client.head(f"{URL}/download")

    assert started.status_code == 202
    assert started.headers["cache-control"] == "no-store"
    assert started.json()["state"] == "preparing"
    since, until = (started.json()["requested_range"][k] for k in ("since", "until"))
    assert since.endswith("Z") and until.endswith("Z")
    assert ready["requested_range"] == started.json()["requested_range"]

    info = ready["download"]
    assert info["filename"].startswith("kalinka-logs-") and info["filename"].endswith("Z.zip")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    assert download.headers["content-disposition"] == f'attachment; filename="{info["filename"]}"'
    assert download.headers["content-length"] == str(info["size_bytes"])
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert "content-encoding" not in download.headers
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert sorted(archive.namelist()) == ["manifest.json", "server.log"]
        assert archive.read("server.log") == b"a server line\n"

    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(info["size_bytes"])


async def test_an_archive_downloads_again_and_leaves_no_copies_behind(export_dir):
    manager = _manager(export_dir, _server())
    async with _client(manager) as client:
        await client.post(URL, json={"lookback_seconds": DAY})
        await _until_state(client, "ready")
        first = await client.get(f"{URL}/download")
        second = await client.get(f"{URL}/download")

    assert first.content == second.content
    assert [name for name in os.listdir(export_dir) if not name.endswith(".zip")] == []


async def test_a_second_start_while_preparing_returns_the_running_export(export_dir):
    gate = threading.Event()
    manager = _manager(export_dir, _server(gate=gate))
    async with _client(manager) as client:
        first = await client.post(URL, json={"lookback_seconds": DAY})
        again = await client.post(URL, json={"lookback_seconds": 3600})
        gate.set()
        await _until_state(client, "ready")

    assert again.status_code == 409
    assert again.headers["cache-control"] == "no-store"
    body = again.json()
    assert body["code"] == "export_in_progress"
    assert body["export"]["state"] == "preparing"
    assert body["export"]["requested_range"] == first.json()["requested_range"]


async def test_starting_after_ready_replaces_the_archive(export_dir):
    manager = _manager(export_dir, _server())
    async with _client(manager) as client:
        await client.post(URL, json={"lookback_seconds": DAY})
        await _until_state(client, "ready")
        old = set(os.listdir(export_dir))
        replaced = await client.post(URL, json={"lookback_seconds": 3600})
        await _until_state(client, "ready")

    assert replaced.status_code == 202
    assert not old & set(os.listdir(export_dir))
    assert len(os.listdir(export_dir)) == 1


async def test_withdrawing_during_collection_is_prompt_and_leaves_no_files(export_dir):
    source = _server(gate=threading.Event())
    manager = _manager(export_dir, source)
    async with _client(manager) as client:
        await client.post(URL, json={"lookback_seconds": DAY})
        assert await asyncio.to_thread(source.reached_gate.wait, 2)
        started = time.monotonic()
        withdrawn = await client.delete(URL)
        elapsed = time.monotonic() - started
        after = (await client.get(URL)).json()

    assert withdrawn.status_code == 204
    assert withdrawn.headers["cache-control"] == "no-store"
    assert elapsed < 1.0
    assert after["state"] == "none"
    assert os.listdir(export_dir) == []


async def test_every_withdrawal_waits_for_the_cancelled_worker_to_stop(export_dir, monkeypatch):
    running = threading.Event()
    release = threading.Event()

    def slow_to_stop(*args):
        running.set()
        release.wait(5)
        raise ExportCancelled("withdrawn")

    monkeypatch.setattr(log_export_service, "write_archive", slow_to_stop)
    manager = _manager(export_dir, _server())
    await manager.start(DAY, False)
    assert await asyncio.to_thread(running.wait, 2)
    first = asyncio.create_task(manager.withdraw())
    await asyncio.sleep(0.01)
    second = asyncio.create_task(manager.withdraw())
    await asyncio.sleep(0.05)

    assert not second.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 2)


async def test_withdrawing_with_nothing_to_withdraw_is_still_no_content(export_dir):
    async with _client(_manager(export_dir, _server())) as client:
        assert (await client.delete(URL)).status_code == 204


async def test_a_withdrawal_racing_publication_never_yields_ready(export_dir, monkeypatch):
    published = threading.Event()
    release = threading.Event()

    def publish_then_wait(*args):
        result = write_archive(*args)
        published.set()
        release.wait(5)
        return result

    monkeypatch.setattr(log_export_service, "write_archive", publish_then_wait)
    manager = _manager(export_dir, _server())
    async with _client(manager) as client:
        await client.post(URL, json={"lookback_seconds": DAY})
        assert await asyncio.to_thread(published.wait, 2)
        withdrawal = asyncio.create_task(client.delete(URL))
        await asyncio.sleep(0.05)
        release.set()
        assert (await withdrawal).status_code == 204
        after = (await client.get(URL)).json()

    assert after["state"] == "none"
    assert os.listdir(export_dir) == []


async def test_a_collection_past_its_deadline_fails_as_a_timeout(export_dir):
    manager = _manager(export_dir, _server(gate=threading.Event()), deadline_seconds=0.2)
    async with _client(manager) as client:
        await client.post(URL, json={"lookback_seconds": DAY})
        failed = await _until_state(client, "failed")

    assert failed["error"] == {
        "code": "collection_timeout",
        "message": "Collecting the logs took too long.",
    }
    assert failed["download"] is None
    assert os.listdir(export_dir) == []


async def test_a_failed_export_is_replaced_by_the_next_start(export_dir):
    manager = _manager(export_dir, _server(fail=True))
    async with _client(manager) as client:
        await client.post(URL, json={"lookback_seconds": DAY})
        failed = await _until_state(client, "failed")
        again = await client.post(URL, json={"lookback_seconds": DAY})

    assert failed["error"]["code"] == "logs_unreadable"
    assert again.status_code == 202


async def test_a_failing_renderer_source_still_gives_a_ready_archive(export_dir):
    manager = _manager(export_dir, _server(), ListSource("local_renderer", fail=True))
    async with _client(manager) as client:
        await client.post(URL, json={"lookback_seconds": DAY, "include_local_renderer": True})
        ready = await _until_state(client, "ready")

    assert [(w["source"], w["code"]) for w in ready["warnings"]] == [
        ("local_renderer", "source_unreadable")
    ]


async def test_asking_for_a_renderer_this_server_does_not_have_is_invalid(export_dir):
    async with _client(_manager(export_dir, _server())) as client:
        response = await client.post(
            URL, json={"lookback_seconds": DAY, "include_local_renderer": True}
        )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


async def test_a_server_without_its_log_source_answers_unavailable(export_dir):
    async with _client(_manager(export_dir)) as client:
        response = await client.post(URL, json={"lookback_seconds": DAY})

    assert response.status_code == 503
    assert response.json() == {
        "code": "log_source_unavailable",
        "message": "This server cannot read its logs; its log reader is not installed.",
    }


@pytest.mark.parametrize(
    "body",
    [
        {"lookback_seconds": 59},
        {"lookback_seconds": 604801},
        {"lookback_seconds": 3600.5},
        {"lookback_seconds": "3600"},
        {"lookback_seconds": True},
        {"lookback_seconds": 3600, "include_local_renderer": "yes"},
        {"lookback_seconds": 3600, "format": "tar"},
        {},
    ],
)
async def test_an_invalid_request_is_refused(export_dir, body):
    async with _client(_manager(export_dir, _server())) as client:
        response = await client.post(URL, json=body)

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": "invalid_request", "message": "The request is not valid."}


@pytest.mark.parametrize(
    "content, content_type",
    [
        (b"lookback_seconds=3600", "application/x-www-form-urlencoded"),
        (b'{"lookback_seconds": 3600}', "text/plain"),
        (b"not json", "application/json"),
    ],
)
async def test_a_body_that_is_not_json_is_refused(export_dir, content, content_type):
    async with _client(_manager(export_dir, _server())) as client:
        response = await client.post(URL, content=content, headers={"content-type": content_type})
        after = (await client.get(URL)).json()

    assert response.status_code == 422
    assert after["state"] == "none"


async def test_downloading_with_nothing_ready_is_a_conflict(export_dir):
    async with _client(_manager(export_dir, _server())) as client:
        response = await client.get(f"{URL}/download")

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "export_not_ready"
    assert response.json()["export"]["state"] == "none"


async def _serve(response):
    """Run a response the way the server would and return its body."""
    body = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            body.append(message.get("body", b""))

    scope = {"type": "http", "method": "GET", "headers": []}
    await response(scope, receive, send)
    return b"".join(body)


@pytest.mark.parametrize("what", ["withdraw", "expire", "replace"])
async def test_a_download_under_way_completes_after_the_archive_goes(export_dir, what):
    manager = _manager(export_dir, _server(), lifetime_seconds=0.2 if what == "expire" else 600)
    async with _client(manager) as client:
        await client.post(URL, json={"lookback_seconds": DAY})
        await _until_state(client, "ready")
        expected = (await client.get(f"{URL}/download")).content
        response = _CheckedOutFileResponse(manager.checkout())

        if what == "withdraw":
            await client.delete(URL)
        elif what == "expire":
            await _until_state(client, "none")
        else:
            await client.post(URL, json={"lookback_seconds": 3600})
        late = await client.get(f"{URL}/download")

        assert await _serve(response) == expected
        await _until_state(client, "ready" if what == "replace" else "none")

    assert late.status_code == 409
    assert [n for n in os.listdir(export_dir) if not n.endswith(".zip")] == []


async def test_an_archive_expires_back_to_none(export_dir):
    manager = _manager(export_dir, _server(), lifetime_seconds=0.2)
    async with _client(manager) as client:
        await client.post(URL, json={"lookback_seconds": DAY})
        ready = await _until_state(client, "ready")
        gone = await _until_state(client, "none")

    assert ready["download"]["expires_at"].endswith("Z")
    assert gone["state"] == "none"
    assert os.listdir(export_dir) == []


def test_startup_empties_the_directory_and_keeps_it_private(export_dir):
    export_dir.mkdir()
    (export_dir / "stale.zip").write_bytes(b"old")
    (export_dir / "stale.zip.part").write_bytes(b"old")
    os.chmod(export_dir, 0o755)

    _manager(export_dir, _server())

    assert os.listdir(export_dir) == []
    assert stat.S_IMODE(os.stat(export_dir).st_mode) == 0o700


async def test_an_unusable_directory_fails_each_export_but_not_startup(tmp_path):
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("")
    manager = _manager(not_a_directory / "log-export", _server())
    async with _client(manager) as client:
        await client.post(URL, json={"lookback_seconds": DAY})
        failed = await _until_state(client, "failed")

    assert failed["error"]["code"] == "export_failed"


async def test_closing_the_manager_stops_a_running_export(export_dir):
    manager = _manager(export_dir, _server(gate=threading.Event()))
    await manager.start(DAY, False)

    await asyncio.wait_for(manager.close(), 2)

    assert manager.status().state == "none"
    assert os.listdir(export_dir) == []


async def test_the_server_stays_responsive_while_an_export_is_prepared(export_dir):
    now = time.time()
    big = _server(*((now - 1000 + i / 1000, f"line {i} " + "q" * 400) for i in range(25000)))
    manager = _manager(export_dir, big)
    worst = 0.0
    async with _client(manager) as client:
        await client.post(URL, json={"lookback_seconds": DAY})
        while manager.status().state == "preparing":
            started = time.monotonic()
            await client.get(URL)
            await asyncio.sleep(0.005)
            worst = max(worst, time.monotonic() - started - 0.005)
        final = manager.status()

    assert final.state == "ready"
    assert any(w.code == "older_records_dropped" for w in final.warnings)
    assert worst < 0.25
