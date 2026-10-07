"""Renderer WebSocket endpoint: one binary protobuf Envelope per message.

Receiver task pushes parsed envelopes into a bounded queue; processor task
drains it, so message handling never stalls the socket read.
"""

import asyncio
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect
from google.protobuf.message import DecodeError
from kalinka_plugin_sdk.direct_playback import OutputCapabilities

from .config_model import KalinkaConfig
from .renderer_proto import renderer_pb2 as pb
from .renderer_config import RendererConfigService
from .renderer_link import RendererLink
from .renderer_registry import RendererRegistry
from .renderer_sessions import CloseReason, SessionPool
from .renderer_state import StateChange
from .renderer_upgrade import RendererUpgradeService
from .server_identity import get_server_id
from .version import get_rest_api_version, get_version

logger = logging.getLogger(__name__.split(".")[-1])

# Renderer protocol version this Core speaks; independent of REST_API_VERSION.
PROTOCOL_VERSION = 2

INBOX_SIZE = 256

# The messages whose meaning is fixed for every protocol version, so they stay
# usable with a renderer this Core cannot otherwise talk to. Nothing may be
# removed from here: it is the channel an incompatible renderer is reached on.
_VERSION_FREE_PAYLOADS = frozenset({"hello", "goodbye", "upgrade_result"})

_CLOSE_REASON_TO_PB = {
    CloseReason.STALE: pb.SessionClose.REASON_STALE,
    CloseReason.CLOSED_BY_SERVER: pb.SessionClose.REASON_CLOSED_BY_SERVER,
    CloseReason.SHUTDOWN: pb.SessionClose.REASON_CLOSED_BY_SERVER,
}


def _kind_name(kind: int) -> str:
    return pb.RendererKind.Name(kind).removeprefix("RENDERER_KIND_").lower()


def _capabilities(capabilities: pb.Capabilities) -> OutputCapabilities:
    return OutputCapabilities(dsd=capabilities.dsd)


class RendererSession(RendererLink):
    """Send side of one renderer connection; held by the registry.

    Implements :class:`RendererLink`, which is all anyone outside this module
    sees of it."""

    def __init__(self, websocket: WebSocket):
        self._websocket = websocket
        self._out_id = 0
        self._send_lock = asyncio.Lock()

    def next_message_id(self) -> int:
        self._out_id += 1
        return self._out_id

    def _envelope(self, message_id: int | None = None) -> pb.Envelope:
        env = pb.Envelope()
        env.message_id = (
            message_id if message_id is not None else self.next_message_id()
        )
        return env

    async def _send(self, env: pb.Envelope) -> None:
        async with self._send_lock:
            await self._websocket.send_bytes(env.SerializeToString())

    async def send_welcome(self, config: KalinkaConfig) -> None:
        env = self._envelope()
        welcome = env.welcome
        welcome.protocol_version = PROTOCOL_VERSION
        welcome.server_id = get_server_id()
        welcome.server_name = config.server.service_name
        welcome.server_version = get_version()
        welcome.api_version = get_rest_api_version()
        welcome.server_time_unix_ms = int(time.time() * 1000)
        await self._send(env)

    async def send_session_open(
        self,
        session_id: str,
        force_fixed_output: bool = False,
    ) -> None:
        env = self._envelope()
        env.session_open.session_id = session_id
        # The renderer restores its configured mode when this session ends.
        env.session_open.force_fixed_output = force_fixed_output
        await self._send(env)

    async def send_command(self, session_id: str, command: pb.Command) -> None:
        env = self._envelope()
        env.session_id = session_id
        env.command.CopyFrom(command)
        await self._send(env)

    # Both take the id the caller reserved with next_message_id(), which the
    # renderer echoes in in_reply_to.
    async def send_config_request(self, message_id: int) -> None:
        env = self._envelope(message_id)
        env.config_request.SetInParent()
        await self._send(env)

    async def send_config_update(
        self, message_id: int, changes: dict[str, str]
    ) -> None:
        env = self._envelope(message_id)
        for path, value in changes.items():
            setting = env.config_update.settings.add()
            setting.path = path
            setting.value = str(value)
        await self._send(env)

    async def send_upgrade(self, message_id: int, target_version: str) -> None:
        env = self._envelope(message_id)
        env.upgrade.target_version = target_version
        await self._send(env)

    async def send_session_close(self, session_id: str, reason) -> None:
        env = self._envelope()
        env.session_close.session_id = session_id
        env.session_close.reason = _CLOSE_REASON_TO_PB.get(
            reason, pb.SessionClose.REASON_CLOSED_BY_SERVER
        )
        await self._send(env)

    async def send_goodbye(self, reason, detail: str) -> None:
        env = self._envelope()
        env.goodbye.reason = reason
        env.goodbye.detail = detail
        await self._send(env)

    async def replace(self) -> None:
        """Retire this session in favour of a newer connection."""
        try:
            await self.send_goodbye(
                pb.Goodbye.REASON_REPLACED,
                "another connection registered this renderer_id",
            )
            await self._websocket.close()
        except Exception:
            pass  # the old link may already be dead


