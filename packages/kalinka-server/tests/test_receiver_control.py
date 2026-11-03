"""Optional receiver management stays separate from input serving and media."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from kalinka_plugin_sdk import (
    InputModule,
    ModuleConfig,
    ModuleHealthState,
    ModuleState,
    OpenPairing,
    ReceiverDescriptor,
    ReceiverError,
    ReceiverSnapshot,
    ReceiversChangedEvent,
)
from kalinka_plugin_sdk.plugin import InputModulePlugin
from kalinka_server.player_setup import PreparedModuleCollection, PreparedPlugin
from kalinka_server.receiver_control import ReceiverRegistry, register_receiver_routes
from tests.test_renderer_events_state import make_state


class Interface(InputModule):
    def module_name(self):
        return "test"


class Controller(InputModulePlugin):
    PLUGIN_ID = "test"
    SETUP_WHEN_DISABLED = True
    RECEIVER = ReceiverDescriptor(
        title="Test receiver", kind="test", commands=("open_pairing",)
    )

    async def setup(self, context):
        self.context = context
        self.calls = 0
        self.failure = None

    def get_interface(self):
        return Interface()

    def get_receiver_control(self):
        return self

    async def get_state(self):
        return ModuleState(
            state=(
                ModuleHealthState.READY
                if self.context.config.enabled
                else ModuleHealthState.DISABLED
            )
        )

    async def snapshot(self):
        return ReceiverSnapshot(
            enabled=self.context.config.enabled,
            state="ready" if self.context.config.enabled else "disabled",
        )

    async def execute(self, command):
        self.calls += 1
        if self.failure:
            raise self.failure
        return await self.snapshot()


async def prepared(cls=Controller):
    return await PreparedPlugin.setup(
        cls,
        SimpleNamespace(
            config=ModuleConfig(name="test", enabled=False), plugin_id="test"
        ),
    )


async def test_opt_in_setup_does_not_serve_disabled_input_and_needs_no_second_setup():
    entry = await prepared()
    assert entry.plugin_instance.context is entry.plugin_context
    assert entry.health_state == ModuleHealthState.READY
    assert not entry.serves
    entry.plugin_context.config.enabled = True
    await entry.refresh_state()
    assert entry.serves

    class OldPlugin(Controller):
        SETUP_WHEN_DISABLED = False
        RECEIVER = None

    old = await prepared(OldPlugin)
    assert not hasattr(old.plugin_instance, "context")
    assert old.interface is None and not old.serves


@pytest.fixture
async def api():
    entry = await prepared()
    modules = PreparedModuleCollection(prepared_input_modules={"test": entry})
    events = []
    registry = ReceiverRegistry(modules, events.append)
    app = FastAPI()
    register_receiver_routes(app, registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield client, registry, entry, events
    await registry.close()


async def test_inventory_contains_disabled_and_failed_receivers(api):
    client, registry, entry, _ = api
    response = await client.get("/receivers")
    assert response.status_code == 200
    row = response.json()["receivers"][0]
    assert row["receiver_id"] == "test" and row["title"] == "Test receiver"
    assert row["state"] == "disabled" and not row["enabled"]
    entry.plugin_instance = None
    entry.error_message = "Missing dependency"
    failed = (await client.get("/receivers/test")).json()
    assert failed["state"] == "error" and failed["message"] == "Missing dependency"
    assert (await client.get("/receivers/unknown")).status_code == 404
    registry.modules.prepared_input_modules.clear()
    assert (await client.get("/receivers")).json()["receivers"] == []


async def test_password_reveal_is_explicit_opt_in_and_never_broadcast(api):
    from unittest.mock import AsyncMock

    client, registry, entry, events = api
    assert (await client.post("/receivers/test/password")).status_code == 409
    entry.plugin_context.config.enabled = True
    entry.plugin_instance.snapshot = AsyncMock(
        return_value=ReceiverSnapshot(
            enabled=True,
            state="ready",
            password_required=True,
            password_reveal_available=True,
        )
    )
    entry.plugin_instance.reveal_password = AsyncMock(return_value="configured secret")
    snapshot = await client.get("/receivers/test")
    assert "configured secret" not in snapshot.text
    assert (await client.get("/receivers/test/password")).status_code == 405
    revealed = await client.post("/receivers/test/password")
    assert revealed.json() == {"password": "configured secret"}
    assert revealed.headers["cache-control"] == "no-store"
    registry.changed()
    await registry._task
    assert "configured secret" not in events[-1].model_dump_json()
    entry.plugin_context.config.enabled = False
    assert (await client.post("/receivers/test/password")).status_code == 409
    assert entry.plugin_instance.reveal_password.await_count == 1


async def test_demo_gate_refuses_password_reveal_and_pairing_approval():
    from kalinka_server.demo_mode import DemoReadOnlyGate

    app = FastAPI()
    app.add_middleware(DemoReadOnlyGate, enabled=lambda: True)
    register_receiver_routes(app, None)  # A refusal must not reach any controller.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for path in ("/receivers/test/password", "/receivers/test/commands"):
            assert (
                await client.post(
                    path,
                    json={
                        "command": "answer_pairing",
                        "request_id": "request",
                        "accept": True,
                    },
                )
            ).status_code == 403


async def test_pairing_answers_require_a_real_boolean_and_a_request_token(api):
    client, _, _, _ = api
    for body in (
        {"command": "answer_pairing", "accept": True},
        {"command": "answer_pairing", "request_id": "request", "accept": "yes"},
    ):
        assert (
            await client.post("/receivers/test/commands", json=body)
        ).status_code == 422


async def test_commands_are_typed_synchronous_and_not_replayed(api):
    client, _, entry, _ = api
    response = await client.post(
        "/receivers/test/commands", json={"command": "open_pairing"}
    )
    assert response.status_code == 200 and response.json()["receiver_id"] == "test"
    assert entry.plugin_instance.calls == 1
    assert (
        await client.post("/receivers/test/commands", json={"command": "close_pairing"})
    ).status_code == 422
    assert (
        await client.post(
            "/receivers/test/commands",
            json={"command": "open_pairing", "unexpected": 1},
        )
    ).status_code == 422
    assert (
        await client.post(
            "/receivers/test/commands",
            json={"command": "close_pairing", "window_id": "old"},
        )
    ).status_code == 400
    assert entry.plugin_instance.calls == 1
    entry.plugin_instance.failure = ReceiverError(
        "stale_target", "Refresh this session"
    )
    response = await client.post(
        "/receivers/test/commands", json={"command": "open_pairing"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_target"
    assert entry.plugin_instance.calls == 2


async def test_published_snapshot_is_deduplicated_and_survives_queue_replay(api):
    _, registry, entry, events = api
    registry.changed()
    await registry._task
    assert len(events) == 1 and isinstance(events[0], ReceiversChangedEvent)
    state = make_state().apply(events[0].model_copy(update={"seq": 1}))
    assert state.receivers.receivers[0].state == "disabled"
    registry.changed()
    await registry._task
    assert len(events) == 1
    entry.plugin_context.config.enabled = True
    registry.changed("test")
    await registry._task
    assert len(events) == 2 and events[-1].receivers[0].enabled


async def test_timeout_cancels_command_and_reports_uncertain_result(api, monkeypatch):
    from kalinka_server import receiver_control

    client, _, entry, _ = api
    cancelled = asyncio.Event()

    async def slow(command):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    entry.plugin_instance.execute = slow
    monkeypatch.setattr(receiver_control, "CALL_TIMEOUT", 0.01)
    response = await client.post(
        "/receivers/test/commands",
        json=OpenPairing(command="open_pairing").model_dump(),
    )
    assert response.status_code == 504 and cancelled.is_set()
    assert "refresh" in response.json()["detail"]["message"]
