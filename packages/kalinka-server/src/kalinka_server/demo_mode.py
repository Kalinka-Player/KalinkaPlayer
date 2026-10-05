"""A public demo server: anyone may listen and queue, nobody may change it.

Every write outside the queue and playback plane is answered 403 before it
reaches a route, and no renderer may register from outside, so the server stays
as it was deployed whoever connects to it.
"""

from __future__ import annotations

from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config_model import KalinkaConfig
from .presentation_schema import Banner, Severity

REFUSAL_CODE = "demo_read_only"
REFUSAL = {
    "code": REFUSAL_CODE,
    "message": (
        "This is a read-only demo server: playback and the queue work, but "
        "settings, favourites, playlists and collections cannot be changed here."
    ),
}

_BANNER = Banner(
    title="Demo server",
    text=(
        "Playback is simulated, and the queue is shared with everyone trying "
        "the demo. Settings can be explored but not saved."
    ),
    severity=Severity.INFO,
)

_READS = frozenset({"GET", "HEAD", "OPTIONS"})
_WRITABLE_PATHS = frozenset({"/device/set_volume", "/server/config/validate"})
_RENDERER_SOCKET = "/renderer/ws"
_POLICY_VIOLATION = 1008


def is_write_allowed(method: str, path: str) -> bool:
    """Whether a demo server serves an HTTP request: reads, the queue, the
    volume, and the settings dry run, which changes nothing."""
    if method.upper() in _READS:
        return True
    return path.startswith("/queue/") or path in _WRITABLE_PATHS


def page_banners(config: KalinkaConfig) -> list[Banner]:
    """What the General settings page tells a visitor about this server."""
    return [_BANNER] if config.server.demo_mode else []


class DemoReadOnlyGate:
    """ASGI middleware refusing what a demo server must not do.

    A refused request is answered ``403 {"detail": REFUSAL}``, the shape the
    plugin routes refuse with, so a client reads one ``code`` either way. A
    renderer dialing in is turned away before its socket is accepted.

    @param enabled Asked on every request, so the gate follows the live
        configuration rather than the one it was built with.
    """

    def __init__(self, app: ASGIApp, enabled: Callable[[], bool]) -> None:
        self._app = app
        self._enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and self._enabled()
            and not is_write_allowed(scope["method"], scope["path"])
        ):
            refusal = JSONResponse({"detail": REFUSAL}, status_code=403)
            await refusal(scope, receive, send)
            return
        if (
            scope["type"] == "websocket"
            and scope["path"] == _RENDERER_SOCKET
            and self._enabled()
        ):
            await send({"type": "websocket.close", "code": _POLICY_VIOLATION})
            return
        await self._app(scope, receive, send)
