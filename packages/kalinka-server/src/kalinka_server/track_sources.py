"""Where a queued track's audio comes from, decided when it is about to play.

The play queue holds tracks as metadata alone and asks here for a track's
source only when it plays it. Nothing is asked of a module before then, so
restoring or adding to the queue never waits on a module being ready, and a
module switched on later serves the tracks that were waiting for it.
"""

from typing import Optional, Protocol

from kalinka_plugin_sdk.datamodel import EntityId
from kalinka_plugin_sdk.inputmodule import (
    InputModule,
    SourceUnavailableError,
    TrackSource,
)

_NO_MODULE = "This track's source is not available"


class TrackSourceResolver(Protocol):
    """Resolves a queued track's id to the source a renderer plays.

    Raises SourceUnavailableError, with a message fit for the user, when the
    track exists but cannot be served now; anything else it raises fails the
    play with no reason to show.
    """

    async def resolve(self, track_id: EntityId) -> TrackSource: ...

    def unavailable_reason(self, track_id: EntityId) -> Optional[str]:
        """Why the track cannot be played now, if that is known without asking
        anyone — nothing here could serve it — else None. Fit for the user."""
        ...


class InputModuleRegistry(Protocol):
    """The input modules the server can reach right now, by module name."""

    def enabled_input_module(self, name: str) -> Optional[InputModule]:
        """The module ``name`` while it is enabled and set up, else None."""
        ...


class ModuleTrackSources:
    """Resolves a track through the module that owns it.

    The module is looked up in the registry on every call rather than held,
    so one enabled, disabled or set up again since the track was queued is
    the one asked.
    """

    def __init__(self, registry: InputModuleRegistry):
        self._registry = registry

    async def resolve(self, track_id: EntityId) -> TrackSource:
        module = self._registry.enabled_input_module(track_id.source)
        if module is None:
            raise SourceUnavailableError(_NO_MODULE)
        return await module.get_track_source(track_id.id)

    def unavailable_reason(self, track_id: EntityId) -> Optional[str]:
        if self._registry.enabled_input_module(track_id.source) is None:
            return _NO_MODULE
        return None
