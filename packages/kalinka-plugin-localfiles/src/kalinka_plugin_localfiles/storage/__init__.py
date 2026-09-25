"""Storage the library reads its music from, whatever the protocol.

:class:`FileStorage` is the interface; :class:`StorageResolver` maps a path
to the implementation that owns it. Import from here rather than from the
submodules, except in :func:`build_resolver`, which is the one place that is
allowed to know the concrete classes.
"""

from .base import (
    PROBE_TIMEOUT_S,
    ChangeKind,
    ChangeWatcher,
    DirEntry,
    FileIdentity,
    FileStat,
    FileStorage,
    RootStatus,
    WatchResult,
    storage_failure,
)
from .locator import (
    FILE_SCHEME,
    SMB_SCHEME,
    LocatorError,
    StorageLocator,
    is_within,
    media_type_of,
    parse,
    root_of,
    scheme_of,
)
from .credentials import SmbCredentials
from .resolver import StorageResolver, build_resolver
from .sources import library_locations, library_roots, share_logins, source_location
from .unavailable import UnavailableStorage

__all__ = [
    "PROBE_TIMEOUT_S",
    "ChangeKind",
    "ChangeWatcher",
    "DirEntry",
    "FILE_SCHEME",
    "FileIdentity",
    "FileStat",
    "FileStorage",
    "LocatorError",
    "RootStatus",
    "SMB_SCHEME",
    "SmbCredentials",
    "StorageLocator",
    "StorageResolver",
    "UnavailableStorage",
    "WatchResult",
    "build_resolver",
    "storage_failure",
    "is_within",
    "library_locations",
    "library_roots",
    "media_type_of",
    "parse",
    "root_of",
    "scheme_of",
    "share_logins",
    "source_location",
]
