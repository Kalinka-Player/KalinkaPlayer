#!/usr/bin/env python3
"""Reading a share over SMB, without mounting it.

What the storage owes the rest of the module: locations translated into the
UNC form the client takes, credentials and port carried along, a listing that
costs one round trip, an identity stable enough for move detection, and —
above all — every backend failure arriving as an ``OSError``. An
authentication error escaping as itself would abort a scan over one
unreachable share.
"""

import io
import os
import stat
import threading
import time
from types import SimpleNamespace

import pytest
from smbprotocol.exceptions import (
    AccessDenied,
    LogonFailure,
    PasswordExpired,
    SMBAuthenticationError,
)

import kalinka_plugin_localfiles.storage.smb as smb_mod
from kalinka_plugin_localfiles.storage.locator import LocatorError, parse
from kalinka_plugin_localfiles.storage.smb import SmbCredentials, SmbStorage


class _Entry:
    """One row of a directory query, with what that query already returned.

    ``is_dir`` mirrors ``smbclient``: it takes ``follow_symlinks``, defaulting
    to True as the real client does, and a reparse point is a directory only
    when the target is followed.
    """

    def __init__(self, name, is_dir=False, size=0, link_to_dir=False):
        self.name = name
        self._is_dir = is_dir
        self._link_to_dir = link_to_dir
        self.smb_info = SimpleNamespace(end_of_file=size)

    def is_dir(self, follow_symlinks=True):
        if self._link_to_dir:
            return follow_symlinks
        return self._is_dir


class _FakeSmbClient:
    """An in-memory share, recording what it was asked and with what."""

    def __init__(self, listings=None, files=None, stats=None, failure=None):
        self.listings = listings or {}
        self.files = files or {}
        self.stats = stats or {}
        self.failure = failure
        self.calls = []
        self.logons = []
        self.session = SimpleNamespace(tree_connect_table={})

    def register_session(self, server, **kwargs):
        """What the real client pools a connection and a session under. It is
        also where a logon fails, so a configured failure surfaces here."""
        self.logons.append((server, kwargs))
        if self.failure is not None:
            raise self.failure
        return self.session

    def _record(self, unc, kwargs):
        self.calls.append((unc, kwargs))
        if self.failure is not None:
            raise self.failure

    def scandir(self, unc, **kwargs):
        self._record(unc, kwargs)
        if unc not in self.listings:
            raise OSError(f"no such directory: {unc}")
        return iter(self.listings[unc])

    def stat(self, unc, **kwargs):
        self._record(unc, kwargs)
        if unc in self.stats:
            return self.stats[unc]
        if unc in self.listings:
            return _stat_result(is_dir=True)
        if unc in self.files:
            return _stat_result(size=len(self.files[unc]))
        raise OSError(f"no such path: {unc}")

    def open_file(self, unc, mode="rb", buffering=-1, share_access=None,
                  **kwargs):
        self._record(
            unc,
            dict(
                kwargs,
                mode=mode,
                buffering=buffering,
                share_access=share_access,
            ),
        )
        if unc not in self.files:
            raise OSError(f"no such file: {unc}")
        return io.BytesIO(self.files[unc])


def _stat_result(is_dir=False, size=0, mtime_ns=1_700_000_000_000_000_000,
                 dev=305419896, ino=42):
    return SimpleNamespace(
        st_mode=stat.S_IFDIR if is_dir else stat.S_IFREG,
        st_size=size,
        st_mtime_ns=mtime_ns,
        st_dev=dev,
        st_ino=ino,
    )


class _FakeTreeConnect:
    """A share connected by hand, as the storage does for a guest."""

    connected = []

    def __init__(self, session, share_name):
        self.session = session
        self.share_name = share_name

    def connect(self, require_secure_negotiate=True):
        _FakeTreeConnect.connected.append((self.share_name, require_secure_negotiate))
        self.session.tree_connect_table[len(self.session.tree_connect_table)] = self


@pytest.fixture(autouse=True)
def fresh_refusals(monkeypatch):
    """Refusals are remembered process-wide; each test starts with none."""
    monkeypatch.setattr(smb_mod, "_REFUSALS", smb_mod._RefusedLogins())
    monkeypatch.setattr(smb_mod, "TreeConnect", _FakeTreeConnect)
    _FakeTreeConnect.connected = []


