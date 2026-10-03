"""A renderer whose protocol the Core does not speak must stay reachable.

Hanging up on it would make it invisible to every client, leaving a shell on
its own machine as the only way to upgrade it — which is precisely the case
this handshake exists to survive.
"""

import asyncio
import socket
from unittest.mock import create_autospec

import pytest
from fastapi import WebSocketDisconnect

from kalinka_server.config_model import KalinkaConfig
from kalinka_server.renderer_config import RendererConfigService
from kalinka_server.renderer_proto import renderer_pb2 as pb
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool
from kalinka_server.renderer_upgrade import RendererUpgradeService
from kalinka_server.renderer_ws_handler import (
    PROTOCOL_VERSION,
    handle_renderer_connection,
    runs_here,
)

SERVER_ADDR = ("192.168.50.85", 8000)


class FakeWebSocket:
    """Feeds queued frames to the handler and records what it sends back."""

    def __init__(
        self, incoming: list[pb.Envelope], client=("192.168.50.20", 51234)
    ):
        self._incoming = list(incoming)
        self.sent: list[pb.Envelope] = []
        self.scope = {"server": SERVER_ADDR, "client": client}
        self.accepted = False
        self.closed = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_bytes(self) -> bytes:
        if not self._incoming:
            raise WebSocketDisconnect(1000)
        return self._incoming.pop(0).SerializeToString()

    async def send_bytes(self, data: bytes) -> None:
        env = pb.Envelope()
        env.ParseFromString(data)
        self.sent.append(env)

    async def close(self) -> None:
        self.closed = True


def _hello(
    min_version: int = PROTOCOL_VERSION,
    max_version: int = PROTOCOL_VERSION,
    hostname: str = "attic",
) -> pb.Envelope:
    env = pb.Envelope()
    hello = env.hello
    hello.protocol_versions.min = min_version
    hello.protocol_versions.max = max_version
    hello.renderer_id = "old-rid"
    hello.instance_id = "inst-1"
    hello.friendly_name = "Old Renderer"
    hello.software_version = "0.3.0"
    hello.kind = pb.RENDERER_KIND_NATIVE
    hello.platform.os = "linux"
    hello.platform.hostname = hostname
    return env


def _volume_report() -> pb.Envelope:
    """Any message that only means something under an agreed protocol."""
    env = pb.Envelope()
    env.volume_changed.volume.current = 40
    return env


async def _run(
    incoming: list[pb.Envelope], pool=None, **connection
) -> tuple[FakeWebSocket, RendererRegistry]:
    registry = RendererRegistry(offline_timeout_s=30.0)
    if pool is None:
        pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    websocket = FakeWebSocket(incoming, **connection)

    async def no_playback(_renderer_id: str) -> None:
        pass

    await asyncio.wait_for(
        handle_renderer_connection(
            websocket,
            KalinkaConfig(),
            registry,
            pool,
            RendererConfigService(registry),
            RendererUpgradeService(registry, lambda _: False, no_playback),
        ),
        timeout=5,
    )
    return websocket, registry


async def test_incompatible_renderer_registers_and_is_told_our_version():
    websocket, registry = await _run([_hello(PROTOCOL_VERSION + 5, PROTOCOL_VERSION + 6)])

    (entry,) = registry.list()
    assert entry["renderer_id"] == "old-rid"
    assert entry["compatible"] is False
    assert entry["software_version"] == "0.3.0"

    # The Welcome is what names the version it has to become; a Goodbye would
    # have ended the only conversation available with it.
    welcomes = [e for e in websocket.sent if e.WhichOneof("payload") == "welcome"]
    assert [w.welcome.protocol_version for w in welcomes] == [PROTOCOL_VERSION]
    assert not [e for e in websocket.sent if e.WhichOneof("payload") == "goodbye"]


async def test_a_compatible_renderer_still_registers_as_compatible():
    _, registry = await _run([_hello(PROTOCOL_VERSION, PROTOCOL_VERSION)])
    (entry,) = registry.list()
    assert entry["compatible"] is True


