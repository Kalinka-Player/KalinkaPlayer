# kalinka_plugin_sdk/api.py
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from enum import Enum
from typing import Any, Generic, Literal, Optional, Protocol, TypeVar, Union

from pydantic import BaseModel, ConfigDict

from .datamodel import EntityId, PlaybackMode, PlaybackState, Track, TrackList
from .inputmodule import TrackInfo


class PlayQueueController(Protocol):
    """
    Contract for play queue operations.
    All methods should be implemented by the play queue provider.
    """

    def play(self, index: Optional[int] = None) -> Coroutine[Any, Any, None]:
        """Start playback at the given index, or resume if index is None."""
        ...

    def play_next(self, index: int) -> Coroutine[Any, Any, None]:
        """Play the track at the given index next."""
        ...

    def pause(self, paused: bool) -> Coroutine[Any, Any, None]:
        """Pause or resume playback.
        If paused is True, pause playback; if False, resume playback.
        """
        ...

    def next(self) -> Coroutine[Any, Any, None]:
        """Skip to the next track."""
        ...

    def prev(self) -> Coroutine[Any, Any, None]:
        """Go back to the previous track."""
        ...

    def seek(self, position_ms: int) -> Coroutine[Any, Any, None]:
        """Seek to the given position in milliseconds."""
        ...

    def stop(self) -> Coroutine[Any, Any, None]:
        """Stop playback."""
        ...

    def add(
        self, tracks: list[Track | TrackInfo], index: Optional[int] = None
    ) -> Coroutine[Any, Any, None]:
        """Add tracks to the queue. If index is given, insert at that position; otherwise append.

        The queue keeps each track's metadata and asks the track's module for
        its source when it plays (SDK 3.8). A TrackInfo is still accepted:
        its metadata is kept and its source_retriever is not used, and one
        without metadata is not queued.
        """
        ...

    def remove(self, tracks: list[int]) -> Coroutine[Any, Any, None]:
        """Remove tracks by their indices."""
        ...

    def list(self, offset: int, limit: int) -> Coroutine[Any, Any, TrackList]:
        """List tracks in the queue with pagination."""
        ...

    def get_track_info(self, index: int) -> Coroutine[Any, Any, Optional[Track]]:
        """Get info for the track at the given index."""
        ...

    def get_playback_state(self) -> Coroutine[Any, Any, PlaybackState]:
        """Get the current playback state."""
        ...

    def restore_from_state(
        self,
        state: Any,
        track_info_retriever: Optional[
            Callable[[EntityId], Awaitable[TrackInfo]]
        ] = None,
    ) -> Coroutine[Any, Any, None]:
        """Restore the playback state from a saved state.

        Every saved track comes back as it was saved, with no module asked
        (SDK 3.8). ``track_info_retriever`` is accepted from older callers
        and not used.
        """
        ...

    def clear(self) -> Coroutine[Any, Any, None]:
        """Clear the play queue.
        Stops the playback if it is active.
        """
        ...

    async def replace(self, tracks: Sequence[Track | TrackInfo]) -> None:
        """SDK 3.10: clear the queue and add ``tracks`` in its place.

        The server's queue does both as one command, so no other caller finds
        the queue empty in between, and its playback state goes straight to the
        new first track. This default, which a queue written before 3.10
        inherits, clears and then adds.
        """
        await self.clear()
        await self.add(list(tracks))

    def move(self, from_index: int, to_index: int) -> Coroutine[Any, Any, None]:
        """Move the track at from_index to to_index, shifting others as needed."""
        ...

    def set_playback_mode(
        self,
        shuffle: Optional[bool],
        repeat_single: Optional[bool],
        repeat_all: Optional[bool],
    ) -> Coroutine[Any, Any, PlaybackMode]:
        """Set playback modes: shuffle, repeat single, repeat all."""
        ...

    def get_playback_mode(self) -> Coroutine[Any, Any, PlaybackMode]:
        """Get the current playback mode."""
        ...


E_contra = TypeVar("E_contra", bound=Enum, contravariant=True)
E = TypeVar("E", bound=Enum)
EV_state = TypeVar("EV_state", bound="BaseEvent")
S = TypeVar("S", bound="BaseState")