@pytest.fixture
def fake_client(monkeypatch):
    def install(**kwargs):
        client = _FakeSmbClient(**kwargs)
        monkeypatch.setattr(smb_mod, "smbclient", client)
        return client

    return install


def _storage(**credentials):
    return SmbStorage(SmbCredentials(**credentials))


class TestHowTheShareIsAddressed:
    def test_a_share_root_becomes_a_unc_path(self, fake_client):
        client = fake_client(listings={r"\\nas\music": []})
        _storage().listdir("smb://nas/music")
        assert client.calls[0][0] == r"\\nas\music"

    def test_a_folder_inside_the_share_keeps_its_spaces(self, fake_client):
        client = fake_client(listings={"\\\\nas\\music\\The Beatles": []})
        _storage().listdir("smb://nas/music/The Beatles")
        assert client.calls[0][0] == "\\\\nas\\music\\The Beatles"

    def test_an_ipv6_address_loses_its_brackets_on_the_wire(self, fake_client):
        client = fake_client(listings={r"\\fe80::1\music": []})
        _storage().listdir("smb://[fe80::1]/music")
        assert client.calls[0][0] == r"\\fe80::1\music"

    def test_a_misspelt_location_is_an_os_error(self):
        with pytest.raises(LocatorError):
            _storage().listdir("smb://nas")


class TestCredentials:
    def test_the_configured_account_is_used(self, fake_client):
        client = fake_client(listings={r"\\nas\music": []})
        _storage(username="media", password="hunter2").listdir("smb://nas/music")
        _, kwargs = client.calls[0]
        assert kwargs["username"] == "media"
        assert kwargs["password"] == "hunter2"

    def test_no_account_logs_in_as_guest_by_name(self, fake_client):
        """An omitted username does not mean anonymous. ``smbclient`` picks
        the first session already open to that server when asked for no
        particular user, so a guest share would be read as whoever
        authenticated first — and ``spnego`` cannot build a context with no
        username at all."""
        client = fake_client(listings={r"\\nas\music": []})
        _storage().listdir("smb://nas/music")
        _, kwargs = client.calls[0]
        assert kwargs["username"] == smb_mod.GUEST_USERNAME
        assert kwargs["password"] == ""

    def test_two_shares_with_two_accounts_do_not_share_a_session(
        self, fake_client
    ):
        client = fake_client(
            listings={r"\\nas\music": [], r"\\nas\private": []}
        )
        _storage(username="alice", password="hunter2").listdir("smb://nas/private")
        _storage().listdir("smb://nas/music")

        assert [c[1]["username"] for c in client.calls] == ["alice", "guest"]

    def test_a_user_in_the_url_signs_in_as_nobody(self, fake_client):
        """The login is the source's; a path cannot name another."""
        client = fake_client(listings={r"\\nas\music": []})
        with pytest.raises(OSError):
            _storage(username="media", password="hunter2").listdir(
                "smb://guest@nas/music"
            )
        assert client.calls == []

    def test_a_non_default_port_travels_with_the_request(self, fake_client):
        client = fake_client(listings={r"\\nas\music": []})
        _storage().listdir("smb://nas:4450/music")
        assert client.calls[0][1]["port"] == 4450

    def test_the_default_port_is_left_to_the_client(self, fake_client):
        client = fake_client(listings={r"\\nas\music": []})
        _storage().listdir("smb://nas/music")
        assert "port" not in client.calls[0][1]

    def test_encryption_is_requested_when_configured(self, fake_client):
        client = fake_client(listings={r"\\nas\music": []})
        _storage(encrypt=True).listdir("smb://nas/music")
        assert client.calls[0][1]["encrypt"] is True

    def test_encryption_is_left_unsaid_when_not_configured(self, fake_client):
        """``encrypt`` is tri-state: an explicit False means *force it off*,
        which ``smbclient`` refuses on a session the server has already
        encrypted — so a NAS requiring SMB3 encryption would fail on the
        second call to it."""
        client = fake_client(listings={r"\\nas\music": []})
        _storage().listdir("smb://nas/music")
        assert "encrypt" not in client.calls[0][1]

    def test_every_request_is_bounded(self, fake_client):
        """An address with nothing behind it has to fail a scan rather than
        stall it."""
        client = fake_client(listings={r"\\nas\music": []})
        _storage().listdir("smb://nas/music")
        assert client.calls[0][1]["connection_timeout"] > 0


