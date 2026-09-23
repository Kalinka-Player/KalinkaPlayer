"""Where a music folder is, expressed as a protocol plus a location.

A configured music folder is either a path on this machine — including
anything the kernel has mounted there, an NFS or CIFS share as much as a
local disk — or a URL naming a service to talk to directly. The URL form is
``smb://[user@]host[:port]/share[/path]``, the spelling file managers and
``smbclient`` use; ``cifs://`` is accepted as the older name for it.

Locations are compared and joined as ordinary POSIX strings everywhere else
in this package, which is why the canonical form is built by hand here
rather than by :func:`os.path.normpath`: normalising a URL collapses the
``//`` after the scheme and silently turns a share into a directory name.
"""

from __future__ import annotations

import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

#: Locations with no scheme are paths on this machine.
FILE_SCHEME = "file"
SMB_SCHEME = "smb"

#: ``cifs`` is the same protocol under its older name.
_SMB_ALIASES = {"smb", "cifs"}

_DEFAULT_SMB_PORT = 445

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")

# To the last "@": over-masking is safe, stopping at a "/" in a password is not.
_URL_PASSWORD_RE = re.compile(r"^(\s*[A-Za-z][A-Za-z0-9+.\-]*://[^/:@]*):.*@")

#: A protocol written with one slash instead of two. Worth catching by name,
#: because it otherwise reads as a relative path and a music folder silently
#: becomes a directory beside the service's working directory.
_ONE_SLASH_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):/?(?!/)")


class LocatorError(OSError):
    """A location cannot be used as written.

    An ``OSError`` because that is what it means to every caller: the path
    names nothing reachable, and the ``except OSError`` that already guards
    each read should treat a malformed location the same way it treats an
    absent one. The message is shown to users, so it says what to write
    instead.
    """


@dataclass(frozen=True)
class StorageLocator:
    """A parsed location.

    @param scheme Protocol name, lowercased; :data:`FILE_SCHEME` for a local
        path.
    @param path Absolute path. For a remote scheme its first component is the
        share, and it is the path as the server spells it — never
        percent-decoded, so a folder really named ``100%`` still resolves.
    @param host Empty for a local path.
    @param port None when the protocol's default applies.
    @param username Taken from the URL when it carries one; the module's
        configured credentials supply it otherwise.
    """

    scheme: str
    path: str
    host: str = ""
    port: Optional[int] = None
    username: Optional[str] = None

    @property
    def is_local(self) -> bool:
        return self.scheme == FILE_SCHEME

    @property
    def components(self) -> list[str]:
        return [c for c in self.path.split("/") if c]

    @property
    def share(self) -> str:
        """The share this location is on. Empty for a local path."""
        parts = self.components
        return parts[0] if parts and not self.is_local else ""

    @property
    def share_path(self) -> str:
        """Location within the share, without leading separator. Empty at its
        root."""
        return "/".join(self.components[1:]) if not self.is_local else ""

    @property
    def authority(self) -> str:
        """``host`` or ``host:port``, as the canonical URL spells it."""
        if self.port is None:
            return self.host
        return f"{self.host}:{self.port}"

    def __str__(self) -> str:
        """The canonical form, which is what gets stored and compared."""
        if self.is_local:
            return self.path
        userinfo = f"{self.username}@" if self.username else ""
        return f"{self.scheme}://{userinfo}{self.authority}{self.path}"


def without_password(raw: str) -> str:
    """``raw`` fit for a log: a password written into its userinfo, which
    :func:`parse` refuses but a hand-edited config can still carry, is masked.
    """
    return _URL_PASSWORD_RE.sub(r"\1:<secret>@", raw)


def scheme_of(raw: str) -> str:
    """The protocol ``raw`` names, without parsing the rest of it."""
    match = _SCHEME_RE.match(raw or "")
    if match is None:
        return FILE_SCHEME
    scheme = match.group(1).lower()
    return SMB_SCHEME if scheme in _SMB_ALIASES else scheme


def parse(raw: str) -> StorageLocator:
    """Parse a configured folder or an indexed file path.

    @raise LocatorError If it is empty, malformed, names a protocol this
        module does not speak, or carries a password (which belongs in the
        module's credential setting, not in a folder list the UI displays).
    """
    raw = (raw or "").strip()
    if not raw:
        raise LocatorError("the music folder is empty")

    if raw.startswith("\\\\"):
        share = raw.strip("\\").replace("\\", "/")
        raise LocatorError(
            f"write a share as a URL, not a Windows path: smb://{share}"
        )

    match = _SCHEME_RE.match(raw)
    if match is None:
        _refuse_one_slash(raw)
        return _parse_local(raw)

    scheme = match.group(1).lower()
    if scheme not in _SMB_ALIASES:
        raise LocatorError(
            f"'{scheme}://' is not a protocol this module can read; use a "
            "local path or smb://host/share"
        )
    return _parse_smb(raw[match.end():])