async def test_messages_from_an_incompatible_renderer_are_not_acted_on():
    """Their meaning is not agreed, so they are dropped rather than believed."""
    pool = create_autospec(SessionPool, instance=True)
    await _run(
        [_hello(PROTOCOL_VERSION + 5, PROTOCOL_VERSION + 6), _volume_report()],
        pool=pool,
    )
    pool.reconcile.assert_not_called()
    pool.handle_state.assert_not_called()


async def test_a_compatible_renderer_is_reconciled_and_its_state_believed():
    """The control: the same two messages do reach the pool when the protocol
    is agreed, so the test above is measuring the version gate."""
    pool = create_autospec(SessionPool, instance=True)
    await _run(
        [_hello(PROTOCOL_VERSION, PROTOCOL_VERSION), _volume_report()], pool=pool
    )
    pool.reconcile.assert_awaited_once()
    pool.handle_state.assert_called_once()


class TestThisMachinesRenderer:
    """The server's own upgrade covers the renderer beside it, so the registry
    has to know which one that is — from the name it reports and an address
    only this machine connects from, together."""

    @pytest.fixture(autouse=True)
    def _host(self, monkeypatch):
        monkeypatch.setattr(socket, "gethostname", lambda: "kalinka")

    async def test_one_reaching_our_lan_address_from_it_is_local(self):
        """The packaged renderer finds the server by mDNS, so it dials the LAN
        address — and, running here, connects from that same address."""
        _, registry = await _run(
            [_hello(hostname="kalinka")], client=(SERVER_ADDR[0], 40000)
        )
        (entry,) = registry.list()
        assert entry["local"] is True

    async def test_a_namesake_behind_a_proxy_here_is_not(self):
        """Through a reverse proxy on this machine every renderer connects
        from loopback, the one on another default-named image included."""
        _, registry = await _run(
            [_hello(hostname="kalinka")], client=("127.0.0.1", 40000)
        )
        assert registry.list()[0]["local"] is False

    async def test_a_namesake_on_another_machine_is_not(self):
        """Every Kalinka image left at its default hostname is 'kalinka'."""
        _, registry = await _run(
            [_hello(hostname="kalinka")], client=("192.168.50.20", 40000)
        )
        assert registry.list()[0]["local"] is False

    async def test_a_renderer_elsewhere_is_not(self):
        _, registry = await _run([_hello(hostname="attic")])
        assert registry.list()[0]["local"] is False

    async def test_an_incompatible_one_is_still_recognised(self):
        """Exactly the one the installer is about to bring forward."""
        _, registry = await _run(
            [_hello(PROTOCOL_VERSION + 5, PROTOCOL_VERSION + 6, "kalinka")],
            client=(SERVER_ADDR[0], 40000),
        )
        assert registry.list()[0]["local"] is True


@pytest.mark.parametrize(
    "hostname, peer, dialed, local",
    [
        ("kalinka", (SERVER_ADDR[0], 1), SERVER_ADDR, True),
        ("kalinka", ("192.168.50.20", 1), SERVER_ADDR, False),
        # Loopback, as every peer of a reverse proxy on this machine is.
        ("kalinka", ("127.0.0.1", 1), ("127.0.0.1", 8000), False),
        ("kalinka", ("::1", 1), SERVER_ADDR, False),
        ("kalinka", ("::ffff:127.0.0.1", 1), SERVER_ADDR, False),
        # A renderer in a container of its own on this machine's address.
        ("container", (SERVER_ADDR[0], 1), SERVER_ADDR, False),
        # A renderer that does not say what it is called.
        ("", (SERVER_ADDR[0], 1), SERVER_ADDR, False),
        ("kalinka", None, SERVER_ADDR, False),
        ("kalinka", (SERVER_ADDR[0], 1), None, False),
        ("kalinka", ("testclient", 1), SERVER_ADDR, False),
    ],
)
def test_runs_here(monkeypatch, hostname, peer, dialed, local):
    monkeypatch.setattr(socket, "gethostname", lambda: "kalinka")
    assert runs_here(hostname, peer, dialed) is local
