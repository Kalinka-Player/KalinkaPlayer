import asyncio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from kalinka_plugin_sdk.inputmodule import ContentInfo
from kalinka_server.content_route import register_content_route
from kalinka_server import live_content
from kalinka_server.live_content import serve
from starlette.requests import Request


class Capture:
    size = None

    def __init__(self):
        self.reads = 0
        self.closed = False
        self.resolves = 0

    async def get_content_info(self, asset_id):
        self.resolves += 1
        return ContentInfo(mime_type='audio/ogg', live=self)

    async def open(self, start, end):
        assert start == 0 and end is None
        return self

    async def read(self, count):
        self.reads += 1
        await asyncio.sleep(.01)
        return b'OggS' if self.reads <= 2 else b''

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("initial_range", [None, "bytes=0-", "bytes=0-383999", "bytes=0-0"])
async def test_content_route_hands_off_live_object_outside_module_call(initial_range):
    capture = Capture()
    app = FastAPI()
    register_content_route(app, lambda name: capture)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        headers = {'Range': initial_range} if initial_range is not None else {}
        response = await client.get('/content/spotify/generation', headers=headers)
    assert response.status_code == 200
    assert response.content == b'OggSOggS'
    assert 'content-length' not in response.headers
    assert 'content-range' not in response.headers
    assert response.headers['accept-ranges'] == 'none'
    assert capture.resolves == 1 and capture.closed


@pytest.mark.parametrize("asgi_version", ["2.0", "2.4"])
async def test_disconnect_closes_a_reader_waiting_for_more_bytes(asgi_version):
    capture = Capture()

    async def read(count):
        await asyncio.Event().wait()

    capture.read = read
    response = await serve(capture, 'audio/ogg', Request({
        'type': 'http', 'method': 'GET', 'headers': [],
    }))
    started = asyncio.Event()

    async def send(message):
        started.set()

    async def receive():
        await started.wait()
        return {'type': 'http.disconnect'}

    await asyncio.wait_for(response({
        'type': 'http', 'asgi': {'spec_version': asgi_version},
    }, receive, send), .2)
    assert capture.closed


@pytest.mark.parametrize("failure_at", ["reader", "socket"])
async def test_reader_faults_propagate_but_disconnected_sockets_do_not(failure_at):
    capture = Capture()

    async def read(count):
        if failure_at == 'reader':
            raise OSError('broken source')
        return b'audio'

    capture.read = read
    response = await serve(capture, 'audio/ogg', Request({
        'type': 'http', 'method': 'GET', 'headers': [],
    }))

    async def send(message):
        if failure_at == 'socket' and message['type'] == 'http.response.body':
            raise OSError('closed socket')

    async def receive():
        await asyncio.Event().wait()

    request = response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    if failure_at == 'reader':
        with pytest.raises(OSError, match='broken source'):
            await asyncio.wait_for(request, .2)
    else:
        await asyncio.wait_for(request, .2)
    assert capture.closed


class Finished:
    size = 8

    def __init__(self):
        self.opened = None
        self.closed = False

    async def get_content_info(self, asset_id):
        return ContentInfo(mime_type='audio/ogg', live=self)

    async def open(self, start, end):
        self.opened = (start, end)
        self.remaining = b'OggSOggS'[start:end]
        return self

    async def read(self, count):
        data, self.remaining = self.remaining[:count], self.remaining[count:]
        return data

    async def aclose(self):
        self.closed = True


async def test_a_finished_resource_answers_ranges_like_any_other_content():
    capture = Finished()
    app = FastAPI()
    register_content_route(app, lambda name: capture)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        ranged = await client.get('/content/spotify/g', headers={'Range': 'bytes=4-'})
        head = await client.head('/content/spotify/g')
        beyond = await client.get('/content/spotify/g', headers={'Range': 'bytes=8-'})
    assert ranged.status_code == 206 and ranged.content == b'OggS'
    assert ranged.headers['content-range'] == 'bytes 4-7/8'
    assert ranged.headers['accept-ranges'] == 'bytes'
    assert ranged.headers['cache-control'] == 'no-store'
    assert capture.opened == (4, 8) and capture.closed
    assert head.status_code == 200 and head.headers['content-length'] == '8'
    assert beyond.status_code == 416 and beyond.headers['content-range'] == 'bytes */8'


async def test_a_producer_that_goes_quiet_is_abandoned(monkeypatch):
    monkeypatch.setattr(live_content, 'IDLE_LIMIT_S', .05)
    capture = Capture()

    async def read(count):
        await asyncio.Event().wait()

    capture.read = read
    response = await serve(capture, 'audio/ogg', Request({
        'type': 'http', 'method': 'GET', 'headers': [],
    }))

    async def send(message):
        pass

    async def receive():
        await asyncio.Event().wait()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send), 1
        )
    assert capture.closed


async def test_a_producer_that_never_opens_is_refused(monkeypatch):
    monkeypatch.setattr(live_content, 'IDLE_LIMIT_S', .05)
    capture = Capture()

    async def open(start, end):
        await asyncio.Event().wait()

    capture.open = open
    response = await serve(capture, 'audio/ogg', Request({
        'type': 'http', 'method': 'GET', 'headers': [],
    }))
    assert response.status_code == 504
