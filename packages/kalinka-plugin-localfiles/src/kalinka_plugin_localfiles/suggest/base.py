"""What can be offered when the user is asked for a music folder.

A suggester answers from what it already knows, because the settings page
asks on every read and a network scan takes seconds. Anything slower is
started by :meth:`RootSuggester.refresh` and collected by a later ask.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from kalinka_plugin_sdk import ConfigOption


@runtime_checkable
class RootSuggester(Protocol):
    """One source of music-folder suggestions.

    @note Lives in the process that serves the settings page and nowhere
        else. The indexer and the embedder read files; only this side is
        asked what files there might be.
    """

    def options(self) -> list[ConfigOption]:
        """What is known right now. Never blocks, never raises."""

    def refresh(self) -> None:
        """Ask for a fresher answer, to be collected by a later
        :meth:`options`. Returns at once; a source with nothing to wait for
        does nothing."""
