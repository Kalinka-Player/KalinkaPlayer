"""Input-module interface for controller-supplied UPnP media."""

from kalinka_plugin_sdk.datamodel import EmptyList, FavoriteIds
from kalinka_plugin_sdk.inputmodule import InputModule


class UpnpInput(InputModule):
    """A receiver has no browseable catalog; its controller supplies tracks."""

    def module_name(self):
        return "upnp"

    def display_name(self):
        return "UPnP"

    async def browse(self, entity_id, offset=0, limit=50, filter=None):
        return EmptyList(offset, limit)

    async def search(self, type, query, offset=0, limit=50):
        return EmptyList(offset, limit)

    async def get_track_info(self, track_ids):
        return []

    async def get(self, entity_id):
        raise KeyError(entity_id)

    async def list_favorite(self, type, filter, offset=0, limit=50):
        return EmptyList(offset, limit)

    async def get_favorite_ids(self):
        return FavoriteIds()

    async def playlist_user_list(self, offset=0, limit=25):
        return EmptyList(offset, limit)
