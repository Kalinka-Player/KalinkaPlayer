"""Everything a client may browse, whether or not a plugin provides it.

Browsing, name search and the vocabularies behind a filter are answered by
input modules *and* by the server's own built-ins — collections is one. Those
built-ins own no audio: nothing streams from them, nothing is suggested by
them, and the queue never resolves a track through them. Splitting the read
contract out of :class:`~kalinka_plugin_sdk.inputmodule.InputModule` is what
keeps a built-in from having to pretend otherwise.

An input module satisfies :class:`BrowseSource` structurally, so a plugin
declares nothing to take part.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, Sequence

from kalinka_plugin_sdk.datamodel import BrowseItem, BrowseItemList, EntityId
from kalinka_plugin_sdk.filters import FilterQuery, FilterValueList
from kalinka_plugin_sdk.inputmodule import SearchType


class BrowseSource(Protocol):
    """The read half of a source: listings, lookups, names and vocabularies.

    Every method carries the meaning it has on
    :class:`~kalinka_plugin_sdk.inputmodule.InputModule`; see that contract for
    paging, filter refusal and the shape of what comes back.
    """

    def module_name(self) -> str: ...

    async def browse(
        self,
        entity_id: EntityId,
        offset: int = 0,
        limit: int = 50,
        filter: Optional[FilterQuery] = None,
    ) -> BrowseItemList: ...

    async def get(self, entity_id: EntityId) -> BrowseItem: ...

    async def search(
        self, type: SearchType, query: str, offset: int = 0, limit: int = 50
    ) -> BrowseItemList: ...

    async def list_filter_values(
        self,
        catalog_id: EntityId,
        field: str,
        offset: int = 0,
        limit: int = 50,
        q: str = "",
    ) -> FilterValueList: ...

    async def playlist_user_list(
        self, offset: int = 0, limit: int = 25
    ) -> BrowseItemList: ...

    async def tracks_to_add(
        self, entity_id: EntityId, limit: int
    ) -> Optional[List[EntityId]]: ...


@dataclass(frozen=True)
class RegisteredSource:
    """One browsable source as clients see it.

    ``title`` is the name shown in headers and badges — a plugin's comes from
    its config, a built-in's from itself, so neither has to be looked up twice.
    ``builtin`` says the server provides it: it has no config page, no
    suggestions and no favourites.
    """

    name: str
    title: str
    source: BrowseSource
    builtin: bool = False


class BrowseSourceRegistry:
    """Which sources a read request may reach, resolved per call.

    Plugins are supplied by a callable rather than a snapshot: one can be
    enabled or torn down while the server runs, and a listing must follow that
    without the registry being rebuilt.
    """

    def __init__(
        self,
        plugins: Callable[[], List[RegisteredSource]],
        builtins: Sequence[RegisteredSource] = (),
    ):
        self._plugins = plugins
        self._builtins = tuple(builtins)

    def entries(self) -> List[RegisteredSource]:
        """Every reachable source, ordered by name — the order the browse root
        is listed in."""
        return sorted(
            [*self._plugins(), *self._builtins], key=lambda entry: entry.name
        )

    def get(self, name: str) -> Optional[RegisteredSource]:
        return next((entry for entry in self.entries() if entry.name == name), None)

    def sources(self) -> List[BrowseSource]:
        return [entry.source for entry in self.entries()]
