"""Which storage a location belongs to.

The library holds paths, not protocols. Everything that reads a file asks
the resolver which storage speaks for it and then uses that storage's
methods — so adding a protocol means adding a :class:`FileStorage` and
listing it in :func:`build_resolver`, and nothing that reads files changes.

The resolver also owns the two questions that span every storage: what a
configured folder's canonical form is, and which folder a given file lives
under. Both are answered by delegating to the storage that owns the path.

A protocol is not always enough to pick the storage. Two SMB sources may sign
in as different accounts, so a share path goes to the storage of the source
whose root holds it, and one no source holds is read by nobody.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Mapping, Optional, Sequence

from kalinka_plugin_sdk import paths

from ..config_model import LocalFilesConfig
from .base import FileStorage
from .credentials import SmbCredentials
from .local import LocalStorage
from .locator import (
    SMB_SCHEME,
    LocatorError,
    is_within,
    parse,
    root_of,
    scheme_of,
    without_password,
)
from .sources import share_logins
from .unavailable import UnavailableStorage

logger = logging.getLogger(__name__.split(".")[-1])


class StorageResolver:
    """Maps a location to the storage that can read it.

    @param storages Tried in order for a path under none of ``roots``; the
        first whose protocol matches reads it.
    @param roots The storage that reads under each configured root, for a
        protocol where the location alone does not say how to sign in.
    @note Depends only on :class:`FileStorage`. The one concrete type it
        names is :class:`UnavailableStorage`, because the contract is that
        every location resolves to something: a folder naming a protocol
        nobody handles has to report *why* rather than raise past the caller.
    """

    def __init__(
        self,
        storages: Sequence[FileStorage],
        roots: Optional[Mapping[str, FileStorage]] = None,
    ) -> None:
        self._storages = tuple(storages)
        self._roots = dict(roots or {})
        self._unhandled: dict[str, FileStorage] = {}

    @property
    def storages(self) -> tuple[FileStorage, ...]:
        held = [*self._storages, *self._roots.values(), *self._unhandled.values()]
        return tuple(dict.fromkeys(held))

    def for_path(self, path: str) -> FileStorage:
        """The storage that speaks for ``path``. Never raises and never
        returns None.

        @note One instance per unhandled scheme, not one per call: callers
            group paths by storage object, and the registry that holds a
            hung root to a single probe lives on the instance.
        """
        routed = self._storage_of_root(path)
        if routed is not None:
            return routed
        for storage in self._storages:
            if storage.handles(path):
                return storage
        scheme = scheme_of(path)
        return self._unhandled.setdefault(
            scheme,
            UnavailableStorage(
                scheme,
                f"'{scheme}://' is not a protocol this module can read; use a "
                "local path or smb://host/share",
            ),
        )

    def _storage_of_root(self, path: str) -> Optional[FileStorage]:
        """The storage of the innermost root holding ``path``, so a source
        nested inside another is read with its own login."""
        holding = [root for root in self._roots if is_within(path, root)]
        if not holding:
            return None
        return self._roots[max(holding, key=len)]

    def close(self) -> None:
        """Release every storage behind it. See :meth:`FileStorage.close`."""
        for storage in self.storages:
            try:
                storage.close()
            except Exception as e:  # noqa: BLE001 — teardown, never fatal
                logger.warning("Releasing %s storage failed: %s", storage.scheme, e)

    def canonical_roots(self, folders: Iterable[str]) -> list[str]:
        """The configured music folders in the one spelling everything else
        compares against.

        A folder that cannot be parsed is kept as written, bar any password,
        rather than dropped: it still has to appear as a root so the settings
        page can say what is wrong with it, and so nothing indexed under it
        is purged in the meantime.
        """
        roots: list[str] = []
        for raw in folders:
            if not raw or not raw.strip():
                continue
            try:
                root = self.for_path(raw).canonical(raw)
            except LocatorError as e:
                shown = without_password(raw)
                # The error may quote what it misread, password included.
                reason = e if shown == raw else "a password is written into it"
                logger.warning("Music folder %s cannot be used: %s", shown, reason)
                root = shown.strip()
            if root not in roots:
                roots.append(root)
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


def _share_root(path: str) -> str:
    return str(parse(path))


def build_resolver(config: LocalFilesConfig) -> StorageResolver:
    """The resolver this module's configuration asks for.

    The only place that names concrete storage classes, so everything else
    depends on the interface alone. Called once per process that reads
    files — the indexer's, the embedder's and the module's own.

    @note Sources that sign in the same way share one storage, and with it
        one connection per server.
    """
    spill_dir = os.path.join(paths.cache_dir(), "storage")
    logins = share_logins(config)

    try:
        from .smb import SmbStorage
    except ImportError as e:
        logger.warning("SMB music sources cannot be read: %s", e)
        missing = UnavailableStorage(
            SMB_SCHEME,
            "the SMB client library is not installed, so SMB shares cannot "
            "be read",
            canonicalize=_share_root,
        )
        return StorageResolver(
            [LocalStorage(), missing], {root: missing for root in logins}
        )

    unrouted = UnavailableStorage(
        SMB_SCHEME,
        "no music source is set up for this share",
        canonicalize=_share_root,
    )
    by_login: dict[SmbCredentials, FileStorage] = {}
    for credentials in logins.values():
        if credentials not in by_login:
            by_login[credentials] = SmbStorage(credentials, spill_dir=spill_dir)
    return StorageResolver(
        [LocalStorage(), unrouted],
        {root: by_login[credentials] for root, credentials in logins.items()},
    )