class BaseEvent(BaseModel, Generic[E]):
    """Base event type for user-defined events.

    Users should subclass this and add event-specific fields.
    `event_type` should be set to an enum value describing the event.
    Global sequence `seq` is assigned by the bus on dispatch.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    event_type: E
    seq: int = 0


class ReplayEvent(BaseModel, Generic[S]):
    """Synthetic event delivered first on subscription containing a snapshot of state.

    `seq` is the per-subscription sequence number (starts at 0).
    `state_type` identifies the type of state for proper deserialization.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    event_type: Literal["replay_event"] = "replay_event"
    state_type: str
    state: S
    server_time_ns: int
    seq: int


class BaseState(BaseModel, Generic[EV_state]):
    """Base state type for user-defined states.

    Users should subclass this and add state-specific fields.
    Subclasses should implement apply(event) to return a new state instance.

    Type parameter EV_state specifies the event type this state handles.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    seq: int = 0

    def apply(self, event: EV_state) -> "BaseState[EV_state]":
        """Apply an event to produce a new state (immutable pattern).

        Subclasses must override this to return a new state with updates applied.
        If the event is stale (seq <= self.seq), return self unchanged.

        Args:
            event: The event to apply (type matches the state's event type parameter)

        Returns:
            A new state with the event applied, or self if event is stale
        """
        raise NotImplementedError("Subclasses must implement apply()")


EV_emit = TypeVar("EV_emit", bound=BaseEvent, contravariant=True)
EV_listen = TypeVar("EV_listen", bound=BaseEvent, covariant=True)
S_emit = TypeVar("S_emit", bound=BaseState, contravariant=True)


class EventEmitter(Protocol[EV_emit, S_emit]):
    def dispatch(self, event: EV_emit) -> None:
        """Dispatch a new event to the bus (thread-safe)."""
        ...

    def set_initial_state(self, state: S_emit) -> None:
        """Set the initial state and notify subscribers with a replay event.

        This method is intended for initialization (e.g., during device setup).
        It replaces the current bus state, increments the global sequence, and sends
        a replay event to all existing subscribers.

        Args:
            state: The new initial state to set.
        """
        ...


class AsyncEventStream(Protocol[EV_listen, S]):
    """Protocol for async iteration over events.

    Represents an async iterable stream of events that can be consumed
    using async for loops. May include replay events at the start of the stream.
    """

    def __aiter__(self) -> "AsyncEventStream[EV_listen, S]":
        """Return the async iterator object."""
        ...

    async def __anext__(self) -> Union[EV_listen, ReplayEvent[S]]:
        """Return the next event in the stream.

        Raises StopAsyncIteration when the stream is closed.
        """
        ...


class EventListener(Protocol[E_contra, EV_listen, S]):
    def subscribe(
        self,
        event_types: Iterable[E_contra],
        callback: Optional[Callable[[Union[EV_listen, ReplayEvent[S]]], None]] = None,
        listener_id: Optional[str] = None,
    ) -> "Subscription":
        """Subscribe to event types with optional callback; returns a Subscription."""
        ...

    def unsubscribe(self, listener_id: str) -> None:
        """Unsubscribe and close the listener's queue."""
        ...

    def stream(
        self,
        event_types: Iterable[E_contra],
    ) -> "AsyncEventStream[EV_listen, S]":
        """Create an async event stream for the given event types.

        Returns an AsyncEventStream that can be consumed using async for loops.
        The stream will include a replay event at the start containing the current state.
        """
        ...


class Subscription(Protocol):
    @property
    def id(self) -> str:  # unique per subscription
        ...

    def unsubscribe(self) -> None: ...


class LoggerAPI(Protocol):
    def debug(self, msg, *args, **kwargs): ...
    def info(self, msg, *args, **kwargs): ...
    def warning(self, msg, *args, **kwargs): ...
    def error(self, msg, *args, **kwargs): ...
    def critical(self, msg, *args, **kwargs): ...
    def fatal(self, msg, *args, **kwargs): ...
