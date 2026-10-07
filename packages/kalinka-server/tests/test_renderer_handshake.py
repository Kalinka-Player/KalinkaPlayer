"""A renderer whose protocol the Core does not speak must stay reachable.

Hanging up on it would make it invisible to every client, leaving a shell on
its own machine as the only way to upgrade it — which is precisely the case
this handshake exists to survive.
"""

import asyncio
from unittest.mock import create_autospec

from fastapi import WebSocketDisconnect
from kalinka_plugin_sdk.direct_playback import OutputCapabilities

from kalinka_server.config_model import KalinkaConfig
from kalinka_server.renderer_config import RendererConfigService
from kalinka_server.renderer_proto import renderer_pb2 as pb
from kalinka_server.renderer_registry import RendererRegistry
from kalinka_server.renderer_sessions import SessionPool
from kalinka_server.renderer_upgrade import RendererUpgradeService
from kalinka_server.renderer_ws_handler import (
    PROTOCOL_VERSION,
    handle_renderer_connection,
)


class FakeWebSocket:
    """Feeds queued frames to the handler and records what it sends back."""

    def __init__(self, incoming: list[pb.Envelope]):
        self._incoming = list(incoming)
        self.sent: list[pb.Envelope] = []
        self.scope = {"server": ("192.168.50.85", 8000)}
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


def _hello(min_version: int, max_version: int) -> pb.Envelope:
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
    return env


def _volume_report() -> pb.Envelope:
    """Any message that only means something under an agreed protocol."""
    env = pb.Envelope()
    env.volume_changed.volume.current = 40
    return env


async def _run(
    incoming: list[pb.Envelope], pool=None
) -> tuple[FakeWebSocket, RendererRegistry]:
    registry = RendererRegistry(offline_timeout_s=30.0)
    if pool is None:
        pool = SessionPool(registry, "test-server-id")
    registry.set_on_removed(pool.handle_renderer_removed)
    websocket = FakeWebSocket(incoming)
    await asyncio.wait_for(
        handle_renderer_connection(
            websocket,
            KalinkaConfig(),
            registry,
            pool,
            RendererConfigService(registry),
            RendererUpgradeService(registry, lambda _: False),
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


def _capabilities_changed(dsd: bool) -> pb.Envelope:
    env = pb.Envelope()
    env.capabilities_changed.capabilities.dsd = dsd
    return env


async def test_what_hello_says_the_renderer_plays_is_kept():
    hello = _hello(PROTOCOL_VERSION, PROTOCOL_VERSION)
    hello.hello.capabilities.dsd = True
    _, registry = await _run([hello])
    assert registry.get("old-rid").capabilities == OutputCapabilities(dsd=True)


async def test_a_renderer_too_old_to_say_what_it_plays_is_not_guessed_at():
    _, registry = await _run([_hello(PROTOCOL_VERSION, PROTOCOL_VERSION)])
    assert registry.get("old-rid").capabilities is None


async def test_a_change_in_what_the_renderer_plays_is_believed():
    hello = _hello(PROTOCOL_VERSION, PROTOCOL_VERSION)
    hello.hello.capabilities.dsd = False
    _, registry = await _run([hello, _capabilities_changed(True)])
    assert registry.get("old-rid").capabilities == OutputCapabilities(dsd=True)


async def test_an_incompatible_renderer_is_not_believed_about_what_it_plays():
    hello = _hello(PROTOCOL_VERSION + 5, PROTOCOL_VERSION + 6)
    hello.hello.capabilities.dsd = True
    _, registry = await _run([hello, _capabilities_changed(True)])
    assert registry.get("old-rid").capabilities is None
