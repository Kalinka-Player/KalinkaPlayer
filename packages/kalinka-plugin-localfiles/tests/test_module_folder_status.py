#!/usr/bin/env python3
"""Reporting music folder availability to the settings page.

The status is evaluated on every request rather than once at setup, so an
unreachable share appears and clears without a restart. That makes the cost
of one evaluation the thing to watch: the app polls module status, and each
poll probes every folder — including the ones that have stopped answering.

What keeps that affordable lives on the storage instances, so the plugin has
to keep them. A resolver built per call brings an empty probe registry with
it, and the deduplication that holds a hung folder to a single blocked worker
thread never sees a second caller.
"""

import asyncio
import threading

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.module_setup import KalinkaPluginLocalFiles
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
        changed.smb.username = "media"
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


class TestWhatItReports:
    @pytest.mark.asyncio
    async def test_a_folder_that_is_there(self, plugin):
        made, music = plugin
        (music / "a.flac").write_bytes(b"x")

        [(status, stored)] = await made._music_folder_statuses()
        assert status.available
        assert not status.empty
        assert stored is None

    @pytest.mark.asyncio
    async def test_a_folder_that_is_not(self, plugin, tmp_path):
        made, _ = plugin
        made._context.config.music_folders = [str(tmp_path / "absent")]

        [(status, _)] = await made._music_folder_statuses()
        assert not status.available
        assert status.reason

    @pytest.mark.asyncio
    async def test_a_folder_naming_a_protocol_we_cannot_read(self, plugin):
        """It has to come back as an unavailable folder with a reason,
        because this is what the settings page renders — an exception here
        would blank the whole module's status."""
        made, _ = plugin
        made._context.config.music_folders = ["webdav://nas/music"]

        [(status, _)] = await made._music_folder_statuses()
        assert not status.available
        assert "webdav" in status.reason

    @pytest.mark.asyncio
    async def test_a_misspelt_share_url(self, plugin):
        made, _ = plugin
        made._context.config.music_folders = ["smb://nas"]

        [(status, _)] = await made._music_folder_statuses()
        assert not status.available
        assert "share" in status.reason
