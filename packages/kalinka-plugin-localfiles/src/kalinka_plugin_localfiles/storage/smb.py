"""Files on an SMB share, read over the protocol rather than through a mount.

Mounting a share needs privileges this service does not have and will not be
given: the unit runs with ``NoNewPrivileges``, which is exactly what stops
the setuid helper every FUSE filesystem needs. Speaking SMB2/3 from inside
the process needs nothing but a socket, and a share added this way needs no
root, no ``fstab`` entry and no packages on the appliance.

Shares the kernel *has* mounted are not this storage's business — they are
ordinary paths, and :class:`~.local.LocalStorage` reads them.

Locations are written ``smb://host[:port]/share/path``. How to sign in comes
from the music source that names the share, never from the URL.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from stat import S_ISDIR
from typing import Any, BinaryIO, Iterable, Iterator, Optional

import smbclient
from smbprotocol.exceptions import (
    AccessDenied,
    LogonFailure,
    PasswordExpired,
    SMBAuthenticationError,
)

from .base import (
    DirEntry,
    FileIdentity,
    FileStat,
    FileStorage,
    RootStatus,
)
from .credentials import SmbCredentials
from .locator import (
    SMB_SCHEME,
    LocatorError,
    StorageLocator,
    is_within,
    parse,
    scheme_of,
)

#: Read-ahead per request. A tag read walks a file's header and the audio
#: embedder seeks to a few fragments, so the win is in asking for a chunk
#: worth having rather than a block at a time over the network.
_READ_BUFFER = 1024 * 1024

#: Long enough for a NAS that has spun its disks down, short enough that an
#: address with nothing behind it fails a scan rather than stalling it.
_CONNECT_TIMEOUT_S = 15

#: What the backend raises when a server answers and turns the login down.
#: Separated from the transport failures because the two read nothing alike
#: to someone who has just typed a password.
_REFUSED_LOGIN = (
    SMBAuthenticationError,
    LogonFailure,
    PasswordExpired,
    AccessDenied,
)

#: Who an unconfigured share logs in as. A username is not optional: the
#: session pool matches sessions on it, and omitting it would both fail to
#: authenticate and silently adopt whichever session a differently
#: credentialed share had already opened to the same server.
GUEST_USERNAME = "guest"

#: Readers must not lock each other out. The server holds a stream open for
#: the length of a response while the indexer may be reading the same track,
#: and ``smbclient`` defaults to a deny-all open.
_SHARE_ACCESS = "rwd"


class SmbStorage(FileStorage):
    """An SMB client presented as a filesystem.

    Connections and sessions are pooled per server and credential, so
    repeated calls cost one round trip rather than a new logon, and nothing
    here needs to be closed between files.

    The pool is this instance's own rather than ``smbclient``'s global one,
    because the global one is keyed on server and username and hands back a
    matching session without re-checking the password. Two storages built
    from different credentials would then answer for each other: a password
    the user has typed but not saved would be judged against the session
    playback is already using and pronounced good whatever was typed, and
    asking one of them for encryption would turn it on for the other, which
    the protocol does not allow turning back off.

    @note Holding a pool is what makes :meth:`close` necessary — see there.
    @note Every operation translates backend failures into ``OSError``,
        including authentication and transport ones, because that is what
        this interface promises and what each caller's ``except OSError``
        already handles.
    """

    def __init__(
        self,
        credentials: Optional[SmbCredentials] = None,
        spill_dir: Optional[str] = None,
    ) -> None:
        super().__init__(spill_dir=spill_dir)
        self._credentials = credentials or SmbCredentials()
        self._connections: dict[str, Any] = {}
        self._logon_locks: dict[tuple[str, Optional[int]], threading.Lock] = {}
        self._logon_locks_guard = threading.Lock()

    def close(self) -> None:
        """Disconnect every server this storage logged in to.

        Each connection owns a socket and the thread reading it, so a
        storage replaced when the credentials changed takes them with it
        rather than leaving one set behind per password typed.
        """
        smbclient.reset_connection_cache(
            connection_cache=self._connections, fail_on_error=False
        )

    @property
    def scheme(self) -> str:
        return SMB_SCHEME

    def handles(self, path: str) -> bool:
        return scheme_of(path) == SMB_SCHEME

    def canonical(self, path: str) -> str:
        return str(parse(path))

    def contains(self, path: str, roots: Iterable[str]) -> bool:
        """Textual, on canonical URLs. There is no symlink to resolve: this
        storage never follows a reparse point, and ``..`` is refused when the
        location is parsed."""
        try:
            canonical = str(parse(path))
        except LocatorError:
            return False
        return any(is_within(canonical, root) for root in roots)

    def listdir(self, path: str) -> list[DirEntry]:
        locator = parse(path)
        with self._as_os_error():
            return [
                self._entry(locator, child)
                for child in smbclient.scandir(
                    self._unc(locator), **self._logon(locator)
                )
            ]

    def stat(self, path: str) -> FileStat:
        locator = parse(path)
        with self._as_os_error():
            info = smbclient.stat(self._unc(locator), **self._logon(locator))
        return FileStat(
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            is_dir=S_ISDIR(info.st_mode),
            identity=self._identity(info),
        )

    def open(self, path: str) -> BinaryIO:
        locator = parse(path)
        with self._as_os_error():
            return smbclient.open_file(
                self._unc(locator),
                mode="rb",
                buffering=_READ_BUFFER,
                share_access=_SHARE_ACCESS,
                **self._logon(locator),
            )

    def probe_root_blocking(self, root: str) -> RootStatus:
        try:
            locator = parse(root)
        except LocatorError as e:
            return self.unavailable(root, str(e))

        unc = self._unc(locator)
        try:
            with self._as_os_error():
                info = smbclient.stat(unc, **self._logon(locator))
                if not S_ISDIR(info.st_mode):
                    return self.unavailable(
                        root, "it names a file rather than a folder"
                    )
        except OSError as e:
            return self.unavailable(root, self._reason(locator, e))

        return RootStatus(
            root=root,
            available=True,
            reason="",
            fs_type=SMB_SCHEME,
            is_network=True,
            is_autofs=False,
            identity=f"smb //{locator.authority}/{locator.share} {info.st_dev}",
        )

    def _identity(self, info) -> Optional[FileIdentity]:
        """The volume serial and the server's file index, or None.

        A rename inside a share keeps both, which is what lets a moved file
        keep its library identity. Servers that supply no file index report
        zero for every file, so a zero has to read as "no identity" — shared
        between files it would make each one look like a rename of the last.
        The scheme prefix keeps a volume serial from colliding with a local
        ``st_dev``, since the library looks identities up in one table.
        """
        if not info.st_dev or not info.st_ino:
            return None
        return FileIdentity(
            device=f"{SMB_SCHEME}:{info.st_dev}", inode=str(info.st_ino)
        )

    def _entry(self, locator: StorageLocator, child) -> DirEntry:
        """A listing row, built from what the directory query already
        returned — no stat per entry, which is what keeps a folder of
        artwork to one round trip.

        @note ``follow_symlinks=False`` is the contract, and here it also
            keeps the listing to that one round trip: resolving a reparse
            point costs a further query that can fail on its own and take
            the whole directory's music down with it.
        """
        return DirEntry(
            name=child.name,
            path=f"{locator}/{child.name}",
            is_dir=child.is_dir(follow_symlinks=False),
            size=child.smb_info.end_of_file,
        )

    def _unc(self, locator: StorageLocator) -> str:
        r"""The ``\\host\share\path`` form ``smbclient`` takes. The port is
        not part of it; it rides in the session arguments instead."""
        host = locator.host.strip("[]")
        return "\\\\" + "\\".join([host, *locator.components])

    def _logon(self, locator: StorageLocator) -> dict[str, Any]:
        """Session arguments for one location, with the logon already made.

        ``smbclient`` fills a connection and session pool with an
        unsynchronised check-then-create, so two threads reaching one server
        at once each build a connection and the loser's socket and reader
        thread leak. Logging in under a lock per server leaves the pool to
        one thread at a time.

        Before every operation, not once: registering an existing session is
        a pair of dictionary lookups, and it is also what rebuilds a pooled
        connection the server has since dropped.

        @note The lock is per server, not per account: the race is for the
            connection underneath the session.
        """
        session = self._session(locator)
        server = locator.host.strip("[]")
        with self._logon_locks_guard:
            lock = self._logon_locks.setdefault(
                (server, locator.port), threading.Lock()
            )
        with lock:
            smbclient.register_session(server, **session)
        return session

    def _session(self, locator: StorageLocator) -> dict[str, Any]:
        """Connection and credential arguments for one location.

        A username is always sent: ``smbclient`` pools sessions per server
        and picks the first one when asked for no particular user, so a
        share left to the guest default would read as whoever logged in
        first.

        ``encrypt`` is only sent when it is wanted. It is tri-state in
        ``smbclient``, where an explicit False means *force encryption off*
        and is refused on a session a server has already encrypted.
        """
        session: dict[str, Any] = {
            "connection_cache": self._connections,
            "connection_timeout": _CONNECT_TIMEOUT_S,
            "username": self._credentials.username or GUEST_USERNAME,
            "password": self._credentials.password,
        }
        if self._credentials.encrypt:
            session["encrypt"] = True
        if locator.port is not None:
            session["port"] = locator.port
        return session

    def _reason(self, locator: StorageLocator, error: OSError) -> str:
        """Why a root is unavailable, in words a user can act on."""
        if isinstance(error, PermissionError):
            return (
                f"{locator.host} refused the login for share "
                f"'{locator.share}' ({error})"
            )
        return (
            f"{locator.host} did not answer for share "
            f"'{locator.share}' ({error})"
        )

    @contextmanager
    def _as_os_error(self) -> Iterator[None]:
        """Present every backend failure as an ``OSError``.

        ``smbclient`` raises ``OSError`` for what a filesystem would, but
        authentication, negotiation and transport failures come out as its
        own exception types, and one of those escaping would abort a scan
        over a single unreachable share. Only the cause is carried over:
        every caller already names the path it was reading.

        A refused login becomes ``PermissionError`` rather than a plain
        ``OSError``, because it is the one failure here the user can do
        something about and it reads nothing like a server that is off.
        It is still an ``OSError``, so no caller has to know that.
        """
        try:
            yield
        except OSError:
            raise
        except _REFUSED_LOGIN as e:
            raise PermissionError(str(e)) from e
        except Exception as e:
            raise OSError(str(e)) from e
