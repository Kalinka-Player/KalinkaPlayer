"""Where a queued track's audio comes from, decided when it is about to play.

The play queue holds tracks as metadata alone and asks here for a track's
source only when it plays it. Nothing is asked of a module before then, so
restoring or adding to the queue never waits on a module being ready, and a
module switched on later serves the tracks that were waiting for it.
"""

import logging
from typing import Callable, Optional, Protocol

from kalinka_plugin_sdk.datamodel import EntityId
from kalinka_plugin_sdk.inputmodule import (
    InputModule,
    SourceUnavailableError,
    TrackSource,
)

logger = logging.getLogger(__name__.split(".")[-1])

_NO_MODULE = "This track's source is not available"


class TrackSources(Protocol):
    """Resolves a queued track's id to the source a renderer plays.

    Raises SourceUnavailableError, with a message fit for the user, when the
    track exists but cannot be served now, and LookupError when its source no
    longer has it.
    """

    async def resolve(self, track_id: EntityId) -> TrackSource: ...

    def unavailable_reason(self, track_id: EntityId) -> Optional[str]:
        """Why the track cannot be played now, if that is known without asking
        anyone — nothing here could serve it — else None. Fit for the user."""
        ...


class ModuleTrackSources(TrackSources):
    """Resolves a track through the module that owns it.

    The module is looked up through ``module_for`` on every call rather than
    held, so one enabled, disabled or set up again since the track was queued
    is the one asked. ``module_for`` answers None for a module that is not
    enabled and set up.
    """

    def __init__(self, module_for: Callable[[str], Optional[InputModule]]):
        self._module_for = module_for

    async def resolve(self, track_id: EntityId) -> TrackSource:
        module = self._module_for(track_id.source)
        if module is None:
            raise SourceUnavailableError(_NO_MODULE)
        try:
            return await module.get_track_source(track_id.id)
        except SourceUnavailableError:
            raise
        except Exception as e:
            # A plugin's KeyError or IndexError is a bug, not a missing track.
            if isinstance(e, LookupError) and not isinstance(e, (KeyError, IndexError)):
                raise
            # The type only: a plugin's error text may carry a credential.
            logger.warning(
                "%s could not resolve %s: %s",
                track_id.source,
                track_id.to_string,
                type(e).__name__,
            )
            raise SourceUnavailableError(
                f"{module.display_name()} cannot play this track right now"
            ) from e

    def unavailable_reason(self, track_id: EntityId) -> Optional[str]:
        if self._module_for(track_id.source) is None:
            return _NO_MODULE
        return None
