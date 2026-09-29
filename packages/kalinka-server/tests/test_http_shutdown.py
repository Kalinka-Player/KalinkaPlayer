import asyncio
from contextlib import asynccontextmanager
import logging
import socket
from unittest.mock import AsyncMock

from fastapi import FastAPI, Request
import pytest
import uvicorn

from kalinka_server.live_content import serve
from kalinka_server.http_server import KalinkaServer


@pytest.mark.parametrize("buffered", [False, True])
async def test_shutdown_releases_live_output_before_waiting_for_http(buffered, caplog):
    closed = asyncio.Event()

    class Capture:
        size = None

        async def open(self, start, end):
            return self

        async def read(self, count):
            nonlocal buffered
            if buffered:
                buffered = False
                return b"OggS"
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    capture = Capture()
    output = None
    connections_at_release = []

    async def stop_output():
        if output is not None:
            connections_at_release.append(len(server.server_state.connections))
            output.close()
            await output.wait_closed()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await stop_output()

    app = FastAPI(lifespan=lifespan)

    @app.get("/stream")
    async def stream(request: Request):
        return await serve(capture, "audio/ogg", request)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(
        app, log_config=None, access_log=False, timeout_graceful_shutdown=0.15,
    )
    server = KalinkaServer(config, before_shutdown=stop_output)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    caplog.set_level(logging.ERROR)
    try:
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(0.01)
            response, output = await asyncio.open_connection(*sock.getsockname())
            output.write(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await output.drain()
            assert b"200 OK" in await response.readuntil(b"\r\n\r\n")
            server.should_exit = True
            await task
        assert closed.is_set()
        assert connections_at_release[0] > 0
        assert "timeout graceful shutdown exceeded" not in caplog.text
        assert "Exception in ASGI application" not in caplog.text
    finally:
        server.should_exit = True
        if output is not None:
            output.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        sock.close()


@pytest.mark.parametrize("failure", ["timeout", "error", "force_exit"])
async def test_playback_teardown_cannot_block_http_shutdown(failure, monkeypatch, caplog):
    async def before_shutdown():
        if failure == "error":
            raise RuntimeError("private playback detail")
        await asyncio.Event().wait()

    release = AsyncMock(side_effect=before_shutdown)
    http_shutdown = AsyncMock()
    monkeypatch.setattr(uvicorn.Server, "shutdown", http_shutdown)
    server = KalinkaServer(
        uvicorn.Config(FastAPI(), timeout_graceful_shutdown=0.01),
        before_shutdown=release,
    )
    server.force_exit = failure == "force_exit"
    await asyncio.wait_for(server.shutdown(), 1)
    http_shutdown.assert_awaited_once_with(sockets=None)
    if failure == "force_exit":
        release.assert_not_awaited()
    else:
        release.assert_awaited_once()
        assert "Releasing playback before HTTP shutdown failed" in caplog.text
    assert "private playback detail" not in caplog.text
