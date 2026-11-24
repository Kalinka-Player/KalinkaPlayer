"""Requests written to a renderer connection, waiting for the reply.

Two planes work this way — settings and upgrades — and both reach a renderer
that may reconnect underneath them, so a wait is keyed by the link it went out
on as well as by the message id it reserved. A socket that has been replaced
then neither answers nor fails what is riding on its replacement.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .renderer_link import RendererLink
from .renderer_registry import RendererUnavailable

logger = logging.getLogger(__name__.split(".")[-1])


class PendingReplies:
    """What one plane has out on renderer links, and who is waiting for it.

    @param what Names this plane's traffic in log lines: "config", "upgrade".
    """

    def __init__(self, what: str, timeout_s: float):
        self._what = what
        self._timeout_s = timeout_s
        self._pending: dict[tuple[RendererLink, int], asyncio.Future] = {}

    def waiting_on(self, link: RendererLink) -> bool:
        """Whether a request of this plane's is already out on ``link``."""
        return any(sent_on is link for sent_on, _ in self._pending)

    async def request(
        self,
        renderer_id: str,
        link: RendererLink,
        send: Callable[[int], Awaitable[None]],
        timeout_s: float | None = None,
    ) -> Any:
        """Reserve a message id, hand it to ``send``, and await the reply."""
        # Reserved before the write: the answer must never be able to arrive
        # before there is something waiting for it.
        message_id = link.next_message_id()
        key = (link, message_id)
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            await send(message_id)
            return await asyncio.wait_for(
                future, self._timeout_s if timeout_s is None else timeout_s
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Renderer %s did not answer the %s request",
                renderer_id,
                self._what,
            )
            raise
        finally:
            self._pending.pop(key, None)

    def handle_reply(
        self, renderer_id: str, link: RendererLink, in_reply_to: int, message: Any
    ) -> None:
        future = self._pending.get((link, in_reply_to))
        if future is None or future.done():
            logger.debug(
                "Ignoring %s reply to message %d from %s, which nobody awaits",
                self._what,
                in_reply_to,
                renderer_id,
            )
            return
        future.set_result(message)

    def handle_disconnect(self, renderer_id: str, link: RendererLink) -> None:
        """Nothing is coming back on a socket that is gone."""
        for key, future in list(self._pending.items()):
            if key[0] is link and not future.done():
                future.set_exception(
                    RendererUnavailable(
                        f"renderer {renderer_id} disconnected before answering"
                    )
                )
