"""Which storage a location belongs to.

The library holds paths, not protocols. Everything that reads a file asks
the resolver which storage speaks for it and then uses that storage's
methods — so adding a protocol means adding a :class:`FileStorage` and
listing it in :func:`build_resolver`, and nothing that reads files changes.

The resolver also owns the two questions that span every storage: what a
configured folder's canonical form is, and which folder a given file lives
under. Both are answered by delegating to the storage that owns the path.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional, Sequence

from kalinka_plugin_sdk import paths

from ..config_model import LocalFilesConfig
from .base import FileStorage
from .local import LocalStorage
from .locator import SMB_SCHEME, LocatorError, root_of, scheme_of
from .unavailable import UnavailableStorage

logger = logging.getLogger(__name__.split(".")[-1])


class StorageResolver:
    """Maps a location to the storage that can read it.

    @note Depends only on :class:`FileStorage`. The one concrete type it
        names is :class:`UnavailableStorage`, because the contract is that
        every location resolves to something: a folder naming a protocol
        nobody handles has to report *why* rather than raise past the caller.
    """

    def __init__(self, storages: Sequence[FileStorage]) -> None:
        self._storages = tuple(storages)

    @property
    def storages(self) -> tuple[FileStorage, ...]:
        return self._storages

    def for_path(self, path: str) -> FileStorage:
        """The storage that speaks for ``path``. Never raises and never
        returns None."""
        for storage in self._storages:
            if storage.handles(path):
                return storage
        scheme = scheme_of(path)
        return UnavailableStorage(
            scheme,
            f"'{scheme}://' is not a protocol this module can read; use a "
            "local path or smb://host/share",
        )

    def canonical_roots(self, folders: Iterable[str]) -> list[str]:
        """The configured music folders in the one spelling everything else
        compares against.

        A folder that cannot be parsed is kept as written rather than
        dropped: it still has to appear as a root so the settings page can
        say what is wrong with it, and so nothing indexed under it is
        purged in the meantime.
        """
        roots: list[str] = []
        for raw in folders:
            if not raw or not raw.strip():
                continue
            try:
                roots.append(self.for_path(raw).canonical(raw))
            except LocatorError as e:
                logger.warning("Music folder %s cannot be used: %s", raw, e)
                roots.append(raw.strip())
        return roots

    def root_of(self, path: str, roots: Iterable[str]) -> Optional[str]:
        """The configured root ``path`` lies under, matched textually —
        indexed paths are derived from these roots, so no round trip is
        needed to recognise one."""
        return root_of(path, roots)

    def within_roots(self, path: str, roots: Iterable[str]) -> bool:
        """Whether ``path`` is inside the access boundary.

        Asked of the path's own storage, because containment is
        protocol-specific: a local symlink pointing out of a music folder is
        outside it, while a share path is compared as written.
        """
        return self.for_path(path).contains(path, roots)


def build_resolver(config: LocalFilesConfig) -> StorageResolver:
    """The resolver this module's configuration asks for.

    The only place that names concrete storage classes, so everything else
    depends on the interface alone. Called once per process that reads
    files — the indexer's, the embedder's and the module's own.
    """
    spill_dir = os.path.join(paths.cache_dir(), "storage")
    storages: list[FileStorage] = [LocalStorage()]

    try:
        from .smb import SmbCredentials, SmbStorage
    except ImportError as e:
        logger.warning("SMB music folders cannot be read: %s", e)
        storages.append(
            UnavailableStorage(
                SMB_SCHEME,
                "the SMB client library is not installed, so smb:// folders "
                "cannot be read",
            )
        )
    else:
        storages.append(
            SmbStorage(
                SmbCredentials(
                    username=config.smb.username,
                    password=config.smb.password,
                    encrypt=config.smb.encrypt,
                ),
                spill_dir=spill_dir,
            )
        )

    return StorageResolver(storages)
