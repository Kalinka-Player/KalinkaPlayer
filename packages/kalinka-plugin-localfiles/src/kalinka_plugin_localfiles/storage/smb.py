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

import hashlib
import hmac
import io
import os
import secrets
import threading
import time
from contextlib import contextmanager
from stat import S_ISDIR
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Optional

import smbclient
from smbprotocol.exceptions import (
    SMBAuthenticationError,
    SMBConnectionClosed,
    SMBException,
    SMBOSError,
    SMBResponseException,
)
from smbprotocol.tree import TreeConnect

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

#: How long a connection waits on a silent server before sending it an echo,
#: and how long the echo then has. ``smbprotocol`` reads it as each
#: connection is made; its own ten minutes would stall a scan or a stream for
#: twenty on a NAS that has gone, where this fails them within thirty seconds
#: and still gives one spinning its disks up time to answer.
_RECEIVE_TIMEOUT_S = 15
os.environ.setdefault(
    "SMB_EXPERIMENTAL_TRANSPORT_RECEIVE_TIMEOUT", str(_RECEIVE_TIMEOUT_S)
)

#: Read-ahead per request. A tag read walks a file's header and the audio
#: embedder seeks to a few fragments, so the win is in asking for a chunk
#: worth having rather than a block at a time over the network.
_READ_BUFFER = 1024 * 1024

#: Long enough for a NAS that has spun its disks down, short enough that an
#: address with nothing behind it fails a scan rather than stalling it.
_CONNECT_TIMEOUT_S = 15

#: Who an unconfigured share logs in as. A username is not optional: the
#: session pool matches sessions on it, and omitting it would both fail to
#: authenticate and silently adopt whichever session a differently
#: credentialed share had already opened to the same server.
GUEST_USERNAME = "guest"

#: Readers must not lock each other out. The server holds a stream open for
#: the length of a response while the indexer may be reading the same track,
#: and ``smbclient`` defaults to a deny-all open.
_SHARE_ACCESS = "rwd"

#: How long a refused login is answered from memory instead of being sent
#: again. Windows locks an account, and a Synology blocks the address, after
#: about ten failures in this long.
_REFUSAL_MEMORY_S = 600

#: Statuses a server turns a login down with, and what each means to the
#: person who typed it.
_LOGIN_REFUSALS = {
    0xC000006D: "the user name or password is wrong",
    0xC0000234: "the account is locked after too many failed logins",
    0xC0000072: "the account is disabled",
    0xC0000193: "the account has expired",
    0xC0000071: "the password has expired",
    0xC0000224: "the password has to be changed first",
    0xC000006E: "the account may not sign in from here",
    0xC000006F: "the account may not sign in at this time of day",
    0xC0000070: "the account may not sign in from this device",
    0xC000015B: "the account may not sign in over the network",
}
_STATUS_ACCESS_DENIED = 0xC0000022
_STATUS_OBJECT_NAME_INVALID = 0xC0000033
_MISSING_SHARE = {0xC00000CC, 0xC0000225}
_MISSING_PATH = {0xC0000034, 0xC000003A, 0xC000000F}

