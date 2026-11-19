"""What happens during the renderer's playback, passed on to clients for what
they are shown playing.

Events are scoped as playback state is: only a session that playback runs on —
one the pool announced, which a speaker test's never is — on the renderer
clients are told is current, and, for an event about a source, only the source
it plays. Anything else happened to audio no client is looking at, and is
dropped.
"""

from __future__ import annotations

from typing import Callable

from kalinka_plugin_sdk.events import PlaybackEventReport

from .playback_view import token_to_play_id
from .renderer_registry import RendererRegistry
from .renderer_sessions import PlaybackSession, SessionPool


class PlaybackEventRelay:
    """Dispatches a PlaybackEventReport for each playback event clients should
    see, of any kind the protocol has: none is known here by name.

    @note Runs on the event loop, from the renderer connection's processor;
        ``dispatch`` must not block.
    """

    def __init__(
        self,
        registry: RendererRegistry,
        pool: SessionPool,
        dispatch: Callable[[PlaybackEventReport], None],
    ):
        self._registry = registry
        self._dispatch = dispatch
        pool.add_open_hook(self._adopt)

    def _adopt(self, session: PlaybackSession) -> None:
        session.on_playback_event(self._relay)

    def _relay(self, session: PlaybackSession, event: dict) -> None:
        if session.renderer_id != self._registry.serving_id():
            return
        token = event["source_token"]
        if token is not None and token != session.snapshot.get("source_token"):
            return
        self._dispatch(
            PlaybackEventReport(
                play_id=token_to_play_id(token),
                kind=event["kind"],
                details=event["details"],
            )
        )
