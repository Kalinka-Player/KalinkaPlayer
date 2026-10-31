#!/usr/bin/env python3
"""Reporting music folder availability to the settings page.

The status is evaluated on request rather than once at setup, so an
unreachable share appears and clears without a restart. That makes the cost
of one evaluation the thing to watch: the settings page polls the folders'
status, and each poll probes every folder — including the ones that have
stopped answering. The module's health, which the server polls far more
often, reports the last such check instead of making one.

What keeps that affordable lives on the storage instances, so the plugin has
to keep them. A resolver built per call brings an empty probe registry with
it, and the deduplication that holds a hung folder to a single blocked worker
thread never sees a second caller.
"""

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from kalinka_plugin_sdk import ModuleHealthState

from kalinka_plugin_localfiles import module_setup
from kalinka_plugin_localfiles.config_model import (
    AccountSignIn,
    LocalFilesConfig,
    LocalLocation,
    LocalSource,
    SmbLocation,
    SmbSource,
)
from kalinka_plugin_localfiles.input_module_db import LocalFilesInputModuleDb
from kalinka_plugin_localfiles.localfiles import LocalFilesInputModule
from kalinka_plugin_localfiles.module_setup import (
    KalinkaPluginLocalFiles,
    _quiet_share_protocol_logs,
)
from kalinka_plugin_localfiles.storage.local import LocalStorage


class _Context:
    """Just enough of the plugin context to read config off."""

    def __init__(self, config):
        self.config = config


