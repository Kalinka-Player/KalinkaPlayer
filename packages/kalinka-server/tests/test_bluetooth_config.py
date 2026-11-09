"""Bluetooth latency settings are judged as a batch before persistence."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kalinka_plugin_bluetooth import BluetoothConfig, KalinkaPluginBluetooth
from kalinka_plugin_sdk import ModuleHealthState, paths
from kalinka_plugin_sdk.live_audio import LivePolicy
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_route import register_config_routes

PREFIX = "input_modules.bluetooth."


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: str(tmp_path))
    config = BluetoothConfig()
    plugin = KalinkaPluginBluetooth()
    plugin.context = SimpleNamespace(config=config)
    app = FastAPI()
    app.state.config = KalinkaConfig(server={"manage_box": False})
    app.state.schema_version = "1"
    app.state.dynamic_paths = frozenset()
    app.state.readonly_paths = frozenset()
    app.state.overrides = {}
    app.state.overrides_file = str(tmp_path / "overrides.json")
    modules = SimpleNamespace(
        prepared_input_modules={
            "bluetooth": SimpleNamespace(
                plugin_context=plugin.context,
                plugin_instance=plugin,
                health_state=ModuleHealthState.DISABLED,
            )
        },
        prepared_devices={},
    )
    register_config_routes(app, app.state.config, modules, settings_changed=AsyncMock())
    with TestClient(app) as result:
        yield result, config


def payload(changes):
    return {"changes": {PREFIX + key: value for key, value in changes.items()}}


@pytest.mark.parametrize(
    "changed,value",
    [
        ("latency_ms", 500),
        ("latency_ms", 250),
        ("max_backlog_ms", 60),
        ("max_backlog_ms", 21),
    ],
)
def test_invalid_latency_policy_is_refused_before_any_changes_are_saved(
    client, tmp_path, changed, value
):
    api, config = client
    before = config.model_dump()
    body = payload({changed: value, "device_name": "New name"})

    dry_run = api.post("/server/config/validate", json=body)
    assert dry_run.status_code == 200
    [issue] = dry_run.json()["issues"]
    assert issue["path"] == PREFIX + changed
    assert issue["severity"] == "error"
    assert config.model_dump() == before
    assert not (tmp_path / "overrides.json").exists()

    saved = api.put("/server/config", json=body)
    assert saved.status_code == 422
    assert saved.json()["issues"] == [issue]
    assert config.model_dump() == before
    assert api.app.state.overrides == {}
    assert not (tmp_path / "overrides.json").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"latency_ms": 500, "max_backlog_ms": 501},
        {"max_backlog_ms": 501, "latency_ms": 500},
        {"latency_ms": 20, "max_backlog_ms": 21},
        {"device_name": "New name"},
    ],
)
def test_valid_settings_save_together_even_if_one_field_alone_would_be_invalid(
    client, tmp_path, changes
):
    api, config = client
    before = config.model_dump()
    body = payload(changes)
    dry_run = api.post("/server/config/validate", json=body)
    assert dry_run.status_code == 200
    assert dry_run.json()["issues"] == []
    assert config.model_dump() == before

    saved = api.put("/server/config", json=body)
    assert saved.status_code == 200
    assert saved.json()["issues"] == []
    persisted = json.loads((tmp_path / "overrides.json").read_text())
    for field, value in changes.items():
        assert getattr(config, field) == value
        assert persisted[PREFIX + field] == value
    LivePolicy(config.latency_ms, config.max_backlog_ms)