def _refuse_one_slash(raw: str) -> None:
    """Reject ``smb:/host/share`` rather than read it as a relative path.

    Only a protocol this module speaks is worth second-guessing; anything
    else with a colon in it is an ordinary filename.
    """
    match = _ONE_SLASH_RE.match(raw)
    if match is None or match.group(1).lower() not in _SMB_ALIASES:
        return
    raise LocatorError(
        f"'{match.group(1)}:' needs two slashes; write "
        f"{SMB_SCHEME}://host/share"
    )


def _parse_local(raw: str) -> StorageLocator:
    try:
        resolved = str(Path(raw).expanduser().resolve())
    except (OSError, RuntimeError, ValueError) as e:
        raise LocatorError(f"{raw} is not a usable path: {e}") from e
    return StorageLocator(scheme=FILE_SCHEME, path=resolved)


def _parse_smb(rest: str) -> StorageLocator:
    authority, _, path = rest.partition("/")
    username = None
    if "@" in authority:
        userinfo, _, authority = authority.rpartition("@")
        if ":" in userinfo:
            raise LocatorError(
                "a password does not belong in a folder URL; write "
                "smb://host/share and set the user and password in the SMB "
                "credentials"
            )
        username = userinfo or None

    host, port = _split_host_port(authority)
    if not host:
        raise LocatorError(
            "name the server, e.g. smb://192.168.1.1/music or smb://nas/music"
        )

    components = []
    for component in path.split("/"):
        if not component or component == ".":
            continue
        if component == "..":
            raise LocatorError(
                "'..' does not belong in a share path; name the folder directly"
            )
        components.append(component)
    if not components:
        raise LocatorError(
            f"name the share on {host}, e.g. smb://{host}/music"
        )

    return StorageLocator(
        scheme=SMB_SCHEME,
        path="/" + "/".join(components),
        host=host,
        port=port,
        username=username,
    )


def _split_host_port(authority: str) -> tuple[str, Optional[int]]:
    """Host and explicit port. IPv6 literals keep their brackets, which is
    what makes the colon in them unambiguous."""
    if authority.startswith("["):
        host, sep, tail = authority.partition("]")
        if not sep:
            raise LocatorError(f"unbalanced '[' in {authority}")
        host = (host + sep).lower()
        port_text = tail[1:] if tail.startswith(":") else ""
    elif authority.count(":") == 1:
        host, _, port_text = authority.partition(":")
        host = host.lower()
    elif ":" in authority:
        raise LocatorError(
            f"'{authority}' has more than one colon in it; an IPv6 address "
            "goes in brackets, as smb://[fe80::1]/music"
        )
    else:
        host, port_text = authority.lower(), ""

    if not port_text:
        return host, None
    try:
        port = int(port_text)
    except ValueError as e:
        raise LocatorError(f"'{port_text}' is not a port number") from e
    if not 1 <= port <= 65535:
        raise LocatorError(f"port {port} is out of range")
    return host, None if port == _DEFAULT_SMB_PORT else port


def is_within(path: str, root: str) -> bool:
    """Whether ``path`` lies inside ``root``, on a separator boundary.

    Purely textual: both must already be canonical. ``/Music`` does not
    admit a sibling ``/Music2``, and a path equal to the root is inside it.
    The separator is ``/`` for local paths and share paths alike, which is
    what lets one implementation serve both.

    @note A root that already ends in the separator — ``/`` being the one
        that reaches here in practice — must not have a second one appended,
        or it matches nothing and every indexed file reads as being outside
        its own music folder.
    """
    if not path or not root:
        return False
    if path == root:
        return True
    return path.startswith(root if root.endswith("/") else root + "/")


def media_type_of(path: str) -> Optional[str]:
    """The media type a location's name implies, or None.

    Guessed from the last component alone, because
    :func:`mimetypes.guess_type` treats a scheme-bearing string as a URL and
    truncates it at the first ``#`` or ``?`` — which silently unnames every
    share-backed track called something like ``Symphony #5.flac``. The type
    decides which tag reader runs and what a renderer is told it is playing,
    so getting it from the name is not optional.
    """
    return mimetypes.guess_type(os.path.basename(path))[0]


def root_of(path: str, roots: Iterable[str]) -> Optional[str]:
    """The configured root ``path`` lies under, or None.

    Matched textually, because indexed paths are derived from these roots
    and so need no stat to be recognised.
    """
    for root in roots:
        if is_within(path, root):
            return root
    return None
