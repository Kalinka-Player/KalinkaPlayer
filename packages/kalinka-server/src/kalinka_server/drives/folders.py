"""The server's folders as Settings browses them to pick a music folder.

Its own storage, the mounted volumes and the users' home folders, and in
them nothing but folders: no file is listed, read or played from here.
"""

from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel

from kalinka_plugin_sdk.inputmodule import SourceUnavailableError
from kalinka_plugin_sdk.mounts import covers
from kalinka_plugin_sdk.storage import VolumeState

from ..storage.describe import icon_of
from ..storage.manager import StorageManager
from . import fsio
from .fsio import DriveIO
from .listing import ListingCache, count_folder, folder_subname, shown

HOME = "/home"
#: The most folders one page lists, whatever it asks for.
MAX_PAGE = 200


class ServerFolder(BaseModel):
    path: str
    name: str
    subname: Optional[str] = None
    icon: str = "folder"


class ServerFolderList(BaseModel):
    offset: int
    limit: int
    total: int
    folders: list[ServerFolder]


class ServerFolders:
    """Where a music folder can be on this server, and the folders in each."""

    def __init__(self, storage: StorageManager, io: DriveIO, home: str = HOME):
        self._storage = storage
        self._io = io
        self._home = home
        self._listings = ListingCache()

    async def list(self, path: Optional[str] = None, offset: int = 0, limit: int = 50) -> ServerFolderList:
        """The folders in ``path``, or the places folders can be in.

        @raise LookupError For a path in none of the places.
        @raise SourceUnavailableError For a folder that cannot be read now.
        """
        limit = min(limit, MAX_PAGE)
        if path is None:
            places = self._places()
            return ServerFolderList(offset=offset, limit=limit, total=len(places), folders=places[offset : offset + limit])
        if not os.path.isabs(path) or "\0" in path:
            raise LookupError(path)
        real = await self._io.run(os.path.realpath, path)
        if not self._allowed(real):
            raise LookupError(path)
        try:
            contents = await self._io.run(self._listings.list, real)
        except OSError as e:
            raise SourceUnavailableError(f"{shown(os.path.basename(real) or real)} cannot be read") from e
        page = contents.folders[offset : offset + limit]
        counts = await self._io.each_within(count_folder, [p for _, p in page], fsio.COUNT_BUDGET_S)
        folders = [
            ServerFolder(
                path=folder,
                name=shown(name),
                subname=folder_subname(count, await self._storage.library_holding(folder)),
            )
            for (name, folder), count in zip(page, counts)
        ]
        return ServerFolderList(offset=offset, limit=limit, total=len(contents.folders), folders=folders)

    def _mounted(self):
        return [v for v in self._storage.volumes() if v.state == VolumeState.MOUNTED]

    def _places(self) -> list[ServerFolder]:
        places = [
            ServerFolder(path=v.mount_point, name=v.label, subname=v.summary, icon=icon_of(v))
            for v in self._mounted()
        ]
        if os.path.isdir(self._home):
            places.append(
                ServerFolder(path=self._home, name="Home folders", subname="Folders of the users on this server")
            )
        return places

    def _allowed(self, real: str) -> bool:
        if any(covers(v.mount_point, real) for v in self._mounted()):
            return True
        return os.path.isdir(self._home) and covers(self._home, real)