class TestTheLogon:
    """``smbclient`` pools connections and sessions in process-global
    dictionaries and fills them with an unsynchronised check-then-create, so
    the storage logs in explicitly and serialises the logons per server."""

    def test_the_session_is_registered_before_the_share_is_read(
        self, fake_client
    ):
        client = fake_client(listings={r"\\nas\music": []})
        _storage(username="media", password="hunter2").listdir("smb://nas/music")

        assert client.logons[0][0] == "nas"
        assert client.logons[0][1]["username"] == "media"
        assert client.logons[0][1]["password"] == "hunter2"

    def test_the_port_travels_with_the_logon(self, fake_client):
        client = fake_client(listings={r"\\nas\music": []})
        _storage().listdir("smb://nas:4450/music")

        assert client.logons[0][1]["port"] == 4450

    def test_two_shares_on_one_server_do_not_log_in_at_once(self, fake_client):
        """The loser of that race leaves a connected socket and a reader
        thread behind for the life of the process, because only the winner
        is in the cache to be closed."""
        client = fake_client(
            listings={r"\\nas\music": [], r"\\nas\classical": []}
        )
        overlapped = []
        registering = threading.Lock()

        def slow_register(server, **kwargs):
            if not registering.acquire(blocking=False):
                overlapped.append(server)
            else:
                time.sleep(0.2)
                registering.release()
            client.logons.append((server, kwargs))
            return client.session

        client.register_session = slow_register
        storage = _storage()
        threads = [
            threading.Thread(target=storage.listdir, args=(url,))
            for url in ("smb://nas/music", "smb://nas/classical")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert overlapped == []
        assert len(client.logons) == 2

    def test_a_second_server_is_not_held_up_by_the_first(self, fake_client):
        """One lock for every server would make an unreachable NAS delay the
        shares that answer."""
        client = fake_client(
            listings={r"\\nas\music": [], r"\\vault\music": []}
        )
        entered = threading.Event()
        release = threading.Event()

        def blocking_register(server, **kwargs):
            if server == "nas":
                entered.set()
                assert release.wait(timeout=5)
            client.logons.append((server, kwargs))
            return client.session

        client.register_session = blocking_register
        storage = _storage()
        held = threading.Thread(target=storage.listdir, args=("smb://nas/music",))
        held.start()
        assert entered.wait(timeout=5)

        storage.listdir("smb://vault/music")

        release.set()
        held.join(timeout=5)
        assert [server for server, _ in client.logons] == ["vault", "nas"]


class TestListing:
    def test_children_come_back_as_share_urls(self, fake_client):
        fake_client(
            listings={
                r"\\nas\music": [
                    _Entry("The Beatles", is_dir=True),
                    _Entry("loose.flac", size=4096),
                ]
            }
        )
        entries = _storage().listdir("smb://nas/music")

        assert [e.path for e in entries] == [
            "smb://nas/music/The Beatles",
            "smb://nas/music/loose.flac",
        ]
        assert [e.is_dir for e in entries] == [True, False]

    def test_sizes_arrive_with_the_listing(self, fake_client):
        """A share reports them in the directory query, so filtering a folder
        of artwork costs no extra round trip."""
        fake_client(listings={r"\\nas\music": [_Entry("cover.jpg", size=1234)]})
        [entry] = _storage().listdir("smb://nas/music")
        assert entry.size == 1234
        assert _storage().size_of(entry) == 1234

    def test_a_link_to_a_folder_is_not_reported_as_one(self, fake_client):
        """A scan descends into directories. Following a reparse point would
        index a share twice under two paths, and a link pointing at one of
        its own ancestors would walk until the paths grew without bound."""
        fake_client(
            listings={
                r"\\nas\music": [
                    _Entry("Albums", is_dir=True),
                    _Entry("Best", link_to_dir=True),
                ]
            }
        )
        entries = _storage().listdir("smb://nas/music")
        assert [(e.name, e.is_dir) for e in entries] == [
            ("Albums", True),
            ("Best", False),
        ]

    def test_a_directory_that_is_not_there_is_an_os_error(self, fake_client):
        fake_client(listings={})
        with pytest.raises(OSError):
            _storage().listdir("smb://nas/music/gone")


class TestMeasuring:
    def test_a_file_reports_size_and_identity(self, fake_client):
        fake_client(files={r"\\nas\music\a.flac": b"audio"})
        measured = _storage().stat("smb://nas/music/a.flac")

        assert measured.size == 5
        assert not measured.is_dir
        assert measured.identity.device == "smb:305419896"
        assert measured.identity.inode == "42"

    def test_a_server_without_a_file_index_reports_no_identity(
        self, fake_client
    ):
        """Some servers answer zero for every file. Passed on as an identity
        it would make each file look like a rename of the last one indexed,
        handing an unrelated library row's history to a new file."""
        fake_client(
            stats={r"\\nas\music\a.flac": _stat_result(size=5, ino=0)}
        )
        assert _storage().stat("smb://nas/music/a.flac").identity is None

    def test_a_volume_serial_cannot_collide_with_a_local_device(
        self, fake_client
    ):
        """The library looks identities up in one table for every storage, so
        the protocol has to be part of the key."""
        fake_client(files={r"\\nas\music\a.flac": b"audio"})
        identity = _storage().stat("smb://nas/music/a.flac").identity
        assert identity.device.startswith("smb:")

    def test_a_folder_says_so(self, fake_client):
        fake_client(listings={r"\\nas\music": []})
        assert _storage().stat("smb://nas/music").is_dir

    def test_presence_is_answered_without_raising(self, fake_client):
        fake_client(files={r"\\nas\music\a.flac": b"audio"})
        storage = _storage()
        assert storage.exists("smb://nas/music/a.flac")
        assert storage.is_file("smb://nas/music/a.flac")
        assert not storage.exists("smb://nas/music/b.flac")


class TestReading:
    def test_a_file_opens_for_reading(self, fake_client):
        client = fake_client(files={r"\\nas\music\a.flac": b"fLaC-ish"})
        with _storage().open("smb://nas/music/a.flac") as handle:
            assert handle.read() == b"fLaC-ish"
        assert client.calls[0][1]["mode"] == "rb"

    def test_readers_do_not_lock_each_other_out(self, fake_client):
        """``smbclient`` opens deny-all by default. The server holds a stream
        open for a whole response while the indexer may be reading the same
        track, and the second open would be refused as a sharing
        violation — which a renderer sees as a dead track."""
        client = fake_client(files={r"\\nas\music\a.flac": b"x"})
        _storage().open("smb://nas/music/a.flac")
        assert client.calls[0][1]["share_access"] == "rwd"

    def test_reads_are_buffered(self, fake_client):
        """Tag reads walk a header and the embedder seeks to fragments, so a
        chunk worth having beats a block at a time over the network."""
        client = fake_client(files={r"\\nas\music\a.flac": b"x"})
        _storage().open("smb://nas/music/a.flac")
        assert client.calls[0][1]["buffering"] >= 64 * 1024

    def test_nothing_local_to_hand_to_another_process(self, fake_client):
        fake_client(files={r"\\nas\music\a.flac": b"x"})
        assert _storage().local_path("smb://nas/music/a.flac") is None

    def test_a_copy_is_made_for_tools_that_want_a_filename(
        self, fake_client, tmp_path
    ):
        """fpcalc opens a file by name, so the bytes are spilled for the
        length of the call and removed afterwards."""
        fake_client(files={r"\\nas\music\a.flac": b"audio bytes"})
        storage = SmbStorage(spill_dir=str(tmp_path / "spill"))

        with storage.materialize("smb://nas/music/a.flac") as local:
            assert open(local, "rb").read() == b"audio bytes"
            assert local.endswith(".flac")
            spilled = local

        assert not os.path.exists(spilled)

    def test_a_copy_left_by_a_killed_run_is_swept(self, fake_client, tmp_path):
        fake_client(files={r"\\nas\music\a.flac": b"audio bytes"})
        spill = tmp_path / "spill"
        spill.mkdir()
        orphan = spill / "kalinka-spill-old.flac"
        orphan.write_bytes(b"left behind")
        os.utime(orphan, (0, 0))

        with SmbStorage(spill_dir=str(spill)).materialize("smb://nas/music/a.flac"):
            pass

        assert not orphan.exists()

    def test_the_sweep_leaves_alone_what_it_did_not_spill(
        self, fake_client, tmp_path
    ):
        """The default spill directory is the shared system one, where a
        stranger's file — another program's, another user's — is not this
        class's to delete however old it is."""
        fake_client(files={r"\\nas\music\a.flac": b"audio bytes"})
        spill = tmp_path / "spill"
        spill.mkdir()
        strangers = [spill / "kalinka-server.sock", spill / "kalinka-9.log"]
        for stranger in strangers:
            stranger.write_bytes(b"not ours")
            os.utime(stranger, (0, 0))

        with SmbStorage(spill_dir=str(spill)).materialize("smb://nas/music/a.flac"):
            pass

        assert all(stranger.exists() for stranger in strangers)


class TestFailuresArriveAsOsErrors:
    """Authentication, negotiation and transport failures are the client's own
    exception types. One escaping would abort a scan."""

    def test_an_authentication_failure(self, fake_client):
        from smbprotocol.exceptions import SMBAuthenticationError

        fake_client(failure=SMBAuthenticationError("bad password"))
        with pytest.raises(OSError, match="bad password"):
            _storage().listdir("smb://nas/music")

    def test_a_transport_failure_while_opening(self, fake_client):
        from smbprotocol.exceptions import SMBConnectionClosed

        fake_client(failure=SMBConnectionClosed("connection reset"))
        with pytest.raises(OSError):
            _storage().open("smb://nas/music/a.flac")

    def test_a_failure_while_measuring(self, fake_client):
        from smbprotocol.exceptions import SMBAuthenticationError

        fake_client(failure=SMBAuthenticationError("nope"))
        with pytest.raises(OSError):
            _storage().stat("smb://nas/music/a.flac")


class TestAvailability:
    @pytest.mark.asyncio
    async def test_a_reachable_share_names_what_it_is(self, fake_client):
        fake_client(listings={r"\\nas\music": [_Entry("a.flac", size=1)]})
        status = await _storage().probe_root("smb://nas/music")

        assert status.available
        assert status.is_network
        assert not status.is_autofs
        assert status.fs_type == "smb"
        assert status.identity == "smb //nas/music 305419896"

    @pytest.mark.asyncio
    async def test_a_reachable_share_is_not_listed_to_say_so(self, fake_client):
        """Every playback asks whether the folder is available, and a
        listing to find out whether it also happens to be empty would be a
        round trip per track served."""
        client = fake_client(listings={r"\\nas\music": []})
        status = await _storage().probe_root("smb://nas/music")

        assert status.available
        assert [unc for unc, _ in client.calls] == [r"\\nas\music"]

    @pytest.mark.asyncio
    async def test_an_empty_share_answers_the_purge_guard(self, fake_client):
        fake_client(listings={r"\\nas\music": []})
        assert _storage().is_empty("smb://nas/music")

    @pytest.mark.asyncio
    async def test_a_share_that_will_not_list_does_not_read_as_empty(
        self, fake_client
    ):
        fake_client(listings={})
        with pytest.raises(OSError):
            _storage().is_empty("smb://nas/music")

    @pytest.mark.asyncio
    async def test_a_wrong_password_says_so(self, fake_client):
        from smbprotocol.exceptions import SMBAuthenticationError

        fake_client(failure=SMBAuthenticationError("logon failure"))
        status = await _storage().probe_root("smb://nas/music")

        assert not status.available
        assert "nas" in status.reason
        assert "music" in status.reason

    @pytest.mark.asyncio
    async def test_a_misspelt_url_says_what_to_write(self, fake_client):
        fake_client()
        status = await _storage().probe_root("smb://nas")
        assert not status.available
        assert "smb://nas/music" in status.reason

    @pytest.mark.asyncio
    async def test_a_file_named_as_a_folder_is_refused(self, fake_client):
        fake_client(files={r"\\nas\music": b"not a folder"})
        status = await _storage().probe_root("smb://nas/music")
        assert not status.available
        assert "folder" in status.reason

    @pytest.mark.asyncio
    async def test_nothing_defers_a_probe(self, fake_client):
        """Only a local automount has a reason to be left alone; asking a
        server is just a request."""
        fake_client(listings={r"\\nas\music": []})
        assert not _storage().should_defer_probe("smb://nas/music")


class TestChangeNotification:
    def test_a_share_cannot_report_changes(self):
        """SMB2 change notification is not something a NAS can be relied on
        for, so these roots are left to the periodic scan — and saying so is
        what keeps the watcher from pretending otherwise."""
        assert _storage().watcher() is None


class TestTellingARefusalFromSilence:
    """The one failure the person reading it can act on. A server that is
    switched off and a password that is wrong are the same OSError to every
    caller, and nothing like each other to the user."""

    @pytest.mark.parametrize(
        "raised",
        [
            SMBAuthenticationError("bad credentials"),
            LogonFailure(),
            PasswordExpired(),
            AccessDenied(),
        ],
    )
    def test_a_refused_login_is_a_permission_error(self, raised):
        storage = SmbStorage()
        with pytest.raises(PermissionError):
            with storage._as_os_error(parse("smb://nas/music")):
                raise raised

    def test_a_refused_login_is_still_an_os_error(self):
        """Every caller guards with ``except OSError`` and none of them
        should have to learn a second type."""
        storage = SmbStorage()
        with pytest.raises(OSError):
            with storage._as_os_error(parse("smb://nas/music")):
                raise LogonFailure()

    def test_a_server_that_does_not_answer_stays_a_plain_os_error(self):
        storage = SmbStorage()
        with pytest.raises(OSError) as caught:
            with storage._as_os_error(parse("smb://nas/music")):
                raise ValueError("the socket went away")
        assert not isinstance(caught.value, PermissionError)

    def test_the_two_read_differently(self, fake_client):
        fake_client(failure=LogonFailure())
        refused = _storage(username="media", password="x").probe_root_blocking(
            "smb://nas/music"
        )
        fake_client(failure=ValueError("Failed to connect to 'nas:445'"))
        silent = _storage().probe_root_blocking("smb://nas/music")
        assert "refused the login" in refused.reason
        assert "did not answer" in silent.reason


def _response(status):
    """A server's answer carrying ``status``, for statuses ``smbprotocol``
    has no exception class of its own for."""
    from smbprotocol.exceptions import SMB2ErrorResponse, SMBResponseException
    from smbprotocol.header import SMB2HeaderResponse

    header = SMB2HeaderResponse()
    header["status"] = status
    header["data"] = SMB2ErrorResponse().pack()
    return SMBResponseException(header)


class TestGuestSessions:
    """A guest has no key to sign with, and ``smbprotocol`` insists on signing
    unless told otherwise — so every guest share failed to open."""

    def test_a_guest_session_is_not_signed(self, fake_client):
        client = fake_client(listings={r"\\nas\music": []})
        _storage().listdir("smb://nas/music")
        assert client.logons[0][1]["require_signing"] is False

    def test_a_signed_in_session_keeps_signing(self, fake_client):
        client = fake_client(listings={r"\\nas\music": []})
        _storage(username="media", password="hunter2").listdir("smb://nas/music")
        assert "require_signing" not in client.logons[0][1]

    def test_a_guest_connects_its_share_without_the_signed_check(
        self, fake_client
    ):
        """The SMB 3.0 negotiation check needs a signature a guest cannot
        make, and ``smbclient`` only offers turning it off for the whole
        process — so the share is connected here instead."""
        fake_client(listings={r"\\nas\music": []})
        _storage().listdir("smb://nas/music")
        assert _FakeTreeConnect.connected == [(r"\\nas\music", False)]

    def test_the_share_is_connected_once_per_session(self, fake_client):
        fake_client(listings={r"\\nas\music": []})
        storage = _storage()
        storage.listdir("smb://nas/music")
        storage.listdir("smb://nas/music")
        assert len(_FakeTreeConnect.connected) == 1

    def test_a_signed_in_share_is_left_to_the_client(self, fake_client):
        fake_client(listings={r"\\nas\music": []})
        _storage(username="media", password="hunter2").listdir("smb://nas/music")
        assert _FakeTreeConnect.connected == []


class TestSayingWhatWentWrong:
    """Most of these reach whoever typed the share and the login, where "did
    not answer" for a NAS that answered sends them after the wrong thing."""

    @pytest.mark.parametrize(
        "raised, expected",
        [
            (LogonFailure(), "the user name or password is wrong"),
            (_response(0xC0000234), "locked after too many failed logins"),
            (_response(0xC0000072), "the account is disabled"),
            (_response(0xC0000071), "the password has expired"),
        ],
    )
    def test_a_refused_login_says_why(self, fake_client, raised, expected):
        fake_client(failure=raised)
        with pytest.raises(PermissionError) as caught:
            _storage(username="media", password="x").listdir("smb://nas/music")
        assert expected in str(caught.value)
        assert "'media'" in str(caught.value)
        assert "'music'" in str(caught.value)

    def test_a_share_that_is_not_there_is_named(self, fake_client):
        from smbprotocol.exceptions import BadNetworkName

        fake_client(failure=BadNetworkName())
        with pytest.raises(OSError, match="has no share named 'music'") as caught:
            _storage(username="media", password="x").listdir("smb://nas/music")
        assert not isinstance(caught.value, PermissionError)

    def test_a_path_that_is_not_there_is_a_missing_file(self, fake_client):
        """The indexer tells a deleted file from a share that went away by
        this type."""
        from smbprotocol.exceptions import SMBOSError

        client = fake_client()

        def missing(unc, **kwargs):
            raise SMBOSError(0xC0000034, unc)

        client.stat = missing
        with pytest.raises(FileNotFoundError, match="'Album/a.flac' in share 'music'"):
            _storage().stat("smb://nas/music/Album/a.flac")

    def test_an_unknown_user_mapped_to_guest_is_said_to_be_unknown(
        self, fake_client
    ):
        """A NAS set to map unknown users to guest accepts a mistyped name
        as a guest, which a signed session then refuses."""
        from smbprotocol.exceptions import SMBException

        fake_client(
            failure=SMBException(
                "SMB encryption or signing was required but session was "
                "authenticated as a guest which does not support encryption "
                "or signing"
            )
        )
        with pytest.raises(PermissionError, match="does not know that user name"):
            _storage(username="kalinka@WORKGROUP", password="x").listdir(
                "smb://nas/music"
            )

    def test_a_server_insisting_on_signing_turns_a_guest_away(self, fake_client):
        from smbprotocol.exceptions import SMBException

        fake_client(failure=SMBException("session was authenticated as a guest"))
        with pytest.raises(PermissionError, match="sign in with an account"):
            _storage().listdir("smb://nas/music")

    def test_a_guest_asked_to_encrypt_is_told_it_cannot(self, fake_client):
        from smbprotocol.exceptions import SMBException

        fake_client(failure=SMBException("session was authenticated as a guest"))
        with pytest.raises(PermissionError, match="a guest cannot encrypt"):
            _storage(encrypt=True).listdir("smb://nas/music")

    def test_a_server_that_hangs_up_on_the_negotiation_may_only_speak_smb1(
        self, fake_client
    ):
        from smbprotocol.exceptions import SMBConnectionClosed

        fake_client(failure=SMBConnectionClosed("SMB socket was closed"))
        with pytest.raises(OSError, match="SMB2 or later"):
            _storage().listdir("smb://nas/music")

    def test_a_server_that_is_not_there_did_not_answer(self, fake_client):
        fake_client(failure=ValueError("Failed to connect to 'nas:445'"))
        with pytest.raises(OSError, match="did not answer"):
            _storage().listdir("smb://nas/music")


class TestRefusedLoginsAreNotRepeated:
    """Scans, playback and the renderer all retry. Each retry of a wrong
    password counts against the account, which a NAS or Windows locks — or
    whose address it blocks — after about ten."""

    def test_a_refused_password_is_not_sent_again(self, fake_client):
        client = fake_client(failure=LogonFailure())
        storage = _storage(username="media", password="wrong")
        with pytest.raises(PermissionError):
            storage.listdir("smb://nas/music")
        with pytest.raises(PermissionError, match="not tried again before"):
            storage.listdir("smb://nas/music")
        assert len(client.logons) == 1

    def test_another_storage_with_the_same_login_remembers_it(self, fake_client):
        """The indexer, the server's stream and a settings check each hold a
        storage of their own."""
        client = fake_client(failure=LogonFailure())
        with pytest.raises(PermissionError):
            _storage(username="media", password="wrong").listdir("smb://nas/music")
        with pytest.raises(PermissionError):
            _storage(username="media", password="wrong").stat("smb://nas/music/a.flac")
        assert len(client.logons) == 1

    def test_a_password_typed_anew_is_tried(self, fake_client):
        client = fake_client(failure=LogonFailure())
        with pytest.raises(PermissionError):
            _storage(username="media", password="wrong").listdir("smb://nas/music")
        client.failure = None
        client.listings = {r"\\nas\music": []}
        _storage(username="media", password="right").listdir("smb://nas/music")
        assert len(client.logons) == 2

    def test_a_refusal_is_forgotten_once_it_expires(self, fake_client, monkeypatch):
        monkeypatch.setattr(smb_mod, "_REFUSALS", smb_mod._RefusedLogins(memory_s=0))
        client = fake_client(failure=LogonFailure())
        storage = _storage(username="media", password="wrong")
        for _ in range(2):
            with pytest.raises(PermissionError):
                storage.listdir("smb://nas/music")
        assert len(client.logons) == 2

    def test_a_server_that_did_not_answer_is_asked_again(self, fake_client):
        """Silence says nothing about the password."""
        client = fake_client(failure=ValueError("Failed to connect"))
        storage = _storage(username="media", password="hunter2")
        for _ in range(2):
            with pytest.raises(OSError):
                storage.listdir("smb://nas/music")
        assert len(client.logons) == 2

    def test_the_password_is_not_kept(self):
        refusals = smb_mod._RefusedLogins()
        assert b"hunter2" not in refusals.digest("hunter2")
        assert refusals.digest("hunter2") != smb_mod._RefusedLogins().digest("hunter2")


class _DroppingFile(io.BytesIO):
    """A handle whose connection goes away after the open."""

    def __init__(self, error):
        super().__init__(b"audio")
        self.error = error

    def read(self, size=-1):
        raise self.error

    def readinto(self, buffer):
        raise self.error

    def seek(self, offset, whence=0):
        raise self.error

    def close(self):
        raise self.error


class TestReadsFailAsOsErrors:
    """The open was translated, but a read or a seek long after it raised the
    protocol's own exception — which the tag reader and the embedder took for
    a broken file, parking it for good."""

    @pytest.fixture
    def dropping(self, fake_client):
        from smbprotocol.exceptions import SMBConnectionClosed

        client = fake_client()
        client.open_file = lambda unc, **kwargs: _DroppingFile(
            SMBConnectionClosed("SMB socket was closed")
        )
        return client

    def test_a_read(self, dropping):
        with _storage().open("smb://nas/music/a.flac") as handle:
            with pytest.raises(OSError, match="dropped the connection"):
                handle.read(4)

    def test_a_read_into_a_buffer(self, dropping):
        with _storage().open("smb://nas/music/a.flac") as handle:
            with pytest.raises(OSError):
                handle.readinto(bytearray(4))

    def test_a_seek(self, dropping):
        with _storage().open("smb://nas/music/a.flac") as handle:
            with pytest.raises(OSError):
                handle.seek(10)

    def test_closing_after_the_connection_went_is_quiet(self, dropping):
        """The server dropped the handle with the connection, and a close
        failing would turn a read that had finished into an error."""
        handle = _storage().open("smb://nas/music/a.flac")
        handle.close()
        assert handle.closed


class TestHangsAreBounded:
    def test_a_connection_gives_up_on_a_silent_server_within_seconds(self):
        """``smbprotocol`` waits ten minutes for a silent server before an
        echo and ten more for the echo, which stalled a scan or a stream for
        twenty on a NAS that had been switched off. It takes the wait from
        its environment as each connection is made — if that knob goes, this
        is what notices."""
        import uuid

        from smbprotocol.connection import Connection

        connection = Connection(uuid.uuid4(), "nas", 445)
        assert connection._receive_timeout <= 30


class TestOnlyTheLoginIsRemembered:
    def test_a_share_that_turns_a_guest_away_is_asked_again(
        self, fake_client, monkeypatch
    ):
        """No account to lock: the login worked and the share said no."""
        from smbprotocol.exceptions import AccessDenied

        class _RefusingTree(_FakeTreeConnect):
            def connect(self, require_secure_negotiate=True):
                raise AccessDenied()

        client = fake_client(listings={r"\\nas\music": []})
        monkeypatch.setattr(smb_mod, "TreeConnect", _RefusingTree)
        storage = _storage()
        for _ in range(2):
            with pytest.raises(PermissionError, match="may not read"):
                storage.listdir("smb://nas/music")
        assert len(client.logons) == 2
