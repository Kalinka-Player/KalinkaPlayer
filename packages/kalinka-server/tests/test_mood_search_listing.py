"""Jamendo's mood search as clients find it in the module listing: offered
while its index can be searched, and otherwise listed with the reason, as soon
as setting the index up has failed."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager

import pytest

from kalinka_plugin_jamendo.module_setup import KalinkaPluginJamendo
from kalinka_plugin_sdk.events import ModulesChangedEvent, PlayQueueEventType

from kalinka_server import server
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.player_setup import modules
from kalinka_server.text_embedder import SharedTextEmbedder
from tests.app_harness import isolate_app, running


@pytest.fixture(autouse=True)
def jamendo_alone(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        modules, "_scan_entry_points", lambda: iter([("jamendo", KalinkaPluginJamendo)])
    )
    # Its runtime installed and its model present, so the index decides.
    monkeypatch.setattr(SharedTextEmbedder, "missing_runtime", lambda self: [])

    async def available(self):
        return True

    monkeypatch.setattr(SharedTextEmbedder, "available", available)


@asynccontextmanager
async def _started(tmp_path, **jamendo):
    settings = {
        "client_id": "x",
        "ai_index_path": str(tmp_path / "jamendo_index_v2.sqlite"),
        **jamendo,
    }
    app = await server.create_app(
        str(tmp_path / "overrides.json"),
        KalinkaConfig(),
        {f"input_modules.jamendo.{key}": value for key, value in settings.items()},
    )
    async with running(app) as client:
        yield client


def _jamendo(event):
    listing = event if isinstance(event, ModulesChangedEvent) else event.state.modules
    return next(m for m in listing.input_modules if m.name == "jamendo")


async def test_a_failed_index_is_listed_without_ai_search_and_with_the_reason(
    tmp_path,
):
    async with _started(tmp_path, ai_index_url="") as client:
        bus = modules.player_context.playqueue_eventbus
        async with bus.stream([PlayQueueEventType.ModulesChanged]) as events:
            # Well inside the 10 s the regular check would take.
            async with asyncio.timeout(5):
                async for event in events:
                    if _jamendo(event).state == "warning":
                        break
        listed = (await client.get("/server/modules")).json()["input_modules"]

    jamendo = next(m for m in listed if m["name"] == "jamendo")
    assert jamendo["state"] == "warning"
    assert jamendo["error_message"].startswith("Mood search unavailable: ")
    assert "**Mood index download URL** is empty" in jamendo["error_message"]
    assert jamendo["capabilities"] == ["catalogs"]


async def test_a_working_index_is_listed_with_ai_search(tmp_path):
    con = sqlite3.connect(tmp_path / "jamendo_index_v2.sqlite")
    try:
        con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        con.execute("INSERT INTO meta VALUES ('dtype', 'int8')")
        con.commit()
    finally:
        con.close()

    async with _started(tmp_path) as client:
        plugin = modules.prepared_input_modules["jamendo"].plugin_instance
        await asyncio.wait_for(plugin._provision_task, 5)
        listed = (await client.get("/server/modules")).json()["input_modules"]

    jamendo = next(m for m in listed if m["name"] == "jamendo")
    assert jamendo["state"] == "ready"
    assert jamendo["capabilities"] == ["ai_search", "catalogs"]
