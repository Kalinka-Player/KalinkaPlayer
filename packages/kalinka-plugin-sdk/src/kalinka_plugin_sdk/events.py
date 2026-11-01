from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .api import BaseEvent, BaseState

from .datamodel import (
    Track,
    PlaybackControl,
    PlaybackState,
    PlaybackMode,
)
from .module_health import ModuleHealthState
from .module_icons import ModuleIcons
from .storage import VolumeDescriptor


class PlayQueueEventType(Enum):
    PlaybackStateChanged = "state_changed"
    RequestMoreTracks = "request_more_tracks"
    TracksAdded = "tracks_added"
    TracksRemoved = "tracks_removed"
    TrackMoved = "track_moved"
    TrackUnavailable = "track_unavailable"
    PlaybackError = "playback_error"
    PlaybackModeChanged = "playback_mode_changed"
    RenderersChanged = "renderers_changed"
    CurrentRendererChanged = "current_renderer_changed"
    PlaybackControlChanged = "playback_control_changed"
    ModulesChanged = "modules_changed"
    VolumesChanged = "volumes_changed"


class RendererDescriptor(BaseModel):
    """One playback endpoint as the Core knows it.

    Deliberately carries no active/selected marker: which renderer is current
    is a separate axis of state (``CurrentRendererChangedEvent``), so one
    fact never rides two events.
    """

    renderer_id: str
    instance_id: str = ""
    friendly_name: str = ""
    software_version: str = ""
    kind: str = ""
    status: str = "offline"
    # False when the renderer speaks no protocol version this Core does. It
    # stays listed and connected so it can still be told to upgrade, but
    # playback never goes to it.
    compatible: bool = True
    # Whether this renderer can install a new release of itself on request.
    # False for one installed some other way (flatpak, from source), which is
    # never offered an upgrade it could not perform.
    upgrade_supported: bool = False
    platform: Dict[str, str] = Field(default_factory=dict)
    connected_at: float = 0.0
    last_seen: float = 0.0


class ModuleDescriptor(BaseModel):
    """SDK 3.17: one plugin, or a source the server provides itself, as
    clients see it — an entry of ``GET /server/modules``.

    ``state``, ``error_message`` and ``missing_packages`` are what the plugin
    last said of itself; ``enabled`` is its configuration's switch.
    """

    name: str
    title: str
    icon: Optional[str] = None
    icons: Optional[ModuleIcons] = None
    enabled: bool = True
    state: ModuleHealthState
    error_message: Optional[str] = None
    missing_packages: List[str] = Field(default_factory=list)
    builtin: bool = False
    capabilities: List[str] = Field(default_factory=list)
    #: SDK 3.20: a number a client may show on the source's icon, such as
    #: the drives Drives lists; None where it counts nothing.
    count: Optional[int] = None


class ModulesSnapshot(BaseModel):
    """SDK 3.17: every input module and device, as ``GET /server/modules``
    answers."""

    input_modules: List[ModuleDescriptor] = Field(default_factory=list)
    devices: List[ModuleDescriptor] = Field(default_factory=list)


class PlayQueueEvent(BaseEvent[PlayQueueEventType]):
    """Base class for all play queue events."""

    pass


class PlayQueueState(BaseState[PlayQueueEvent]):
    """State model for playqueue - uses Pydantic BaseModel for serialization."""

    playback_state: PlaybackState
    track_list: List[Track]
    playback_mode: PlaybackMode
    renderers: List[RendererDescriptor] = Field(default_factory=list)
    current_renderer_id: Optional[str] = None
    selected_renderer_id: Optional[str] = None
    # Whether the queue plays, or an input plugin holds the output exclusively;
    # while one does, playback_state describes the plugin's playback.
    playback_control: PlaybackControl = Field(default_factory=PlaybackControl)
    # SDK 3.17; None from a server that never publishes them.
    modules: Optional[ModulesSnapshot] = None
    # SDK 3.20; None from a server without a storage manager.
    volumes: Optional[List[VolumeDescriptor]] = None

    def apply(self, event: PlayQueueEvent) -> "PlayQueueState":
        """Apply event and return a new state (immutable pattern)."""
        if self.seq >= event.seq:
            return self

        updates: dict[str, Any] = {"seq": event.seq}

        if isinstance(event, PlaybackStateChangedEvent):
            updates["playback_state"] = event.state
        elif isinstance(event, TracksAddedEvent):
            track_list = list(self.track_list)
            for i, track in enumerate(event.tracks):
                track_list.insert(event.index + i, track)
            updates["track_list"] = track_list
        elif isinstance(event, TracksRemovedEvent):
            new_track_list = [
                track
                for i, track in enumerate(self.track_list)
                if i not in event.indices
            ]
            updates["track_list"] = new_track_list
        elif isinstance(event, TrackMovedEvent):
            track_list = list(self.track_list)
            track = track_list.pop(event.from_index)
            track_list.insert(event.to_index, track)
            updates["track_list"] = track_list
        elif isinstance(event, TrackUnavailableEvent):
            if not (0 <= event.index < len(self.track_list)):
                return self
            track_list = list(self.track_list)
            track_list[event.index] = track_list[event.index].model_copy(
                update={"unavailable": event.unavailable}
            )
            updates["track_list"] = track_list
        elif isinstance(event, PlaybackModeChangedEvent):
            updates["playback_mode"] = event.mode
        elif isinstance(event, RenderersChangedEvent):
            updates["renderers"] = event.renderers
        elif isinstance(event, CurrentRendererChangedEvent):
            updates["current_renderer_id"] = event.renderer_id
            updates["selected_renderer_id"] = event.selected_renderer_id
        elif isinstance(event, PlaybackControlChangedEvent):
            updates["playback_control"] = event.control
        elif isinstance(event, ModulesChangedEvent):
            updates["modules"] = ModulesSnapshot(
                input_modules=event.input_modules, devices=event.devices
            )
        elif isinstance(event, VolumesChangedEvent):
            updates["volumes"] = event.volumes
        else:
            return self

        return self.model_copy(update=updates)