#: What ``smbprotocol`` says when a server hands out a guest session where
#: signing or encryption is required, which a guest has no key for.
_GUEST_SESSION = "authenticated as a guest"


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
    @note Every operation, and every read from a file it opened, translates
        backend failures into ``OSError`` worded for the person reading it:
        ``PermissionError`` for a refused login or access, and
        ``FileNotFoundError`` for a path the share does not have. Each
        caller's ``except OSError`` then already handles them.
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
        with self._as_os_error(locator):
            return [
                self._entry(locator, child)
                for child in smbclient.scandir(
                    self._unc(locator), **self._logon(locator)
                )
            ]

    def stat(self, path: str) -> FileStat:
        locator = parse(path)
        with self._as_os_error(locator):
            info = smbclient.stat(self._unc(locator), **self._logon(locator))
        return FileStat(
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            is_dir=S_ISDIR(info.st_mode),
            identity=self._identity(info),
        )

    def open(self, path: str) -> BinaryIO:
        locator = parse(path)
        with self._as_os_error(locator):
            handle = smbclient.open_file(
                self._unc(locator),
                mode="rb",
                buffering=_READ_BUFFER,
                share_access=_SHARE_ACCESS,
                **self._logon(locator),
            )
        return _SmbFile(handle, lambda: self._as_os_error(locator))

    def probe_root_blocking(self, root: str) -> RootStatus:
        try:
            locator = parse(root)
        except LocatorError as e:
            return self.unavailable(root, str(e))

        unc = self._unc(locator)
        try:
            with self._as_os_error(locator):
                info = smbclient.stat(unc, **self._logon(locator))
                if not S_ISDIR(info.st_mode):
                    return self.unavailable(
                        root, "it names a file rather than a folder"
                    )
        except OSError as e:
            return self.unavailable(root, str(e))

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

    @property
    def _guest(self) -> bool:
        return not self._credentials.username

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

        A login the server has lately refused is not sent again (see
        :class:`_RefusedLogins`): the scan, playback and the renderer all
        retry, and each retry would count against the account.

        @note The lock is per server, not per account: the race is for the
            connection underneath the session.
        """
        session = self._session(locator)
        server = locator.host.strip("[]")
        refusal = (
            server.lower(),
            locator.port,
            session["username"].casefold(),
            _REFUSALS.digest(session["password"]),
        )
        _REFUSALS.check(refusal)
        with self._logon_locks_guard:
            lock = self._logon_locks.setdefault(
                (server, locator.port), threading.Lock()
            )
        with lock, self._as_os_error(locator, logon=True, refusal=refusal):
            registered = smbclient.register_session(server, **session)
            if self._guest:
                self._connect_guest_tree(registered, server, locator.share)
        return session

    def _session(self, locator: StorageLocator) -> dict[str, Any]:
        """Connection and credential arguments for one location.

        A username is always sent: ``smbclient`` pools sessions per server
        and picks the first one when asked for no particular user, so a
        share left to the guest default would read as whoever logged in
        first.

        A guest session is not signed: the server gives a guest no key to
        sign with, and one that insists on signing refuses guests outright.

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
        if self._guest:
            session["require_signing"] = False
        if self._credentials.encrypt:
            session["encrypt"] = True
        if locator.port is not None:
            session["port"] = locator.port
        return session

    @staticmethod
    def _connect_guest_tree(session, server: str, share: str) -> None:
        r"""Connect a guest session to its share, unverified.

        ``smbclient`` checks an SMB 3.0 negotiation against a signature, which
        a guest session cannot make, and only offers turning that off for the
        whole process. Connecting the share here, once per session, leaves
        the check on for every signed-in one: ``smbclient`` reuses a tree
        already connected for ``\\server\share``.
        """
        name = rf"\\{server}\{share}"
        if any(t.share_name == name for t in session.tree_connect_table.values()):
            return
        TreeConnect(session, name).connect(require_secure_negotiate=False)

    def _explain(
        self, locator: StorageLocator, error: BaseException, logon: bool
    ) -> OSError:
        """``error`` as the ``OSError`` a caller gets, worded for the person
        who will read it — most of these end up in front of whoever typed the
        share and the login."""
        host, share = locator.host, locator.share
        user = self._credentials.username or GUEST_USERNAME
        status = _status_of(error)
        if status in _LOGIN_REFUSALS:
            return PermissionError(
                f"{host} refused the login as '{user}' for share '{share}': "
                f"{_LOGIN_REFUSALS[status]}"
            )
        text = _text(error)
        if _GUEST_SESSION in text:
            if not self._guest:
                return PermissionError(
                    f"{host} signed in as a guest instead of as '{user}', "
                    "so it does not know that user name"
                )
            if self._credentials.encrypt:
                return PermissionError(
                    f"a guest cannot encrypt: turn Require encryption off for "
                    f"{host}, or sign in with an account"
                )
            return PermissionError(
                f"{host} requires signed traffic, which a guest cannot send: "
                "sign in with an account"
            )
        if isinstance(error, SMBAuthenticationError):
            return PermissionError(
                f"{host} refused the login as '{user}' for share '{share}' ({text})"
            )
        if status == _STATUS_ACCESS_DENIED:
            return PermissionError(
                f"'{user}' may not read {self._inside(locator)} on {host}"
            )
        if status in _MISSING_SHARE:
            return OSError(f"{host} has no share named '{share}'")
        if status in _MISSING_PATH:
            return FileNotFoundError(f"{host} has no {self._inside(locator)}")
        if status == _STATUS_OBJECT_NAME_INVALID:
            return OSError(
                f"{host} will not open {self._inside(locator)}: the name holds "
                "a character it does not allow"
            )
        if logon and _connection_closed(error, text):
            return OSError(
                f"{host} closed the connection when asked for SMB2 or later; "
                "if it only offers SMB1, turn SMB2 or SMB3 on in its settings"
            )
        if _connection_closed(error, text):
            return OSError(f"{host} dropped the connection ({text})")
        if status is not None:
            return OSError(
                f"{host} turned down a request for share '{share}' ({text})"
            )
        return OSError(f"{host} did not answer for share '{share}' ({text})")

    @staticmethod
    def _inside(locator: StorageLocator) -> str:
        """What a location names within its share, for a message."""
        folders = locator.components[1:]
        if not folders:
            return f"share '{locator.share}'"
        return f"'{'/'.join(folders)}' in share '{locator.share}'"

    @contextmanager
    def _as_os_error(
        self,
        locator: StorageLocator,
        logon: bool = False,
        refusal: Optional[tuple] = None,
    ) -> Iterator[None]:
        """Present every backend failure as an ``OSError`` (see the class).

        ``smbclient`` raises ``OSError`` for what a filesystem would, but
        authentication, negotiation and transport failures come out as its
        own exception types, and one of those escaping would abort a scan
        over a single unreachable share.

        @param logon Whether the failure was in making the session, where a
            closed connection means a server that will not speak SMB2.
        @param refusal The login to remember as refused, if this was one.
        """
        try:
            yield
        except OSError as e:
            if isinstance(e, SMBOSError):
                raise self._explain(locator, e, logon) from e
            raise
        except SMBException as e:
            explained = self._explain(locator, e, logon)
            if refusal is not None and _refuses_login(e):
                _REFUSALS.refused(refusal, str(explained))
            raise explained from e
        except Exception as e:
            raise self._explain(locator, e, logon) from e
        else:
            if refusal is not None:
                _REFUSALS.forget(refusal)


