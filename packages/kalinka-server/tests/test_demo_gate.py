"""The demo gate on its own: which requests pass, and how the rest are told."""

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kalinka_server.demo_mode import REFUSAL, DemoReadOnlyGate, is_write_allowed


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/server/config"),
        ("HEAD", "/content/jamendo/1"),
        ("OPTIONS", "/collections"),
        ("POST", "/queue/add"),
        ("POST", "/queue/replace"),
        ("PUT", "/queue/play"),
        ("PUT", "/queue/current_track/seek"),
        ("PUT", "/device/set_volume"),
    ],
)
def test_listening_and_queueing_pass(method, path):
    assert is_write_allowed(method, path)


@pytest.mark.parametrize(
    "method, path",
    [
        ("PUT", "/server/config"),
        ("POST", "/server/config/validate"),
        ("PUT", "/server/restart"),
        ("PUT", "/server/upgrade"),
        ("POST", "/server/test_tone"),
        ("POST", "/server/logs/export"),
        ("POST", "/collections"),
        ("PATCH", "/collections/c1"),
        ("PUT", "/favorite/add/t1"),
        ("DELETE", "/favorite/remove/t1"),
        ("POST", "/playlist/create"),
        ("PUT", "/renderer/active"),
        ("PUT", "/renderer/r1/config"),
        ("POST", "/renderer/r1/upgrade"),
        ("PUT", "/queuex"),
    ],
)
def test_every_other_change_is_refused(method, path):
    assert not is_write_allowed(method, path)


def _client(enabled: dict) -> TestClient:
    app = FastAPI()
    app.add_middleware(DemoReadOnlyGate, enabled=lambda: enabled["on"])

    @app.get("/server/config")
    def read():
        return {"ok": True}

    @app.put("/server/config")
    def write():
        return {"ok": True}

    async def echo(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    app.add_api_websocket_route("/renderer/ws", echo)
    app.add_api_websocket_route("/queue/ws", echo)
    return TestClient(app)


def test_a_refusal_carries_its_code():
    client = _client({"on": True})
    response = client.put("/server/config")
    assert response.status_code == 403
    assert response.json() == {"detail": REFUSAL}
    assert client.get("/server/config").status_code == 200


def test_no_renderer_may_dial_in():
    client = _client({"on": True})
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/renderer/ws") as socket,
    ):
        socket.receive_text()
    with client.websocket_connect("/queue/ws") as socket:
        assert socket.receive_text() == "hello"


def test_the_gate_follows_the_live_flag():
    enabled = {"on": False}
    client = _client(enabled)
    assert client.put("/server/config").status_code == 200
    with client.websocket_connect("/renderer/ws") as socket:
        assert socket.receive_text() == "hello"
    enabled["on"] = True
    assert client.put("/server/config").status_code == 403
