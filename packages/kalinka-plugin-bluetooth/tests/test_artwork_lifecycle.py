"""Playback-independent OBEX recovery and cancellation-safe resource teardown."""

import asyncio
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from dbus_fast import Message
import pytest

import kalinka_plugin_bluetooth.artwork as artwork
import test_artwork as artwork_tests
from test_artwork import connect_artwork, metadata, until

receiver = artwork_tests.receiver
endpoint = artwork_tests.endpoint


@pytest.mark.parametrize("loss", ["obexd", "bus", "session"])
async def test_missing_handle_does_not_prevent_reconnection(
    receiver, monkeypatch, loss
):
    service, backend, first = receiver
    monkeypatch.setattr(artwork, "RETRY_DELAY", 0.01)
    monkeypatch.setattr(artwork, "HEALTH_CHECK_INTERVAL", 0.01)
    await connect_artwork(service, backend, first)
    # Give the worker a fresh snapshot, then keep the metadata unchanged and
    # without ImgHandle. Recovery cannot depend on a fetch or another signal.
    await service.update_metadata(backend.metadata(service.endpoint))
    await until(lambda: first.check_alive.call_count > 0)
    health = artwork.ObexClient()
    health.bus = SimpleNamespace(connected=True)
    health.obexd = SimpleNamespace(returncode=None)
    health.session = "/session"
    first.check_alive.side_effect = health.check_alive
    if loss == "obexd":
        health.obexd.returncode = 1
    elif loss == "bus":
        health.bus.connected = False
    else:
        health._message(
            Message.new_signal(
                "/",
                artwork.OBJECTS,
                "InterfacesRemoved",
                "oas",
                ["/session", [artwork.SESSION]],
            )
        )
    second = SimpleNamespace(
        connect=AsyncMock(),
        check_alive=Mock(),
        close=AsyncMock(),
        fetch=AsyncMock(return_value=(artwork_tests.PNG, "image/png")),
    )
    service.artwork.client_factory = lambda: second
    await until(lambda: second.connect.await_count == 1)
    first.close.assert_awaited_once()
    first.fetch.assert_not_awaited()
    second.fetch.assert_not_awaited()
    await metadata(service, backend, Title="Recovered", ImgHandle="0000002")
    await until(lambda: service.artwork_url is not None)
    assert service.track().title == "Recovered"
    second.fetch.assert_awaited_once_with("0000002")


async def stubborn_child():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); signal.pause()",
        stdout=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(process.stdout.readline(), 2)
    return process


async def test_repeated_shutdown_cancellation_still_terminates_and_reaps_children(
    receiver, monkeypatch
):
    service, backend, _ = receiver
    monkeypatch.setattr(artwork, "CLEANUP_TIMEOUT", 0.1)
    monkeypatch.setattr(artwork, "PROCESS_EXIT_TIMEOUT", 0.05)
    client = artwork.ObexClient()
    client.connect = AsyncMock()
    client.fetch = AsyncMock(wraps=client.fetch)
    client.directory = tempfile.TemporaryDirectory(prefix="kalinka-obex-test-")
    directory = Path(client.directory.name)
    client.session = "/session"
    client.bus = SimpleNamespace(
        connected=True,
        disconnect=Mock(),
        wait_for_disconnect=AsyncMock(),
    )
    bus = client.bus
    cancel_entered, remove_entered = asyncio.Event(), asyncio.Event()
    transfer_started = asyncio.Event()
    closing = again = None
    children = []
    try:
        # Real children ignore TERM, exercising the bounded KILL/reap fallback.
        for _ in range(2):
            children.append(await stubborn_child())
        client.obexd, client.daemon = children

        async def call(path, interface, member, signature="", body=None):
            if member == "Properties":
                return [[]]  # Thumbnail-only sender.
            if member == "GetThumbnail":
                Path(body[0]).write_bytes(artwork_tests.JPEG)
                transfer_started.set()
                return ["/transfer", {"Status": "active"}]
            if member == "Cancel":
                cancel_entered.set()
            elif member == "RemoveSession":
                remove_entered.set()
            else:
                pytest.fail(f"Unexpected method {member}")
            await asyncio.Event().wait()  # Unresponsive OBEX cleanup methods.

        client.call = AsyncMock(side_effect=call)
        service.artwork.client_factory = lambda: client
        await connect_artwork(service, backend, client)
        await metadata(service, backend, Title="First", ImgHandle="0000001")
        await asyncio.wait_for(transfer_started.wait(), 1)
        worker = service.artwork.task
        closing = asyncio.create_task(service.artwork.close())
        await asyncio.wait_for(cancel_entered.wait(), 1)
        closing.cancel()
        await asyncio.wait_for(remove_entered.wait(), 1)
        closing.cancel()  # Cancellation again, during session removal.
        worker.cancel()  # Also exercise ObexClient.close's own cancellation guard.
        again = asyncio.create_task(service.artwork.close())
        results = await asyncio.wait_for(
            asyncio.gather(closing, again, return_exceptions=True), 2
        )
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
        assert worker.done() and service.artwork.task is None
        assert client.bus is None and client.obexd is None and client.daemon is None
        assert all(child.returncode == -signal.SIGKILL for child in children)
        bus.disconnect.assert_called_once()
        assert not directory.exists()
    finally:
        # Keep failed regressions from leaving the test's real children behind.
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.wait()
        for task in (closing, again):
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await client.close()
