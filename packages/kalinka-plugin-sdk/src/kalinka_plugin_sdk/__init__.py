"""
Kalinka Plugin SDK

A Software Development Kit for developing input modules and device plugins for the Kalinka Player.
"""

from ._version import __version__

from .api import (
    PlayQueueController,
    EventEmitter,
    EventListener,
    LoggerAPI,
)
from .events import (
    PlayQueueEventType,
    PlayQueueEvent,
    PlayQueueState,
    PlaybackStateChangedEvent,
    RequestMoreTracksEvent,
    TracksAddedEvent,
    TracksRemovedEvent,
    TrackMovedEvent,
    TrackUnavailableEvent,
    PlaybackModeChangedEvent,
    PlaybackErrorEvent,
    RenderersChangedEvent,
    CurrentRendererChangedEvent,
    PlaybackControlChangedEvent,
    RendererDescriptor,
)

from .datamodel import (
    EntityType,
    EntityId,
    DeviceVolume,
    MatchTier,
    NameMatch,
    PlaybackControl,
    PlaybackControlMode,
    VolumeBackend,
)
from .direct_playback import (
    DirectPlayback,
    DirectPlaybackListener,
    DirectPlaybackSession,
    HoldEnded,
    OutputUnavailable,
    RevokeReason,
    TransportKind,
    TransportRequest,
)
from .filters import (
    or_unfiltered,
    TEXT_FIELD,
    TYPE_FIELD,
    FilterKind,
    FilterOp,
    FilterQuery,
    FilterSpec,
    FilterValue,
    FilterValueList,
    RangeSelector,
    TextSelector,
    UnsupportedFilter,
    ValuesSelector,
)
from .inputmodule import (
    InputModule,
    ContentInfo,
    DirectUrl,
    ModuleAsset,
    SourceUnavailableError,
    TrackSource,
    TrackInfo,
    SearchType,
)
from .ext_device import (
    ExternalOutputDevice,
    SupportedFunction,
)
from .plugin import (
    InputPluginContext,
    OutputDevicePluginContext,
)
from .embedding import TextEmbedder
from .module_config import ModuleConfig
from .module_health import ModuleHealthState, ModuleState
from .config_feedback import ConfigIssue, ConfigOption, IssueSeverity
from .config_records import ConfigRecord, Records
from .dynamic_fields import DynamicFieldDecl
from .optional_packages import OptionalPackageSpec

from .external_playback import (
    ExternalPlayback,
    ExternalPlaybackListener,
    ExternalPlaybackSession,
)

__all__ = [
    "ExternalPlayback",
    "ExternalPlaybackListener",
    "ExternalPlaybackSession",
    "__version__",
    # APIs
    "PlayQueueController",
    "EventEmitter",
    "EventListener",
    "LoggerAPI",
    "TextEmbedder",
    "InputPluginContext",
    "OutputDevicePluginContext",
    "DirectPlayback",
    "DirectPlaybackListener",
    "DirectPlaybackSession",
    "HoldEnded",
    "OutputUnavailable",
    "RevokeReason",
    "TransportKind",
    "TransportRequest",
    # Events and States
    "PlayQueueEventType",
    "PlayQueueEvent",
    "PlayQueueState",
    "PlaybackStateChangedEvent",
    "RequestMoreTracksEvent",
    "TracksAddedEvent",
    "TracksRemovedEvent",
    "TrackMovedEvent",
    "TrackUnavailableEvent",
    "PlaybackModeChangedEvent",
    "PlaybackErrorEvent",
    "RenderersChangedEvent",
    "CurrentRendererChangedEvent",
    "PlaybackControlChangedEvent",
    "RendererDescriptor",
    # Data Models
    "EntityType",
    "EntityId",
    "DeviceVolume",
    "MatchTier",
    "NameMatch",
    "PlaybackControl",
    "PlaybackControlMode",
    "VolumeBackend",
    # Filtering
    "TEXT_FIELD",
    "or_unfiltered",
    "TYPE_FIELD",
    "FilterKind",
    "FilterOp",
    "FilterQuery",
    "FilterSpec",
    "FilterValue",
    "FilterValueList",
    "RangeSelector",
    "TextSelector",
    "UnsupportedFilter",
    "ValuesSelector",
    "ContentInfo",
    "DirectUrl",
    "ModuleAsset",
    "SourceUnavailableError",
    "TrackSource",
    "TrackInfo",
    "SearchType",
    # Base Classes
    "InputModule",
    "ExternalOutputDevice",
    "SupportedFunction",
    "ModuleConfig",
    "ModuleHealthState",
    "ModuleState",
    "DynamicFieldDecl",
    "ConfigOption",
    "ConfigIssue",
    "IssueSeverity",
    "ConfigRecord",
    "Records",
    "OptionalPackageSpec",
]