async def handle_renderer_connection(
    websocket: WebSocket,
    config: KalinkaConfig,
    registry: RendererRegistry,
    sessions: SessionPool,
    configs: RendererConfigService,
    upgrades: RendererUpgradeService,
):
    await websocket.accept()
    session = RendererSession(websocket)
    inbox: asyncio.Queue[pb.Envelope] = asyncio.Queue(maxsize=INBOX_SIZE)

    registered_id: str | None = None
    registered_compatible: bool | None = None
    clean_goodbye = False
    renderer_desc = "unregistered renderer"

    async def receiver():
        while True:
            data = await websocket.receive_bytes()
            env = pb.Envelope()
            try:
                env.ParseFromString(data)
            except DecodeError:
                logger.warning(
                    "Dropping unparseable %d-byte message from %s",
                    len(data),
                    renderer_desc,
                )
                await session.send_goodbye(
                    pb.Goodbye.REASON_MALFORMED, "unparseable envelope"
                )
                return
            await inbox.put(env)

    async def processor():
        nonlocal registered_id, registered_compatible, clean_goodbye
        nonlocal renderer_desc
        while True:
            env = await inbox.get()
            payload = env.WhichOneof("payload")
            if (
                registered_compatible is False
                and payload not in _VERSION_FREE_PAYLOADS
            ):
                # Outside that set, nothing means the same on both sides.
                logger.debug(
                    "Ignoring %r from incompatible renderer %s",
                    payload,
                    renderer_desc,
                )
                continue
            if payload == "hello":
                hello = env.hello
                if registered_id is not None:
                    logger.warning(
                        "Renderer %s sent a second Hello; closing", renderer_desc
                    )
                    await session.send_goodbye(
                        pb.Goodbye.REASON_MALFORMED, "Hello already received"
                    )
                    return
                renderer_desc = (
                    f"'{hello.friendly_name}' (id={hello.renderer_id})"
                )
                versions = hello.protocol_versions
                compatible = versions.min <= PROTOCOL_VERSION <= versions.max
                if not compatible:
                    # Hanging up would hide it from every client, leaving a
                    # shell on its own machine as the only way to upgrade it.
                    logger.warning(
                        "Renderer %s speaks protocol %d-%d, server speaks %d; "
                        "keeping it connected as incompatible",
                        renderer_desc,
                        versions.min,
                        versions.max,
                        PROTOCOL_VERSION,
                    )
                # The accepted socket's local address — what the renderer dialed.
                addr = websocket.scope.get("server")
                registry.register(
                    renderer_id=hello.renderer_id,
                    instance_id=hello.instance_id,
                    friendly_name=hello.friendly_name,
                    software_version=hello.software_version,
                    kind=_kind_name(hello.kind),
                    platform={
                        "os": hello.platform.os,
                        "os_version": hello.platform.os_version,
                        "arch": hello.platform.arch,
                        "hostname": hello.platform.hostname,
                        "audio_backend": hello.platform.audio_backend,
                    },
                    session=session,
                    upgrade_supported=hello.upgrade_supported,
                    server_addr=(addr[0], addr[1]) if addr and addr[1] else None,
                    compatible=compatible,
                    capabilities=(
                        _capabilities(hello.capabilities)
                        if compatible and hello.HasField("capabilities")
                        else None
                    ),
                )
                registered_id = hello.renderer_id
                registered_compatible = compatible
                # Sent either way: the version it names is what tells an
                # incompatible renderer what it has to become.
                await session.send_welcome(config)
                if not compatible:
                    continue
                await sessions.reconcile(
                    renderer_id=hello.renderer_id,
                    reported_session_id=hello.active_session_id,
                    reported_owner_server_id=hello.session_owner_server_id,
                    ws_session=session,
                )
            elif payload == "session_open_result":
                result = env.session_open_result
                sessions.handle_open_result(
                    registered_id or "",
                    session_id=result.session_id,
                    accepted=result.accepted,
                    busy=result.error == pb.SessionOpenResult.ERROR_BUSY,
                    detail=result.detail,
                    owner_server_id=result.owner_server_id,
                )
            elif payload == "session_closed":
                closed = env.session_closed
                sessions.handle_closed(
                    registered_id or "",
                    session_id=closed.session_id,
                    renderer_error=closed.reason
                    == pb.SessionClosed.REASON_RENDERER_ERROR,
                    detail=closed.detail,
                )
            elif payload == "upgrade_result":
                upgrades.handle_reply(
                    registered_id or "", session, env.in_reply_to, env.upgrade_result
                )
            elif payload in ("config_snapshot", "config_result"):
                configs.handle_reply(
                    registered_id or "",
                    session,
                    env.in_reply_to,
                    getattr(env, payload),
                )
            elif payload == "capabilities_changed":
                registry.update_capabilities(
                    registered_id or "",
                    session,
                    _capabilities(env.capabilities_changed.capabilities),
                )
            elif payload == "command_rejected":
                sessions.handle_rejection(
                    registered_id or "", rejection=env.command_rejected
                )
            elif (change := StateChange.for_payload(payload or "")) is not None:
                sessions.handle_state(
                    registered_id or "",
                    session_id=env.session_id,
                    change=change,
                    message=getattr(env, payload),
                )
            elif payload == "goodbye":
                clean_goodbye = True
                logger.info(
                    "Renderer %s said goodbye (reason %s)",
                    renderer_desc,
                    pb.Goodbye.Reason.Name(env.goodbye.reason),
                )
            else:
                logger.debug(
                    "Ignoring renderer message with payload %r", payload
                )

    receive_task = asyncio.create_task(receiver())
    process_task = asyncio.create_task(processor())
    try:
        await asyncio.wait(
            {receive_task, process_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        raise
    finally:
        receive_task.cancel()
        process_task.cancel()
        await asyncio.gather(receive_task, process_task, return_exceptions=True)
        for task in (receive_task, process_task):
            exc = task.exception() if task.done() and not task.cancelled() else None
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.error("Renderer %s connection error: %s", renderer_desc, exc)
        if registered_id is not None:
            registry.disconnect(registered_id, session, clean=clean_goodbye)
            sessions.suspend(registered_id, session)
            configs.handle_disconnect(registered_id, session)
            upgrades.handle_disconnect(registered_id, session)
        else:
            logger.info("Renderer connection closed before registration")
        try:
            await websocket.close()
        except Exception:
            pass