@pytest.fixture
def plugin(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    made = KalinkaPluginLocalFiles()
    made._context = _Context(
        LocalFilesConfig(
            music_folders=[str(music)],
            db_path=str(tmp_path / "localfiles.db"),
            artwork_path=str(tmp_path / "artwork"),
        )
    )
    return made, music


@pytest.fixture
def clock(monkeypatch):
    """The clock the plugin ages its folder checks by, moved by hand."""
    clock = SimpleNamespace(now=0.0)
    clock.monotonic = lambda: clock.now
    monkeypatch.setattr(module_setup, "time", clock)
    return clock


def _write(config, **fields):
    """As the server writes a change: set, then reconcile."""
    for name, value in fields.items():
        setattr(config, name, value)
    config.reconcile(frozenset(fields))


def _share(**location):
    return SmbSource(
        id="nas",
        location=SmbLocation(**{"host": "nas", "path": "music", **location}),
        authentication=AccountSignIn(username="media", password="p"),
    )


class TestTheResolverIsKept:
    def test_repeated_polls_share_one_resolver(self, plugin):
        made, _ = plugin
        config = LocalFilesConfig(**made._context.config.model_dump())
        assert made._resolver_for(config) is made._resolver_for(config)

    def test_new_credentials_get_a_new_resolver(self, plugin):
        made, _ = plugin
        config = LocalFilesConfig(**made._context.config.model_dump())
        first = made._resolver_for(config)

        changed = config.model_copy(deep=True)
        changed.music_sources = [_share()]
        assert made._resolver_for(changed) is not first

    @pytest.mark.asyncio
    async def test_a_hung_folder_costs_one_thread_however_often_it_is_polled(
        self, plugin, monkeypatch
    ):
        """Measured rather than asserted about: the executor is shared
        process-wide and small on the appliance, so a folder that never
        answers must not take a worker per poll.
        """
        made, _ = plugin
        entered = threading.Semaphore(0)
        release = threading.Event()
        blocked = []

        def hang(self, root):
            blocked.append(threading.current_thread().name)
            entered.release()
            release.wait(timeout=10)
            return self.unavailable(root, "released")

        monkeypatch.setattr(LocalStorage, "probe_root_blocking", hang)

        try:
            # Three is enough: each poll gives up after the status timeout,
            # so a per-call resolver would show three distinct threads.
            for _ in range(3):
                await made._music_folder_statuses()
            # The first probe is still in the thread; later polls must have
            # waited on it rather than starting their own.
            assert entered.acquire(timeout=5)
            assert len(set(blocked)) == 1
        finally:
            release.set()
            await asyncio.sleep(0.1)


class TestPlaybackAndTheStatusPageAgree:
    """The settings page probes with whatever credentials the config now
    carries. Playback reading a resolver captured at setup is how a folder
    comes to read "Available" while every track behind it fails to open."""

    def test_the_module_reads_the_plugin_s_resolver(self, plugin):
        made, _ = plugin
        config = LocalFilesConfig(**made._context.config.model_dump())
        module = LocalFilesInputModule(
            config,
            LocalFilesInputModuleDb(config),
            storage_source=made._current_resolver,
        )

        assert module._storage is made._current_resolver()

    def test_new_credentials_reach_playback_too(self, plugin):
        made, _ = plugin
        config = LocalFilesConfig(**made._context.config.model_dump())
        module = LocalFilesInputModule(
            config,
            LocalFilesInputModuleDb(config),
            storage_source=made._current_resolver,
        )
        before = module._storage

        _write(made._context.config, music_sources=[_share()])

        assert module._storage is not before
        assert module._storage is made._current_resolver()

    def test_a_module_of_its_own_still_builds_one(self, tmp_path):
        """Nothing outside the plugin has a resolver to lend, and the
        subprocess entry points build the module for themselves."""
        config = LocalFilesConfig(
            music_folders=[str(tmp_path)],
            db_path=str(tmp_path / "localfiles.db"),
            artwork_path=str(tmp_path / "artwork"),
        )
        module = LocalFilesInputModule(config, LocalFilesInputModuleDb(config))

        assert module._storage is not None


@pytest.fixture
def share_protocol_log():
    """The two levels the quieting reads and writes, put back afterwards."""
    root, protocol = logging.getLogger(), logging.getLogger("smbprotocol")
    before = (root.level, protocol.level)
    protocol.setLevel(logging.NOTSET)
    yield root, protocol
    root.setLevel(before[0])
    protocol.setLevel(before[1])


class TestTheShareProtocolIsNotAllowedToNarrate:
    def test_an_ordinary_run_is_spared_the_narration(self, share_protocol_log):
        root, protocol = share_protocol_log
        root.setLevel(logging.INFO)

        _quiet_share_protocol_logs()

        assert not protocol.isEnabledFor(logging.INFO)

    def test_a_debug_run_keeps_the_detail(self, share_protocol_log):
        """Whoever asked for debug logging is the one person who wants to see
        a tree connect."""
        root, protocol = share_protocol_log
        root.setLevel(logging.DEBUG)

        _quiet_share_protocol_logs()

        assert protocol.isEnabledFor(logging.DEBUG)


class TestWhatItReports:
    @pytest.mark.asyncio
    async def test_a_folder_that_is_there(self, plugin):
        made, music = plugin
        (music / "a.flac").write_bytes(b"x")

        [(status, stored)] = await made._music_folder_statuses()
        assert status.available
        assert stored is None

    @pytest.mark.asyncio
    async def test_a_folder_that_is_not(self, plugin, tmp_path):
        made, _ = plugin
        _write(made._context.config, music_folders=[str(tmp_path / "absent")])

        [(status, _)] = await made._music_folder_statuses()
        assert not status.available
        assert status.reason

    @pytest.mark.asyncio
    async def test_a_folder_naming_a_protocol_we_cannot_read(self, plugin):
        """It has to come back as an unavailable folder with a reason,
        because this is what the settings page renders — an exception here
        would blank the whole module's status."""
        made, _ = plugin
        _write(made._context.config, music_folders=["webdav://nas/music"])

        [(status, _)] = await made._music_folder_statuses()
        assert not status.available
        assert "webdav" in status.reason

    @pytest.mark.asyncio
    async def test_a_share_among_the_folders_is_not_read(self, plugin):
        """Only a music source says how to sign in to a share."""
        made, _ = plugin
        _write(made._context.config, music_folders=["smb://nas/music"])

        [(status, _)] = await made._music_folder_statuses()
        assert not status.available
        assert "music source" in status.reason

    @pytest.mark.asyncio
    async def test_a_misspelt_share_source(self, plugin):
        made, _ = plugin
        _write(made._context.config, music_sources=[_share(path="")])

        [(status, _)] = await made._music_folder_statuses()
        assert not status.available
        assert "share" in status.reason

    @pytest.mark.asyncio
    async def test_each_place_is_reported_once_in_the_order_of_the_sources(
        self, plugin, tmp_path
    ):
        made, music = plugin
        disk = tmp_path / "disk"
        disk.mkdir()
        _write(
            made._context.config,
            music_sources=[
                LocalSource(id="disk", location=LocalLocation(path=str(disk))),
                LocalSource(id="music", location=LocalLocation(path=str(music))),
                LocalSource(id="again", location=LocalLocation(path=str(music))),
            ],
        )

        statuses = await made._music_folder_statuses()
        assert [status.root for status, _ in statuses] == [str(disk), str(music)]


class TestHealthReadsTheLastCheck:
    """The server asks for the module's health every few seconds, which must
    not contact every share each time."""

    @pytest.mark.asyncio
    async def test_the_first_poll_starts_a_check_the_next_reports(
        self, plugin, tmp_path
    ):
        made, _ = plugin
        _write(made._context.config, music_folders=[str(tmp_path / "absent")])

        first = await made.get_state()
        await made._folder_probe
        second = await made.get_state()

        assert first.state == ModuleHealthState.READY
        assert second.state == ModuleHealthState.WARNING
        assert "Music folder access" in second.message

    @pytest.mark.asyncio
    async def test_polls_within_the_rescan_interval_check_once(
        self, plugin, monkeypatch
    ):
        made, _ = plugin
        checks = 0
        check = made._music_folder_statuses

        async def counted():
            nonlocal checks
            checks += 1
            return await check()

        monkeypatch.setattr(made, "_music_folder_statuses", counted)

        await asyncio.gather(*(made.get_state() for _ in range(5)))
        await made._folder_probe
        await asyncio.gather(*(made.get_state() for _ in range(5)))

        assert checks == 1

    @pytest.mark.asyncio
    async def test_a_check_older_than_the_rescan_interval_is_renewed(
        self, plugin, clock
    ):
        made, _ = plugin
        await made.get_state()
        first = made._folder_probe
        await first

        clock.now += made._context.config.scan_interval_minutes * 60
        await made.get_state()

        assert made._folder_probe is not first

    @pytest.mark.asyncio
    async def test_with_rescans_manual_only_polls_still_check_once(
        self, plugin, clock
    ):
        """An interval of 0 once made every check stale on arrival, so each
        health poll contacted every share."""
        made, _ = plugin
        _write(made._context.config, scan_interval_minutes=0)
        await made.get_state()
        first = made._folder_probe
        await first

        clock.now += 60
        await made.get_state()
        assert made._folder_probe is first

        clock.now += 15 * 60
        await made.get_state()
        assert made._folder_probe is not first

    @pytest.mark.asyncio
    async def test_changed_folders_are_not_judged_by_the_old_check(
        self, plugin, tmp_path
    ):
        made, music = plugin
        _write(made._context.config, music_folders=[str(tmp_path / "absent")])
        await made.get_state()
        await made._folder_probe
        assert (await made.get_state()).state == ModuleHealthState.WARNING

        _write(made._context.config, music_folders=[str(music)])

        assert (await made.get_state()).state == ModuleHealthState.READY
        await made._folder_probe
        assert (await made.get_state()).state == ModuleHealthState.READY

    @pytest.mark.asyncio
    async def test_the_settings_page_check_is_what_health_reports(
        self, plugin, tmp_path
    ):
        made, _ = plugin
        _write(made._context.config, music_folders=[str(tmp_path / "absent")])

        await made.resolve_dynamic_field("storage.status_view")
        state = await made.get_state()

        assert state.state == ModuleHealthState.WARNING
        assert made._folder_probe is None

    @pytest.mark.asyncio
    async def test_a_failed_check_logs_only_its_type(
        self, plugin, monkeypatch, caplog
    ):
        made, _ = plugin

        async def failing():
            raise RuntimeError("smb://media:s3cret@nas/music refused")

        monkeypatch.setattr(made, "_music_folder_statuses", failing)

        state = await made.get_state()
        await asyncio.gather(made._folder_probe, return_exceptions=True)
        await asyncio.sleep(0)

        assert state.state == ModuleHealthState.READY
        assert "RuntimeError" in caplog.text
        assert "s3cret" not in caplog.text
