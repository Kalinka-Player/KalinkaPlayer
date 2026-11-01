"""What a page says of each folder it lists, for Drives' pages and for the
folders Settings picks from alike."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional, Sequence

from kalinka_plugin_sdk.inputmodule import SourceUnavailableError

from ..storage.manager import StorageManager
from . import fsio
from .fsio import DriveIO
from .listing import FolderContents, FolderCounts, ListingCache, count_folder, folder_subname, shown


@dataclass(frozen=True)
class FolderRow:
    name: str
    path: str
    #: None for a folder not counted in time.
    counts: Optional[FolderCounts]
    #: The library holding it, by title.
    held_in: Optional[str]

    @property
    def subname(self) -> Optional[str]:
        return folder_subname(self.counts, self.held_in)


async def listed(io: DriveIO, listings: ListingCache, path: str) -> FolderContents:
    """What ``path``, a resolved folder, holds.

    @raise SourceUnavailableError For one that cannot be read now.
    """
    try:
        return await io.run(listings.list, path, at=path)
    except OSError as e:
        raise SourceUnavailableError(f"{shown(os.path.basename(path) or path)} cannot be read") from e


async def folder_rows(
    io: DriveIO, storage: StorageManager, folders: Sequence[tuple[str, str]]
) -> list[FolderRow]:
    """``folders``, each a name and a path, counted and with the library
    holding them, all asked at once."""
    paths = [path for _, path in folders]
    counts, held = await asyncio.gather(
        io.each_within(count_folder, paths, fsio.COUNT_BUDGET_S),
        asyncio.gather(*(storage.library_holding(path) for path in paths)),
    )
    return [FolderRow(shown(name), path, c, h) for (name, path), c, h in zip(folders, counts, held)]