class PlaybackStateChangedEvent(PlayQueueEvent):
    event_type: PlayQueueEventType = PlayQueueEventType.PlaybackStateChanged
    state: PlaybackState


class RequestMoreTracksEvent(PlayQueueEvent):
    event_type: PlayQueueEventType = PlayQueueEventType.RequestMoreTracks


class TracksAddedEvent(PlayQueueEvent):
    event_type: PlayQueueEventType = PlayQueueEventType.TracksAdded
    tracks: List[Track]
    index: int  # actual list index at which tracks were inserted


class TracksRemovedEvent(PlayQueueEvent):
    event_type: PlayQueueEventType = PlayQueueEventType.TracksRemoved
    indices: List[int]


class TrackMovedEvent(PlayQueueEvent):
    event_type: PlayQueueEventType = PlayQueueEventType.TrackMoved
    from_index: int
    to_index: int


class PlaybackModeChangedEvent(PlayQueueEvent):
    event_type: PlayQueueEventType = PlayQueueEventType.PlaybackModeChanged
    mode: PlaybackMode


class TrackUnavailableEvent(PlayQueueEvent):
    event_type: PlayQueueEventType = PlayQueueEventType.TrackUnavailable
    index: int
    # True marks the track as unavailable (URL retrieval failed); False clears
    # the flag once the track has been streamed successfully again.
    unavailable: bool = True
    # User-presentable cause when the module offered one (e.g. an unmounted
    # network share); None for a generic failure.
    reason: Optional[str] = None


class PlaybackErrorEvent(PlayQueueEvent):
    event_type: PlayQueueEventType = PlayQueueEventType.PlaybackError
    message: str


class RenderersChangedEvent(PlayQueueEvent):
    """Full snapshot of the known renderers; idempotent by construction, so a
    duplicate or missed event never leaves a client wrong for long."""

    event_type: PlayQueueEventType = PlayQueueEventType.RenderersChanged
    renderers: List[RendererDescriptor]


class CurrentRendererChangedEvent(PlayQueueEvent):
    """Which renderer playback runs on: ``renderer_id`` is the effective one,
    ``selected_renderer_id`` the client's pin (None = automatic)."""

    event_type: PlayQueueEventType = PlayQueueEventType.CurrentRendererChanged
    renderer_id: Optional[str] = None
    selected_renderer_id: Optional[str] = None


class PlaybackControlChangedEvent(PlayQueueEvent):
    """Who drives the output changed: the play queue, or an input plugin
    playing exclusively outside it.

    Always followed by a PlaybackStateChangedEvent describing the new
    controller's playback."""

    event_type: PlayQueueEventType = PlayQueueEventType.PlaybackControlChanged
    control: PlaybackControl = Field(default_factory=PlaybackControl)


class ModulesChangedEvent(PlayQueueEvent):
    """SDK 3.17: what some plugin says of itself changed. Every module and
    device, in the shape ``GET /server/modules`` answers, so like
    ``RenderersChangedEvent`` it is idempotent by construction."""

    event_type: PlayQueueEventType = PlayQueueEventType.ModulesChanged
    input_modules: List[ModuleDescriptor]
    devices: List[ModuleDescriptor]


class VolumesChangedEvent(PlayQueueEvent):
    """SDK 3.20: a volume was mounted, unmounted, or changed state. Every
    volume, as ``GET /server/storage/volumes`` lists them."""

    event_type: PlayQueueEventType = PlayQueueEventType.VolumesChanged
    volumes: List[VolumeDescriptor]
