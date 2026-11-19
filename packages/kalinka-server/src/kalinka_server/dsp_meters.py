"""What the renderer's DSP measures, passed on to clients for what they are
shown playing.

Meters are scoped as playback state is: only a session that playback runs on —
one the pool announced, which a speaker test's never is — on the renderer
clients are told is current, and only for the source it plays. Anything else
is a report about audio no client is looking at, and is dropped.
"""

from __future__ import annotations

from typing import Callable

from kalinka_plugin_sdk.datamodel import DspMeters
from kalinka_plugin_sdk.events import DspMetersChangedEvent

from .renderer_registry import RendererRegistry
from .renderer_sessions import PlaybackSession, SessionPool


class DspMetersRelay:
    """Dispatches a DspMetersChangedEvent for each meters report clients should
    see.

    @note Runs on the event loop, from the renderer connection's processor;
        ``dispatch`` must not block.
    """

    def __init__(
        self,
        registry: RendererRegistry,
        pool: SessionPool,
        dispatch: Callable[[DspMetersChangedEvent], None],
    ):
        self._registry = registry
        self._dispatch = dispatch
        pool.add_open_hook(self._adopt)

    def _adopt(self, session: PlaybackSession) -> None:
        session.on_meters(self._relay)

    def _relay(self, session: PlaybackSession, meters: dict) -> None:
        if session.renderer_id != self._registry.serving_id():
            return
        if meters["source_token"] != session.snapshot.get("source_token"):
            return
        self._dispatch(
            DspMetersChangedEvent(
                meters=DspMeters(
                    clipped_samples=meters["clipped_samples"],
                    true_peak_dbfs=meters["true_peak_dbfs"],
                    load=meters["load"],
                )
            )
        )
