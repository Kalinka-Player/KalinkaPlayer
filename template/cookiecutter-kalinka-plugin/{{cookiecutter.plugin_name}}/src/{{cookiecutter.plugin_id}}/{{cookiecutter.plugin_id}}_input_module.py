from typing import List, Optional
from kalinka_plugin_sdk.datamodel import (
    BrowseItem,
    BrowseItemList,
    EntityId,
    FavoriteIds,
    Playlist,
)
from kalinka_plugin_sdk.filters import FilterQuery, FilterValueList
from kalinka_plugin_sdk.inputmodule import (
    InputModule,
    SearchType,
    TrackInfo,
    TrackSource,
)

from .config_model import {{ cookiecutter.plugin_class_prefix }}Config


class {{ cookiecutter.plugin_class_prefix }}InputModule(InputModule):
    def __init__(self, config: {{ cookiecutter.plugin_class_prefix }}Config):
        self.config = config

    def module_name(self) -> str:
        return "{{ cookiecutter.plugin_display_name }}"

    async def search(
        self, type: SearchType, query: str, offset=0, limit=50
    ) -> BrowseItemList:
        raise NotImplementedError

    async def browse(
        self,
        entity_id: EntityId,
        offset: int = 0,
        limit: int = 50,
        filter: Optional[FilterQuery] = None,
    ) -> BrowseItemList:
        raise NotImplementedError

    async def get_track_info(self, track_ids: List[str]) -> List[TrackInfo]:
        raise NotImplementedError

    async def get_track_source(self, track_id: str) -> TrackSource:
        raise NotImplementedError

    async def list_favorite(
        self, type: SearchType, filter: str, offset: int = 0, limit: int = 50
    ) -> BrowseItemList:
        raise NotImplementedError

    async def get_favorite_ids(self) -> FavoriteIds:
        raise NotImplementedError

    async def add_to_favorite(self, id: str):
        raise NotImplementedError

    async def remove_from_favorite(self, id: str):
        raise NotImplementedError

    async def list_filter_values(
        self,
        catalog_id: EntityId,
        field: str,
        offset: int = 0,
        limit: int = 50,
        q: str = "",
    ) -> FilterValueList:
        raise NotImplementedError

    async def get(self, entity_id: EntityId) -> BrowseItem:
        raise NotImplementedError

    async def playlist_user_list(self, offset: int = 0, limit: int = 25) -> BrowseItemList:
        raise NotImplementedError

    async def playlist_create(self, name: str, description: str) -> Playlist:
        raise NotImplementedError

    async def playlist_update(
        self, id: str, name: str | None, description: str | None
    ) -> Playlist:
        raise NotImplementedError

    async def playlist_delete(self, id: str):
        raise NotImplementedError

    async def playlist_add_tracks(
        self, id: str, track_ids: List[str], allow_duplicates: bool
    ) -> Playlist:
        raise NotImplementedError

    async def playlist_remove_tracks(
        self, id: str, playlist_track_ids: List[str]
    ) -> Playlist:
        raise NotImplementedError

    async def get_resource_path(self, id: str) -> str | None:
        raise NotImplementedError
