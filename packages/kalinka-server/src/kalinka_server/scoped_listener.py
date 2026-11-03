"""The event listener a plugin is handed: the shared bus, with every
subscription the plugin opens on record, so unloading the plugin can close
what it left open — each open subscription is a worker thread that would
keep calling into an instance that is gone.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from kalinka_eventbus.bus import AsyncEventStream
from kalinka_plugin_sdk.api import EventListener, Subscription


class _ScopedSubscription:
    """The plugin's handle on a subscription: closing it through the handle
    takes it off the record as well."""

    def __init__(self, listener: "ScopedListener", subscription: Subscription):
        self._listener = listener
        self._subscription = subscription

    @property
    def id(self) -> str:
        return self._subscription.id

    def unsubscribe(self) -> None:
        self._listener.unsubscribe(self.id)


class ScopedListener:
    """One plugin's view of a bus; substitutable for the bus as a listener."""

    def __init__(self, bus: EventListener, plugin_id: str):
        self._bus = bus
        self._plugin_id = plugin_id
        self._ids: set[str] = set()

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    def subscribe(
        self,
        event_types: Iterable,
        callback: Optional[Callable] = None,
        listener_id: Optional[str] = None,
    ) -> Subscription:
        subscription = self._bus.subscribe(event_types, callback, listener_id)
        self._ids.add(subscription.id)
        return _ScopedSubscription(self, subscription)

    def unsubscribe(self, listener_id: str) -> None:
        self._ids.discard(listener_id)
        self._bus.unsubscribe(listener_id)

    def stream(self, event_types: Iterable) -> AsyncEventStream:
        # Through this listener, so the stream's own subscription is on record.
        return AsyncEventStream(self, event_types)  # type: ignore[arg-type]

    def get_snapshot(self):
        return self._bus.get_snapshot()

    def revoke_all(self) -> int:
        """Close every subscription still open.

        @return How many there were — a plugin's shutdown should have left none.
        """
        left = list(self._ids)
        self._ids.clear()
        for listener_id in left:
            self._bus.unsubscribe(listener_id)
        return len(left)
