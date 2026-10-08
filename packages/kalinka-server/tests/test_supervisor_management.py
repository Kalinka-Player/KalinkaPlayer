"""Settings describe actual installation; Apply alone reconciles the package.

All system commands are replaced: these tests never touch the host's services,
package database or network.
"""

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from kalinka_server import config_route, supervisor_management as management
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_overrides import apply_overrides_with_prefix
from kalinka_server.config_route import register_config_routes
from kalinka_server.config_schema_processor import build_presentation, readonly_paths
from kalinka_server.options_registry import OptionsRegistry


@pytest.fixture
def box(tmp_path, monkeypatch):
    box = SimpleNamespace(
        version=None,
        partial=False,
        calls=[],
        config=tmp_path / "config.json",
        state=tmp_path / "state",
        unavailable=None,
    )
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
    monkeypatch.setattr(
        management,
        "_package_state",
        lambda: (
            ("half-configured", "0.2.0")
            if box.partial
            else ("installed", box.version) if box.version else ("not-installed", "")
        ),
    )
    monkeypatch.setattr(management, "unavailable_reason", lambda: box.unavailable)

    def run(command):
        box.calls.append(command)
        if command[0] == "bash":
            box.version = "0.2.0"
        if "remove" in command:
            box.version = None
            box.partial = False

    monkeypatch.setattr(management, "_run", run)
    return box


def _save(box, value):
    box.config.write_text(json.dumps({management.MANAGE_PATH: value}))


def _result(box):
    return json.loads((box.state / management.RESULT_FILE).read_text())


def _client(box):
    app = FastAPI()
    config = KalinkaConfig()
    overrides = json.loads(box.config.read_text()) if box.config.exists() else {}
    apply_overrides_with_prefix(config, overrides, "base_config.")
    schema = build_presentation(config, {}, {})
    app.state.schema_version = schema.schema_version
    app.state.dynamic_paths = frozenset()
    app.state.readonly_paths = readonly_paths(schema)
    app.state.page_banners = []
    app.state.dynamic_field_registry = {}
    app.state.options_registry = OptionsRegistry()
    app.state.overrides = overrides
    app.state.overrides_file = str(box.config)
    modules = SimpleNamespace(prepared_input_modules={}, prepared_devices={})
    register_config_routes(app, config, modules)
    return TestClient(app)


@pytest.mark.parametrize("version", [None, "0.1.0~ci.12"])
def test_existing_installation_sets_initial_preference_without_saving(box, version):
    box.version = version
    values = _client(box).get("/server/config").json()["values"]
    assert values[management.MANAGE_PATH] is (version is not None)
    assert values[management.STATUS_PATH].startswith(
        "Installed" if version else "Not installed"
    )
    assert not box.config.exists()
    assert box.calls == []


def test_status_is_in_server_settings_but_management_is_expert_only(box):
    schema = _client(box).get("/server/config/schema").json()
    fields = {f["path"]: f for f in schema["expert_fields"]}
    assert fields[management.MANAGE_PATH]["importance"] == "expert"
    assert fields[management.STATUS_PATH]["importance"] == "simple"
    assert fields[management.STATUS_PATH]["readonly"] is True
    assert management.MANAGE_PATH not in json.dumps(schema["pages"])
    general = next(page for page in schema["pages"] if page["id"] == "general")
    server = next(
        section for section in general["sections"] if section["title"] == "Server"
    )
    status = next(
        field for field in server["fields"] if field["path"] == management.STATUS_PATH
    )
    assert status["readonly"] is True


def test_readonly_status_cannot_be_faked_by_a_client(box):
    client = _client(box)
    assert (
        client.put(
            "/server/config",
            json={
                "changes": {management.STATUS_PATH: "Installed"},
            },
        ).status_code
        == 400
    )


def test_saving_stages_work_but_does_not_install_or_lie_about_package(box):
    client = _client(box)
    response = client.put(
        "/server/config",
        json={
            "changes": {management.MANAGE_PATH: True},
        },
    )
    assert response.status_code == 200, response.text
    assert json.loads(box.config.read_text())[management.MANAGE_PATH] is True
    values = client.get("/server/config").json()["values"]
    assert values[management.MANAGE_PATH] is True
    assert values[management.STATUS_PATH].startswith("Not installed.")
    assert "apply and restart" in values[management.STATUS_PATH]
    assert box.calls == []


def test_saved_false_survives_restart_while_package_is_still_installed(box):
    box.version = "0.2.0"
    _save(box, False)
    values = _client(box).get("/server/config").json()["values"]
    assert values[management.MANAGE_PATH] is False
    assert values[management.STATUS_PATH].startswith("Installed (0.2.0). Removal")


@pytest.mark.parametrize("previous", [None, False, True])
def test_a_failed_save_does_not_tell_apply_to_restart(box, monkeypatch, previous):
    if previous is not None:
        _save(box, previous)
    client = _client(box)
    before = client.get("/server/config").json()["values"][management.MANAGE_PATH]
    saved = box.config.read_text() if box.config.exists() else None
    real_save = config_route.save_overrides

    def cannot_save(*args):
        raise OSError("disk is full")

    monkeypatch.setattr(config_route, "save_overrides", cannot_save)
    response = client.put(
        "/server/config",
        json={
            "changes": {management.MANAGE_PATH: not before},
        },
    )
    assert response.status_code == 500
    assert "Could not save" in response.json()["detail"]
    assert (box.config.read_text() if box.config.exists() else None) == saved
    assert (
        client.get("/server/config").json()["values"][management.MANAGE_PATH] is before
    )
    # A later unrelated save must not silently commit the refused request.
    monkeypatch.setattr(config_route, "save_overrides", real_save)
    assert (
        client.put(
            "/server/config",
            json={"changes": {"base_config.server.service_name": "Living room"}},
        ).status_code
        == 200
    )
    assert json.loads(box.config.read_text()).get(management.MANAGE_PATH) is previous
    assert box.calls == []


