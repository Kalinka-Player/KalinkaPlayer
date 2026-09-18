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
from .resolver import StorageResolver, build_resolver
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
    "StorageLocator",
    "StorageResolver",
    "UnavailableStorage",
    "WatchResult",
    "build_resolver",
    "is_within",
    "media_type_of",
    "parse",
    "root_of",
    "scheme_of",
]