class _SmbFile(io.RawIOBase):
    """A file opened on a share, whose reads fail as ``OSError`` too.

    The file ``smbclient`` hands back raises the protocol's own exceptions
    from ``read`` and ``seek``, long after the open that was translated. Tag
    readers, the audio embedder and the server's stream all catch
    ``OSError``, and one of those escaping as anything else reads as a
    broken file rather than a share that went away.
    """

    def __init__(
        self, handle: BinaryIO, translate: Callable[[], Any]
    ) -> None:
        super().__init__()
        self._handle = handle
        self._translate = translate

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        with self._translate():
            return self._handle.read(size)

    def readinto(self, buffer) -> int:
        with self._translate():
            return self._handle.readinto(buffer)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        with self._translate():
            return self._handle.seek(offset, whence)

    def tell(self) -> int:
        with self._translate():
            return self._handle.tell()

    def close(self) -> None:
        """Closing a handle on a connection that has gone cannot fail the
        read that already finished: the server dropped the handle with it."""
        if self.closed:
            return
        try:
            self._handle.close()
        except Exception:
            pass
        finally:
            super().close()


class _RefusedLogins:
    """Logins a server has lately turned down, so they are not sent again.

    Each attempt with a wrong password counts against the account, and a NAS
    or a Windows host locks it, or blocks this address, after about ten. The
    library retries by design — a scan probes its roots again, a track that
    cannot be read is asked for again, the renderer retries a stream — so a
    refusal is remembered and answered without the network until it expires
    or a login with the same credential works. A password typed anew is a
    different credential and is always tried.

    @note Process-wide, because each storage holds one login but several may
        hold the same one. Thread-safe. Passwords are kept only as a keyed
        digest, with a key that dies with the process.
    """

    def __init__(self, memory_s: float = _REFUSAL_MEMORY_S) -> None:
        self._memory_s = memory_s
        self._key = secrets.token_bytes(16)
        self._refused: dict[tuple, tuple[float, float, str]] = {}
        self._lock = threading.Lock()

    def digest(self, password: str) -> bytes:
        return hmac.new(self._key, password.encode(), hashlib.sha256).digest()

    def check(self, login: tuple) -> None:
        """@raise PermissionError If ``login`` was refused lately."""
        with self._lock:
            entry = self._refused.get(login)
            if entry is None:
                return
            expires, retry_at, reason = entry
            if time.monotonic() >= expires:
                del self._refused[login]
                return
        raise PermissionError(
            f"{reason}; not tried again before "
            f"{time.strftime('%H:%M', time.localtime(retry_at))}, so that the "
            "account is not locked out"
        )

    def refused(self, login: tuple, reason: str) -> None:
        with self._lock:
            self._refused[login] = (
                time.monotonic() + self._memory_s,
                time.time() + self._memory_s,
                reason,
            )

    def forget(self, login: tuple) -> None:
        with self._lock:
            self._refused.pop(login, None)


_REFUSALS = _RefusedLogins()


def _status_of(error: BaseException) -> Optional[int]:
    """The NT status behind ``error``, wherever the backend put it."""
    if isinstance(error, SMBOSError):
        return error.ntstatus
    if isinstance(error, SMBResponseException):
        return error.status
    return None


def _refuses_login(error: BaseException) -> bool:
    """Whether the server turned the login itself down, as opposed to the
    share it leads to — only the first counts against the account."""
    return (
        _status_of(error) in _LOGIN_REFUSALS
        or isinstance(error, SMBAuthenticationError)
        or _GUEST_SESSION in _text(error)
    )


def _connection_closed(error: BaseException, text: str) -> bool:
    return isinstance(error, SMBConnectionClosed) or "socket was closed" in text


def _text(error: BaseException) -> str:
    """What ``error`` says. A response error renders the body the server
    sent, and one that arrived without a usable body cannot render at all."""
    try:
        return str(error)
    except Exception:
        return type(error).__name__
