"""Release direct playback while renderer control connections are still open."""

import asyncio
from collections.abc import Awaitable, Callable
import logging

import uvicorn

logger = logging.getLogger(__name__)


class KalinkaServer(uvicorn.Server):
    def __init__(
        self, config: uvicorn.Config, *, before_shutdown: Callable[[], Awaitable[None]]
    ):
        super().__init__(config)
        self.before_shutdown = before_shutdown

    async def shutdown(self, sockets=None):
        # ASGI lifespan teardown runs only after uvicorn drains requests. A
        # live audio request cannot finish until its renderer is told to stop,
        # which also needs the renderer's control connection to remain open.
        if not self.force_exit:
            timeout = self.config.timeout_graceful_shutdown
            try:
                await asyncio.wait_for(
                    self.before_shutdown(), timeout=5 if timeout is None else timeout
                )
            except Exception as exc:
                logger.warning(
                    "Releasing playback before HTTP shutdown failed (%s)",
                    type(exc).__name__,
                )
        await super().shutdown(sockets=sockets)
