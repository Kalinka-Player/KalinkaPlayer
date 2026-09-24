"""Where the configured music is, as the locations the storages read.

A music source is a record: a local source becomes its folder, an SMB source
the ``smb://`` URL :func:`.parse` reads, and the login that share is read
with comes from the source alone. The music folders are only the local
sources again, as older apps see them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config_model import (
    AccountSignIn,
    LocalFilesConfig,
    LocalSource,
    MusicSource,
    SmbLocation,
    SmbSource,
)
from .credentials import SmbCredentials
from .locator import SMB_SCHEME, LocatorError, parse

if TYPE_CHECKING:
    from .resolver import StorageResolver


def source_location(source: MusicSource) -> str:
    """Where ``source`` is: canonical when it parses, as written otherwise,
    so a misspelt one still becomes a root that says what is wrong with it."""
    if isinstance(source, LocalSource):
        return source.location.path
    url = _share_url(source.location)
    try:
        return str(parse(url))
    except LocatorError:
        return url


def _share_url(location: SmbLocation) -> str:
    host = location.host.strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return "/".join([f"{SMB_SCHEME}://{host}:{location.port}", *location.parts()])


def library_locations(config: LocalFilesConfig) -> list[str]:
    """Every configured root as written."""
    return [source_location(source) for source in config.music_sources]


def library_roots(config: LocalFilesConfig, resolver: "StorageResolver") -> list[str]:
    """Every configured root in canonical form, each once.

    @note Resolves local folders, so it may block on a hung mount.
    """
    return resolver.canonical_roots(library_locations(config))


def share_logins(config: LocalFilesConfig) -> dict[str, SmbCredentials]:
    """The login each share a music source names is read with, keyed by its
    location."""
    return {
        source_location(source): _credentials(source)
        for source in config.music_sources
        if isinstance(source, SmbSource)
    }


def _credentials(source: SmbSource) -> SmbCredentials:
    sign_in = source.authentication
    encrypt = source.options.require_encryption
    if isinstance(sign_in, AccountSignIn):
        return SmbCredentials(
            username=sign_in.username, password=sign_in.password, encrypt=encrypt
        )
    return SmbCredentials(encrypt=encrypt)
