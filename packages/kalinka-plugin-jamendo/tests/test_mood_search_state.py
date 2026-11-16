"""Mood search is offered while its index is being set up and once it is
ready, and taken back, with the reason on the modules page, once setting the
index up fails: a client would otherwise send queries that only ever come
back empty."""

import asyncio
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from kalinka_plugin_jamendo import mood_search
from kalinka_plugin_jamendo.config_model import JamendoConfig
from kalinka_plugin_jamendo.module_setup import KalinkaPluginJamendo
from kalinka_plugin_sdk import ModuleHealthState, ModuleState
from kalinka_plugin_sdk.inputmodule import ModuleCapability

# A download address may carry a key, which no message may repeat.
INDEX_URL = "https://index.test/jamendo_index_v2.sqlite?key=s3cret"
INDEX_NAME = "jamendo_index_v2.sqlite"


class FakeEmbedder:
    """The server's shared embedder, its runtime installed."""

    model_id = "all-MiniLM-L6-v2"
    model_version = 1
    dim = 384

    def __init__(self, available: bool = True):
        self._available = available

    async def available(self) -> bool:
        return self._available

    def missing_runtime(self) -> list[str]:
        return []


class HeldEmbedder(FakeEmbedder):
    """Still fetching its model until the test is done."""

    def __init__(self):
        super().__init__()
        self.asked = asyncio.Event()

    async def available(self) -> bool:
        self.asked.set()
        await asyncio.Event().wait()
        return True


def _write_index(path) -> None:
    """An index as far as provisioning reads one: its meta table."""
    meta = [("dtype", "int8"), ("int8_scale", "508")]
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        con.executemany("INSERT INTO meta VALUES (?, ?)", meta)
        con.commit()
    finally:
        con.close()


@pytest.fixture(autouse=True)
def downloads_fail(monkeypatch):
    monkeypatch.setattr(mood_search, "download_file", lambda url, dest: False)


@pytest.fixture
async def set_up(tmp_path):
    """Sets the plugin up as the server does, and records each time it
    reports that its state moved; ``hooked=False`` is a server before SDK
    3.23, which hands None."""
    plugins: list[KalinkaPluginJamendo] = []

    async def set_up(embedder, index_url=INDEX_URL, hooked=True):
        reported: list[None] = []
        plugin = KalinkaPluginJamendo()
        await plugin.setup(
            SimpleNamespace(
                config=JamendoConfig(
                    client_id="x",
                    ai_search_enabled=True,
                    ai_index_path=str(tmp_path / INDEX_NAME),
                    ai_index_url=index_url,
                ),
                embedder=embedder,
                report_state_changed=(
                    (lambda: reported.append(None)) if hooked else None
                ),
            )
        )
        plugins.append(plugin)
        return plugin, reported

    yield set_up
    for plugin in plugins:
        await plugin.shutdown()


def _offers_mood_search(plugin: KalinkaPluginJamendo) -> bool:
    return ModuleCapability.AI_SEARCH in plugin.get_interface().capabilities()


def _another_model(tmp_path, monkeypatch):
    embedder = FakeEmbedder()
    embedder.model_version = 2
    return embedder, INDEX_URL


def _no_sqlite_vec(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    return FakeEmbedder(), INDEX_URL


def _no_index_and_no_url(tmp_path, monkeypatch):
    return FakeEmbedder(), ""


def _download_fails(tmp_path, monkeypatch):
    return FakeEmbedder(), INDEX_URL


def _unreadable_index(tmp_path, monkeypatch):
    (tmp_path / INDEX_NAME).write_text("not a database")
    return FakeEmbedder(), INDEX_URL


def _no_text_model(tmp_path, monkeypatch):
    return FakeEmbedder(available=False), INDEX_URL


@pytest.mark.parametrize(
    "failing, reason",
    [
        (_another_model, "text model is all-MiniLM-L6-v2 v2"),
        (_no_sqlite_vec, "`sqlite-vec` does not load"),
        (_no_index_and_no_url, "**Mood index download URL** is empty"),
        (_download_fails, "could not be downloaded"),
        (_unreadable_index, "cannot be read"),
        (_no_text_model, "text model could not be set up"),
    ],
    ids=[
        "another-model",
        "no-sqlite-vec",
        "no-index-no-url",
        "download-fails",
        "unreadable-index",
        "no-text-model",
    ],
)
async def test_a_failed_index_takes_mood_search_back_and_says_why(
    set_up, tmp_path, monkeypatch, failing, reason
):
    plugin, reported = await set_up(*failing(tmp_path, monkeypatch))

    await plugin._provision_task
    state = await plugin.get_state()

    assert not _offers_mood_search(plugin)
    assert state.state == ModuleHealthState.WARNING
    assert state.message.startswith("Mood search unavailable: ")
    assert reason in state.message
    assert "s3cret" not in state.message
    assert state.missing_packages == []
    assert len(reported) == 1


async def test_a_ready_index_keeps_mood_search_offered(set_up, tmp_path):
    _write_index(tmp_path / INDEX_NAME)
    plugin, reported = await set_up(FakeEmbedder())

    await plugin._provision_task

    assert _offers_mood_search(plugin)
    assert await plugin.get_state() == ModuleState(state=ModuleHealthState.READY)
    assert len(reported) == 1


async def test_mood_search_is_offered_while_its_index_is_set_up(set_up):
    """As it was before provisioning had an outcome to report, so a box whose
    index works never sees the feature come and go."""
    embedder = HeldEmbedder()
    plugin, reported = await set_up(embedder)

    await asyncio.wait_for(embedder.asked.wait(), 5)

    assert _offers_mood_search(plugin)
    assert await plugin.get_state() == ModuleState(state=ModuleHealthState.READY)
    assert reported == []


async def test_an_index_ready_at_the_next_setup_offers_mood_search_again(
    set_up, tmp_path
):
    """What a restart or a saved Jamendo setting does: the plugin is set up
    afresh, and its index is provisioned again."""
    failed, _ = await set_up(FakeEmbedder())
    await failed._provision_task
    assert not _offers_mood_search(failed)

    _write_index(tmp_path / INDEX_NAME)
    plugin, _ = await set_up(FakeEmbedder())
    await plugin._provision_task

    assert _offers_mood_search(plugin)
    assert (await plugin.get_state()).state == ModuleHealthState.READY


async def test_mood_search_turned_off_since_setup_says_nothing_of_its_index(set_up):
    plugin, _ = await set_up(FakeEmbedder())
    await plugin._provision_task

    plugin._context.config.ai_search_enabled = False

    assert (await plugin.get_state()).state == ModuleHealthState.READY


async def test_a_server_without_the_hook_still_finds_the_failure(set_up):
    """At its next health check, as before SDK 3.23."""
    plugin, _ = await set_up(FakeEmbedder(), hooked=False)

    await plugin._provision_task

    assert not _offers_mood_search(plugin)
    assert (await plugin.get_state()).state == ModuleHealthState.WARNING
