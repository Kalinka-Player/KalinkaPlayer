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
from types import SimpleNamespace

import pytest

import kalinka_plugin_localfiles.storage.smb as smb_mod
from kalinka_plugin_localfiles.storage.locator import LocatorError
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
        storage = _storage(username="alice", password="hunter2")
        storage.listdir("smb://nas/private")
        storage.listdir("smb://guest@nas/music")

        assert [c[1]["username"] for c in client.calls] == ["alice", "guest"]

    def test_a_user_in_the_url_overrides_the_configured_one(self, fake_client):
        client = fake_client(listings={r"\\nas\music": []})
        _storage(username="media", password="hunter2").listdir(
            "smb://guest@nas/music"
        )
        _, kwargs = client.calls[0]
        assert kwargs["username"] == "guest"

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
        assert not status.empty
        assert status.identity == "smb //nas/music 305419896"

    @pytest.mark.asyncio
    async def test_an_empty_share_is_reported_separately(self, fake_client):
        fake_client(listings={r"\\nas\music": []})
        status = await _storage().probe_root("smb://nas/music")
        assert status.available
        assert status.empty

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