def test_real_state_is_rechecked_even_after_manual_package_changes(box):
    client = _client(box)
    box.version = "0.3.0"
    assert (
        client.get("/server/config")
        .json()["values"][management.STATUS_PATH]
        .startswith("Installed (0.3.0)")
    )


def test_unsupported_hosts_refuse_the_setting_before_saving(box):
    box.unavailable = "Requires a Debian installation."
    client = _client(box)
    body = {"changes": {management.MANAGE_PATH: True}}
    issues = client.post("/server/config/validate", json=body).json()["issues"]
    response = client.put("/server/config", json=body)
    assert response.status_code == 422
    assert response.json()["issues"] == issues
    assert issues[0]["path"] == management.MANAGE_PATH
    assert not box.config.exists()


@pytest.mark.parametrize("version", [None, "0.2.0"])
def test_no_explicit_preference_never_changes_a_box(box, version):
    box.version = version
    assert management.apply_saved(box.config, box.state) == 0
    assert box.calls == []
    assert not box.state.exists()


@pytest.mark.parametrize("desired,version", [(True, "0.1.0"), (False, None)])
def test_matching_state_never_downloads_upgrades_or_stops_core(box, desired, version):
    _save(box, desired)
    box.version = version
    assert management.apply_saved(box.config, box.state) == 0
    assert box.calls == []
    assert box.version == version


def test_install_stops_core_then_uses_verified_installer_and_records_success(box):
    _save(box, True)
    assert management.apply_saved(box.config, box.state) == 0
    assert box.calls[0] == ["systemctl", "stop", "kalinka.service"]
    assert box.calls[-1] == ["bash", str(management.INSTALLER)]
    assert _result(box)["status"] == "ok"
    assert management.status_text(True, box.state) == "Installed (0.2.0)."
    assert (box.state / management.RESULT_FILE).stat().st_mode & 0o777 == 0o644


def test_remove_keeps_network_dependencies_and_setup_data(box):
    _save(box, False)
    box.version = "0.2.0"
    assert management.apply_saved(box.config, box.state) == 0
    assert box.calls == [
        ["systemctl", "stop", "kalinka.service"],
        [
            "apt-get",
            "-o",
            "DPkg::Lock::Timeout=300",
            "remove",
            "-y",
            "kalinka-supervisor",
        ],
    ]
    assert management.status_text(False, box.state) == "Not installed."


@pytest.mark.parametrize("desired", [True, False])
def test_failure_reports_actual_state_and_can_be_retried(box, monkeypatch, desired):
    _save(box, desired)
    box.version = None if desired else "0.2.0"
    run = management._run

    def fail(command):
        if command[0] == "apt-get":
            raise subprocess.CalledProcessError(100, command)
        run(command)

    monkeypatch.setattr(management, "_run", fail)
    assert management.apply_saved(box.config, box.state) == 1
    assert _result(box)["status"] == "failed"
    status = management.status_text(desired, box.state)
    assert status.startswith("Not installed." if desired else "Installed (0.2.0).")
    assert "failed" in status and "retry" in status
    monkeypatch.setattr(management, "_run", run)
    assert management.apply_saved(box.config, box.state) == 0
    assert "failed" not in management.status_text(desired, box.state)


def test_a_successful_command_without_the_package_is_still_a_failure(box, monkeypatch):
    _save(box, True)
    monkeypatch.setattr(management, "_run", lambda command: None)
    assert management.apply_saved(box.config, box.state) == 1
    assert "did not change" in _result(box)["error"]


def test_disabling_removes_even_an_incomplete_installation(box):
    _save(box, False)
    box.partial = True
    status = management.status_text(False, box.state)
    assert "incomplete" in status
    assert "Removal will run" in status
    assert management.apply_saved(box.config, box.state) == 0
    assert "remove" in box.calls[-1]
    assert management.status_text(False, box.state) == "Not installed."


@pytest.mark.parametrize("bad", ["true", 1, None, {"command": "anything"}])
def test_root_helper_accepts_only_a_boolean(box, bad):
    _save(box, bad)
    assert management.apply_saved(box.config, box.state) == 1
    assert box.calls == []


def test_interrupted_work_is_visible_and_waits_for_another_explicit_restart(
    box, monkeypatch
):
    _save(box, True)

    def interrupted(command):
        raise KeyboardInterrupt

    monkeypatch.setattr(management, "_run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        management.apply_saved(box.config, box.state)
    assert "interrupted" in management.status_text(True, box.state)


def test_debian_restart_always_starts_core_even_if_management_fails():
    package = Path(__file__).resolve().parents[1]
    unit = (package / "scripts/kalinka-restart.service").read_text()
    dropin = (package / "scripts/kalinka-restart-supervisor.conf").read_text()
    assert "ExecStart=/bin/systemctl restart kalinka.service" in unit
    assert "ExecStartPre=-/usr/bin/python3 /opt/kalinka/manage-supervisor.py" in dropin
    assert "TimeoutStartSec=infinity" in dropin


@pytest.mark.parametrize(
    "output,code,expected",
    [
        ("installed 0.2.0", 0, "0.2.0"),
        ("config-files 0.2.0", 0, None),
        ("half-configured 0.2.0", 0, None),
        ("", 1, None),
    ],
)
def test_package_state_excludes_removed_or_incomplete_packages(
    monkeypatch, output, code, expected
):
    monkeypatch.setattr(
        management.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=output, returncode=code),
    )
    assert management.installed_version() == expected
