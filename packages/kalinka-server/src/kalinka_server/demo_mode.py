"""A public demo server: anyone may listen and queue, nobody may change it.

Every write outside the queue and playback plane is answered 403 before it
reaches a route, and no renderer may register from outside, so the server stays
as it was deployed whoever connects to it. What is let through is bounded: each
visitor's changes are rate-limited and the shared queue has a fixed size.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config_model import KalinkaConfig
from .presentation_schema import Banner, Severity
from .queue_limit import QueueLimit

REFUSAL_CODE = "demo_read_only"
REFUSAL = {
    "code": REFUSAL_CODE,
    "message": (
        "This is a read-only demo server: playback and the queue work, but "
        "settings, favourites, playlists and collections cannot be changed here."
    ),
}

QUEUE_LIMIT = 100
QUEUE_FULL = {
    "code": "demo_queue_full",
    "message": (
        f"The demo queue holds up to {QUEUE_LIMIT} tracks. Remove some, or "
        "clear the queue, to add more."
    ),
}
#: The shared queue's limit, well under an ordinary server's.
DEMO_QUEUE = QueueLimit(QUEUE_LIMIT, QUEUE_FULL)

THROTTLED = {
    "code": "demo_rate_limited",
    "message": "Too many changes at once. Wait a moment and try again.",
}
_WRITE_BURST = 20
_WRITE_INTERVAL_S = 3.0
_MAX_TRACKED_CLIENTS = 4096

_BANNER = Banner(
    title="Demo server",
    text=(
        "Playback is simulated, and the queue is shared with everyone trying "
        "the demo. Settings can be explored but not saved."
    ),
    severity=Severity.INFO,
)

_READS = frozenset({"GET", "HEAD", "OPTIONS"})
_WRITABLE_PATHS = frozenset({"/device/set_volume"})
_RENDERER_SOCKET = "/renderer/ws"
_POLICY_VIOLATION = 1008


def is_write_allowed(method: str, path: str) -> bool:
    """Whether a demo server serves an HTTP request: reads, the queue and the
    volume.

    The settings dry run is refused too. It saves nothing, but a plugin's
    check may reach out to whatever a visitor typed, such as a network share.
    """
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


class ClientRate:
    """A token bucket per client: ``burst`` requests at once, then one every
    ``interval_s``.

    Only clients still owed tokens are worth remembering, so a full bucket is
    forgotten once the table grows past a bound.
    """

    def __init__(
        self,
        burst: int = _WRITE_BURST,
        interval_s: float = _WRITE_INTERVAL_S,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._burst = burst
        self._interval_s = interval_s
        self._now = now
        self._buckets: dict[str, tuple[float, float]] = {}

    def take(self, client: str) -> float:
        """Spend one of ``client``'s tokens: 0 when it may go ahead, otherwise
        the seconds until it may."""
        now = self._now()
        tokens = self._tokens(client, now)
        if tokens < 1:
            return (1 - tokens) * self._interval_s
        self._buckets[client] = (tokens - 1, now)
        if len(self._buckets) > _MAX_TRACKED_CLIENTS:
            self._forget_full(now)
        return 0.0

    def _tokens(self, client: str, now: float) -> float:
        held = self._buckets.get(client)
        if held is None:
            return float(self._burst)
        tokens, at = held
        return min(self._burst, tokens + (now - at) / self._interval_s)

    def _forget_full(self, now: float) -> None:
        for client in list(self._buckets):
            if self._tokens(client, now) >= self._burst:
                del self._buckets[client]


class DemoWriteThrottle:
    """ASGI middleware rate-limiting each visitor's changes on a demo server.

    Reads pass untouched. A change over the limit is answered
    ``429 {"detail": THROTTLED}`` with ``Retry-After``. Visitors are told apart
    by address, so behind a reverse proxy the server must trust its forwarded
    header (uvicorn's ``FORWARDED_ALLOW_IPS``).

    @param enabled Asked on every request, as for DemoReadOnlyGate.
    """

    def __init__(
        self,
        app: ASGIApp,
        enabled: Callable[[], bool],
        rate: ClientRate | None = None,
    ) -> None:
        self._app = app
        self._enabled = enabled
        self._rate = rate if rate is not None else ClientRate()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"].upper() not in _READS
            and self._enabled()
        ):
            client = scope.get("client")
            wait_s = self._rate.take(client[0] if client else "")
            if wait_s > 0:
                refusal = JSONResponse(
                    {"detail": THROTTLED},
                    status_code=429,
                    headers={"Retry-After": str(math.ceil(wait_s))},
                )
                await refusal(scope, receive, send)
                return
        await self._app(scope, receive, send)
