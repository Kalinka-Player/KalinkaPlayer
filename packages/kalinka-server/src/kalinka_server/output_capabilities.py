"""What the renderer playback would go to can play, as that renderer says.

A renderer states its capabilities in Hello and again, to every Core it is
connected to, whenever they change, so the answer is already in the registry.
Nothing is known of a renderer too old to state them.
"""

from __future__ import annotations

from typing import Callable

from kalinka_plugin_sdk.direct_playback import OutputCapabilities

from .renderer_registry import RendererRegistry

Watcher = Callable[[OutputCapabilities], None]


class OutputCapabilityTracker:
    """The active renderer's OutputCapabilities, told to watchers as they change.

    Watchers are called synchronously, in order, from whatever moved the
    registry, and must not block.
    """

    def __init__(self, registry: RendererRegistry):
        self._registry = registry
        self._watchers: list[Watcher] = []
        self._published = OutputCapabilities()
        registry.add_observer(self._publish)

    def watch(self, watcher: Watcher) -> Callable[[], None]:
        """Tell ``watcher`` the capabilities now, then each time they change.

        @return A function that stops it; calling it again does nothing.
        """
        self._watchers.append(watcher)
        watcher(self._current())

        def stop() -> None:
            if watcher in self._watchers:
                self._watchers.remove(watcher)

        return stop

    def _current(self) -> OutputCapabilities:
        renderer_id = self._registry.active_id()
        record = self._registry.get(renderer_id) if renderer_id else None
        if record is None or not record.playable or record.capabilities is None:
            return OutputCapabilities()
        return record.capabilities

    def _publish(self) -> None:
        current = self._current()
        if current == self._published:
            return
        self._published = current
        for watcher in list(self._watchers):
            watcher(current)
