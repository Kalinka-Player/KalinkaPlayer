"""Core only ever asks root for a catalog release or a plugin package; the
helper repeats the checks that matter from its own fetch of the catalog."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import httpx
from packaging.version import Version
import pytest

from kalinka_server.plugin_management import operations as module
from kalinka_server.plugin_management.catalog import PublicCatalog
from kalinka_server.plugin_management.compatibility import (
    CatalogCompatibility,
    HostEnvironment,
)
from kalinka_server.plugin_management.operations import (
    REQUEST_FILE,
    RESULT_FILE,
    Failure,
    OperationRequest,
    PluginOperations,
    Refusal,
    apply,
)
from tests.plugin_catalog_fakes import catalog_document

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
HOST = HostEnvironment(
    "linux", "x86_64", "deb", "debian", "13", "5.3.0", "3.5.0", "3.11.0"
)
PACKAGE = b"not really a deb"
DIGEST = hashlib.sha256(PACKAGE).hexdigest()


def document():
    doc = catalog_document()
    artifact = doc["plugins"][0]["releases"][0]["artifacts"][0]
    artifact["sha256"] = DIGEST
    artifact["size_bytes"] = len(PACKAGE)
    return doc


def with_release(doc, version, digest):
    """Add a stable release ``version`` of the demo plugin to ``doc``."""
    plugin = doc["plugins"][0]
    release = deepcopy(plugin["releases"][0])
    release["version"] = version
    release["source_tag"] = f"v{version}"
    artifact = release["artifacts"][0]
    artifact["filename"] = f"kalinka-plugin-demo_{version}_all.deb"
    artifact["package"]["version"] = version
    artifact["sha256"] = digest
    artifact["url"] = f"https://example.org/releases/v{version}/{artifact['filename']}"
    plugin["releases"].append(release)
    return doc


async def loaded_catalog(doc):
    catalog = PublicCatalog(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=doc))
    )
    await catalog.refresh()
    assert catalog.snapshot()["status"] == "available"
    return catalog


class Box:
    """A server whose package database, trigger and clock are under test control."""

    def __init__(self, tmp_path):
        self.state = tmp_path / "state"
        self.installed = {}
        self.restarts = 0
        self.now = NOW
        self.supported = True
        self.restart_error = None
        self.restarting = False

    def restart(self):
        if self.restart_error:
            raise self.restart_error
        self.restarts += 1

    def operations(self, catalog, host=HOST, renderers=()):
        return PluginOperations(
            catalog,
            CatalogCompatibility(lambda: host, lambda: NOW),
            lambda: tuple(renderers),
            state_dir=lambda: str(self.state),
            supported=lambda: self.supported,
            package_version=self.installed.get,
            newer=lambda candidate, installed: Version(candidate) > Version(installed),
            restart=self.restart,
            restarting=lambda: self.restarting,
            clock=lambda: self.now,
        )

    def request(self):
        return json.loads((self.state / REQUEST_FILE).read_text())


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


async def refused(coroutine):
    with pytest.raises(Refusal) as info:
        await coroutine
    return info.value.status, info.value.code


async def test_install_writes_a_catalog_release_request_and_restarts(box):
    ops = box.operations(await loaded_catalog(document()))
    body = await ops.request("install", "demo")
    assert body == {
        "status": "restarting",
        "operation": "install",
        "plugin_id": "demo",
        "distribution": "kalinka-plugin-demo",
        "version": "1.0.0",
        "installed_version": None,
    }
    assert box.restarts == 1
    request = box.request()
    assert request == {
        "schema_version": 1,
        "action": "install",
        "plugin_id": "demo",
        "distribution": "kalinka-plugin-demo",
        "version": "1.0.0",
        "artifact_sha256": DIGEST,
        "requested_at": NOW.isoformat(),
    }
    assert stat.S_IMODE(os.stat(box.state / REQUEST_FILE).st_mode) == 0o640
    # Root reads only what it will check against its own fetch: no URL here.
    assert "url" not in json.dumps(request)


async def test_uninstall_names_the_installed_package(box):
    box.installed["kalinka-plugin-demo"] = "1.0.0"
    ops = box.operations(await loaded_catalog(document()))
    body = await ops.request("uninstall", "demo")
    assert body["operation"] == "uninstall" and body["version"] == "1.0.0"
    request = box.request()
    assert request["action"] == "uninstall"
    assert request["version"] is None and request["artifact_sha256"] is None
    assert box.restarts == 1


async def test_refusals_leave_no_request_behind(box):
    catalog = await loaded_catalog(document())
    ops = box.operations(catalog)
    box.supported = False
    assert await refused(ops.request("install", "demo")) == (
        503,
        "installer_unavailable",
    )
    box.supported = True
    assert await refused(ops.request("install", "radio")) == (404, "unknown_plugin")
    assert await refused(ops.request("uninstall", "demo")) == (409, "not_installed")
    box.installed["kalinka-plugin-demo"] = "1.0.0"
    assert await refused(ops.request("install", "demo")) == (
        409,
        "already_installed",
    )
    del box.installed["kalinka-plugin-demo"]
    older = HostEnvironment(
        "linux", "x86_64", "deb", "debian", "13", "5.2.0", "3.5.0", "3.11.0"
    )
    status, code = await refused(
        box.operations(catalog, host=older).request("install", "demo")
    )
    assert (status, code) == (409, "incompatible")
    assert not (box.state / REQUEST_FILE).exists()
    assert box.restarts == 0


async def test_incompatibility_names_the_reason(box):
    older = HostEnvironment(
        "linux", "x86_64", "deb", "debian", "13", "5.2.0", "3.5.0", "3.11.0"
    )
    ops = box.operations(await loaded_catalog(document()), host=older)
    with pytest.raises(Refusal) as info:
        await ops.request("install", "demo")
    assert "incompatible_server (required >=5.3,<6, installed 5.2.0)" in str(info.value)


async def test_bundled_plugins_belong_to_the_server(box):
    doc = document()
    doc["plugins"][0]["delivery"] = "bundle"
    doc["plugins"][0]["releases"] = []
    ops = box.operations(await loaded_catalog(doc))
    box.installed["kalinka-plugin-demo"] = "1.0.0"
    for action in ("install", "uninstall"):
        assert await refused(ops.request(action, "demo")) == (409, "bundle_managed")


async def test_catalog_must_be_available(box):
    catalog = PublicCatalog(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    )
    ops = box.operations(catalog)
    assert await refused(ops.request("install", "demo")) == (
        503,
        "catalog_unavailable",
    )


async def test_one_request_at_a_time_until_it_goes_stale(box):
    ops = box.operations(await loaded_catalog(document()))
    await ops.request("install", "demo")
    assert await refused(ops.request("install", "demo")) == (
        409,
        "operation_in_progress",
    )
    box.now = NOW + timedelta(minutes=16)
    await ops.request("install", "demo")
    assert box.restarts == 2


async def test_none_while_the_server_is_already_restarting(box):
    """A trigger fired while kalinka-restart.service runs starts no second run,
    so a request its helper has already passed would never be carried out."""
    box.installed["kalinka-plugin-demo"] = "1.0.0"
    ops = box.operations(
        await loaded_catalog(with_release(document(), "1.1.0", DIGEST))
    )
    await ops.request("update", "demo")
    box.restarting = True
    (box.state / REQUEST_FILE).unlink()
    assert await refused(ops.request("update", "demo")) == (
        409,
        "operation_in_progress",
    )
    assert not (box.state / REQUEST_FILE).exists()
    assert box.restarts == 1
    box.restarting = False
    box.installed["kalinka-plugin-demo"] = "1.1.0"
    await ops.request("uninstall", "demo")
    assert box.restarts == 2


async def test_a_failed_restart_request_withdraws_the_request(box):
    ops = box.operations(await loaded_catalog(document()))
    box.restart_error = OSError("read-only")
    assert await refused(ops.request("install", "demo")) == (500, "restart_failed")
    assert not (box.state / REQUEST_FILE).exists()


def test_last_result_is_the_helpers_record_or_nothing(box):
    ops = box.operations(None)
    assert ops.last_result() is None
    box.state.mkdir()
    (box.state / RESULT_FILE).write_text("[]")
    assert ops.last_result() is None
    (box.state / RESULT_FILE).write_text(json.dumps({"status": "ok"}))
    assert ops.last_result() == {"status": "ok"}


@pytest.mark.parametrize(
    "change",
    [
        {"distribution": "kalinka-server"},
        {"distribution": "kalinka-plugin-sdk"},
        {"distribution": "Kalinka-Plugin-Demo"},
        {"plugin_id": "../demo"},
        {"version": None},
        {"artifact_sha256": None},
        {"artifact_sha256": "A" * 64},
        {"action": "purge"},
        {"url": "https://example.org/x.deb"},
    ],
)
def test_requests_root_would_act_on_are_narrow(change):
    request = {
        "action": "install",
        "plugin_id": "demo",
        "distribution": "kalinka-plugin-demo",
        "version": "1.0.0",
        "artifact_sha256": DIGEST,
        "requested_at": NOW.isoformat(),
    }
    OperationRequest.model_validate(request)
    with pytest.raises(ValueError):
        OperationRequest.model_validate(request | change)


class Root:
    """The helper's view of the box: catalog, downloads, commands, dpkg."""

    def __init__(self, tmp_path):
        self.state = tmp_path / "state"
        self.state.mkdir(exist_ok=True)
        self.now = NOW.timestamp()
        self.catalog = {"status": "available", "plugins": document()["plugins"]}
        self.installed = {}
        self.calls = []
        self.plans = {
            "install": "Inst kalinka-plugin-demo (1.0.0 local-deb [all])\n",
            "remove": "Remv kalinka-plugin-demo [1.0.0]\n",
        }
        self.downloads = []
        self.fail = None

    def fetch_catalog(self):
        if self.catalog is None:
            raise Failure("The plugin catalog could not be fetched.")
        return deepcopy(self.catalog)

    def download(self, url, sha256, size_bytes, dest, keepalive=lambda: None):
        self.downloads.append((url, sha256, size_bytes))
        dest.write_bytes(PACKAGE)
        keepalive()
        assert stat.S_IMODE(os.stat(dest.parent).st_mode) == 0o755

    def run(self, command):
        self.calls.append(command)
        if command[0] == "apt-get" and "-s" in command:
            return self.plans["remove" if "remove" in command else "install"]
        if command[0] == "apt-get":
            if self.fail == "apt":
                raise subprocess.CalledProcessError(100, command)
            if "install" in command:
                package = Path(command[-1])
                assert (
                    package.exists() and stat.S_IMODE(package.stat().st_mode) == 0o644
                )
                self.installed["kalinka-plugin-demo"] = package.name.split("_")[1]
            else:
                self.installed.pop(command[-1], None)
        return ""

    def clock(self):
        return self.now

    def apply(self, request):
        (self.state / REQUEST_FILE).write_text(json.dumps(request))
        code = apply(
            self.state,
            fetch_catalog=self.fetch_catalog,
            download=self.download,
            run=self.run,
            package_version=self.installed.get,
            clock=self.clock,
        )
        result = json.loads((self.state / RESULT_FILE).read_text())
        assert stat.S_IMODE(os.stat(self.state / RESULT_FILE).st_mode) == 0o644
        assert not (self.state / REQUEST_FILE).exists()
        return code, result


INSTALL = {
    "schema_version": 1,
    "action": "install",
    "plugin_id": "demo",
    "distribution": "kalinka-plugin-demo",
    "version": "1.0.0",
    "artifact_sha256": DIGEST,
    "requested_at": NOW.isoformat(),
}
UNINSTALL = INSTALL | {"action": "uninstall", "version": None, "artifact_sha256": None}


@pytest.fixture
def root(tmp_path):
    return Root(tmp_path)


def test_helper_installs_the_catalog_artifact_with_core_stopped(root):
    code, result = root.apply(INSTALL)
    assert code == 0, result
    assert result["status"] == "ok" and result["error"] is None
    assert (
        result["action"] == "install"
        and result["distribution"] == "kalinka-plugin-demo"
    )
    assert result["started_at"] <= result["finished_at"]
    url = root.catalog["plugins"][0]["releases"][0]["artifacts"][0]["url"]
    assert root.downloads == [(url, DIGEST, len(PACKAGE))]
    steps = [c[0] if c[0] != "apt-get" else " ".join(c[3:5]) for c in root.calls]
    assert steps == ["-s install", "systemctl", "install -y"]
    assert root.calls[1] == ["systemctl", "stop", "kalinka.service"]
    assert root.calls[2][-1].endswith("/kalinka-plugin-demo_1.0.0_all.deb")


def test_helper_removes_the_package_with_core_stopped(root):
    root.installed["kalinka-plugin-demo"] = "1.0.0"
    code, result = root.apply(UNINSTALL)
    assert code == 0 and result["status"] == "ok"
    assert root.calls == [
        [
            "apt-get",
            "-o",
            "DPkg::Lock::Timeout=300",
            "-s",
            "remove",
            "-y",
            "kalinka-plugin-demo",
        ],
        ["systemctl", "stop", "kalinka.service"],
        [
            "apt-get",
            "-o",
            "DPkg::Lock::Timeout=300",
            "remove",
            "-y",
            "kalinka-plugin-demo",
        ],
    ]
    assert root.downloads == []


def test_nothing_to_do_without_a_request(root):
    assert (
        apply(
            root.state,
            fetch_catalog=None,
            download=None,
            run=None,
            package_version=None,
        )
        == 0
    )
    assert not (root.state / RESULT_FILE).exists()


@pytest.mark.parametrize(
    "change, error",
    [
        ({"version": "1.0.1"}, "no longer lists"),
        ({"artifact_sha256": "b" * 64}, "no longer lists"),
        ({"distribution": "kalinka-plugin-other"}, "no longer lists"),
        ({"plugin_id": "other"}, "no longer lists"),
    ],
)
def test_helper_installs_only_what_its_own_catalog_fetch_pins(root, change, error):
    code, result = root.apply(INSTALL | change)
    assert code == 1 and result["status"] == "failed"
    assert error in result["error"]
    assert root.downloads == [] and root.calls == []


def test_helper_refuses_a_withdrawn_or_bundled_release(root):
    root.catalog["plugins"][0]["releases"][0]["withdrawn"] = True
    assert root.apply(INSTALL)[1]["status"] == "failed"
    root.catalog["plugins"][0]["releases"][0]["withdrawn"] = False
    root.catalog["plugins"][0]["delivery"] = "bundle"
    assert root.apply(INSTALL)[1]["status"] == "failed"
    assert root.calls == []


def test_helper_needs_the_catalog(root):
    root.catalog = None
    code, result = root.apply(INSTALL)
    assert code == 1 and "catalog" in result["error"]


def test_install_plan_may_not_touch_the_server(root):
    root.plans["install"] = (
        "Remv kalinka-plugin-sdk [3.5.0]\nInst kalinka-plugin-demo (1.0.0 local-deb [all])\n"
    )
    code, result = root.apply(INSTALL)
    assert result["error"] == "Installing it would remove kalinka-plugin-sdk."
    root.plans["install"] = "Inst kalinka-server (6.0.0 local-deb [all])\n"
    assert (
        root.apply(INSTALL)[1]["error"] == "Installing it would replace kalinka-server."
    )
    assert all(c[0] != "systemctl" for c in root.calls)


def test_removal_plan_may_only_take_the_plugin(root):
    root.installed["kalinka-plugin-demo"] = "1.0.0"
    root.plans["remove"] = (
        "Remv kalinka-plugin-demo [1.0.0]\nRemv kalinka-server [5.3.0]\n"
    )
    code, result = root.apply(UNINSTALL)
    assert result["error"] == "Removing it would also remove kalinka-server."
    assert len(root.calls) == 1


def test_apt_failure_is_recorded_without_its_output(root):
    root.fail = "apt"
    code, result = root.apply(INSTALL)
    assert code == 1
    assert result["error"] == "apt-get did not complete; see the helper's log."
    assert result["finished_at"] is not None


def test_package_state_is_checked_after_apt(root):
    root.run = lambda command: root.plans["install"] if "-s" in command else ""
    code, result = root.apply(INSTALL)
    assert result["error"] == "The package did not end up installed."
    root.installed["kalinka-plugin-demo"] = "1.0.0"
    root.run = lambda command: root.plans["remove"] if "-s" in command else ""
    assert root.apply(UNINSTALL)[1]["error"] == "The package is still installed."


def test_an_unreadable_request_is_consumed_and_reported(root):
    (root.state / REQUEST_FILE).write_text('{"action": "format_disk"}')
    code = apply(
        root.state,
        fetch_catalog=root.fetch_catalog,
        download=root.download,
        run=root.run,
        package_version=root.installed.get,
    )
    result = json.loads((root.state / RESULT_FILE).read_text())
    assert code == 1 and result["error"] == "The request could not be read."
    assert not (root.state / REQUEST_FILE).exists() and root.calls == []


@pytest.mark.parametrize(
    "age_s, change",
    [
        pytest.param(module.STALE_REQUEST_SECONDS, {}, id="as old as the limit"),
        pytest.param(3 * 24 * 3600, {}, id="days old"),
        pytest.param(-60, {}, id="from the future"),
        pytest.param(0, {"requested_at": "yesterday"}, id="no time"),
        pytest.param(0, {"requested_at": "2026-10-12T12:00:00"}, id="no zone"),
    ],
)
def test_helper_drops_a_request_nobody_is_waiting_for(root, caplog, age_s, change):
    """A request left behind would run at whatever restart came next, a
    settings Apply days later, with nobody expecting it."""
    root.now += age_s
    code, result = root.apply(INSTALL | change)
    assert code == 0
    assert (result["status"], result["error"]) == ("failed", module.EXPIRED)
    assert result["plugin_id"] == "demo" and result["finished_at"] is not None
    assert root.calls == [] and root.downloads == []
    assert "the request expired" in caplog.text


def test_helper_carries_out_a_request_just_short_of_the_limit(root):
    root.now += module.STALE_REQUEST_SECONDS - 1
    code, result = root.apply(INSTALL)
    assert code == 0 and result["status"] == "ok", result
    assert root.installed["kalinka-plugin-demo"] == "1.0.0"


def test_the_result_shows_running_while_the_helper_works(root):
    seen = []

    def run(command):
        seen.append(json.loads((root.state / RESULT_FILE).read_text())["status"])
        return root.run(command)

    (root.state / REQUEST_FILE).write_text(json.dumps(INSTALL))
    apply(
        root.state,
        fetch_catalog=root.fetch_catalog,
        download=root.download,
        run=run,
        package_version=root.installed.get,
        clock=root.clock,
    )
    assert seen == ["running"] * 3


class Server:
    """An upstream that serves the package behind GitHub's style of redirect."""

    def __init__(
        self,
        body=PACKAGE,
        hops=1,
        final="https://cdn.example.org/x.deb?X-Amz-Signature=abc",
    ):
        self.body, self.hops, self.final = body, hops, final
        self.requests = []

    def __call__(self, request):
        self.requests.append(str(request.url))
        if len(self.requests) <= self.hops:
            target = (
                self.final
                if len(self.requests) == self.hops
                else f"https://hop{len(self.requests)}.example.org/x"
            )
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, content=self.body)


def download(server, dest, size=len(PACKAGE), digest=DIGEST):
    module._download(
        "https://github.com/example/releases/download/v1/x.deb",
        digest,
        size,
        dest,
        transport=httpx.MockTransport(server),
    )


def test_download_follows_https_redirects_and_checks_the_bytes(tmp_path):
    server = Server()
    download(server, tmp_path / "x.deb")
    assert (tmp_path / "x.deb").read_bytes() == PACKAGE
    assert server.requests[-1].startswith(
        "https://cdn.example.org/x.deb?X-Amz-Signature="
    )


def test_download_keeps_the_record_alive_as_bytes_arrive(tmp_path):
    ticks = []
    module._download(
        "https://github.com/example/releases/download/v1/x.deb",
        DIGEST,
        len(PACKAGE),
        tmp_path / "x.deb",
        transport=httpx.MockTransport(Server()),
        keepalive=lambda: ticks.append(1),
    )
    assert ticks


def test_a_record_touched_moves_now_and_then(tmp_path):
    now = [0.0]
    record = module._Record(
        tmp_path / RESULT_FILE,
        module._record_of(OperationRequest.model_validate(INSTALL), 0.0),
        lambda: now[0],
    )

    def written():
        return json.loads((tmp_path / RESULT_FILE).read_text())["updated_at"]

    now[0] = module.KEEPALIVE_S / 2
    record.touch()
    assert written() == 0.0

    now[0] = module.KEEPALIVE_S
    record.touch()
    assert written() == module.KEEPALIVE_S


@pytest.mark.parametrize(
    "server, size, digest, error",
    [
        (
            Server(final="http://cdn.example.org/x.deb"),
            len(PACKAGE),
            DIGEST,
            "left HTTPS",
        ),
        (
            Server(final="https://user:pw@cdn.example.org/x.deb"),
            len(PACKAGE),
            DIGEST,
            "left HTTPS",
        ),
        (Server(hops=7), len(PACKAGE), DIGEST, "too many times"),
        (Server(body=PACKAGE + b"x"), len(PACKAGE), DIGEST, "not the one"),
        (Server(body=PACKAGE[:-1]), len(PACKAGE), DIGEST, "not the one"),
        (Server(), len(PACKAGE), "0" * 64, "not the one"),
        (Server(), module.MAX_ARTIFACT_BYTES + 1, DIGEST, "larger"),
    ],
)
def test_download_refuses_what_the_catalog_did_not_describe(
    tmp_path, server, size, digest, error
):
    with pytest.raises(Failure) as info:
        download(server, tmp_path / "x.deb", size, digest)
    assert error in str(info.value)


def test_download_stops_reading_past_the_declared_size(tmp_path):
    server = Server(body=b"x" * 4096)
    with pytest.raises(Failure):
        download(server, tmp_path / "x.deb", 10, DIGEST)
    assert (tmp_path / "x.deb").stat().st_size <= 4096


def test_run_raises_after_printing_what_a_failing_command_said(capsys):
    script = (
        "import sys; "
        "sys.stdout.write('the following packages have unmet dependencies\\n'); "
        "sys.stderr.write('E: Unmet dependencies.\\n'); "
        "sys.exit(1)"
    )
    with pytest.raises(subprocess.CalledProcessError) as info:
        module._run([sys.executable, "-c", script])
    assert info.value.cmd[0] == sys.executable
    printed = capsys.readouterr().out
    assert "unmet dependencies" in printed
    assert "E: Unmet dependencies." in printed


def test_installation_support_needs_the_packaged_restart_service(monkeypatch, tmp_path):
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
    assert module.installation_supported() is False
    monkeypatch.setenv("KALINKA_PREFIX", "/")
    monkeypatch.setattr(module, "RESTART_DROPIN", str(tmp_path / "plugins.conf"))
    monkeypatch.setattr(module, "RESTART_PATH_UNIT", str(tmp_path / "restart.path"))
    assert module.installation_supported() is False
    (tmp_path / "plugins.conf").write_text("")
    (tmp_path / "restart.path").write_text("")
    assert module.installation_supported() is True


async def test_update_names_the_newer_release(box):
    box.installed["kalinka-plugin-demo"] = "1.0.0"
    ops = box.operations(
        await loaded_catalog(with_release(document(), "1.0.1", "b" * 64))
    )
    body = await ops.request("update", "demo")
    assert body == {
        "status": "restarting",
        "operation": "update",
        "plugin_id": "demo",
        "distribution": "kalinka-plugin-demo",
        "version": "1.0.1",
        "installed_version": "1.0.0",
    }
    request = box.request()
    assert request["action"] == "update"
    assert request["version"] == "1.0.1" and request["artifact_sha256"] == "b" * 64
    assert box.restarts == 1


async def test_update_needs_an_installed_package_and_something_newer(box):
    ops = box.operations(
        await loaded_catalog(with_release(document(), "1.0.1", "b" * 64))
    )
    assert await refused(ops.request("update", "demo")) == (409, "not_installed")
    box.installed["kalinka-plugin-demo"] = "1.0.1"
    assert await refused(ops.request("update", "demo")) == (409, "up_to_date")
    # Ahead of the catalog is never brought back down.
    box.installed["kalinka-plugin-demo"] = "2.0.0"
    assert await refused(ops.request("update", "demo")) == (409, "up_to_date")
    assert box.restarts == 0 and not (box.state / REQUEST_FILE).exists()


async def test_updates_list_newer_compatible_releases_of_installed_packages(box):
    doc = with_release(with_release(document(), "1.0.1", "b" * 64), "2.0.0", "c" * 64)
    doc["plugins"][0]["releases"][-1]["requires"]["server"] = ">=6"
    ops = box.operations(await loaded_catalog(doc))
    assert (await ops.updates())["updates"] == []
    box.installed["kalinka-plugin-demo"] = "1.0.0"
    result = await ops.updates()
    assert result["status"] == "available"
    assert result["catalog_revision"] == "test-public-revision"
    assert result["automatic_updates_enabled"] is False
    assert result["updates"] == [
        {
            "plugin_id": "demo",
            "distribution": "kalinka-plugin-demo",
            "version": "1.0.1",
            "installed_version": "1.0.0",
        }
    ]
    box.installed["kalinka-plugin-demo"] = "1.0.1"
    assert (await ops.updates())["updates"] == []


async def test_updates_are_unavailable_without_installer_or_catalog(box):
    ops = box.operations(await loaded_catalog(document()))
    box.supported = False
    assert await ops.updates() == {
        "status": "unavailable",
        "reason": "installer_unavailable",
        "updates": [],
        "automatic_updates_enabled": False,
    }
    assert ops.capabilities() == {
        "installation": False,
        "update_checks": False,
        "live_installation": False,
    }
    box.supported = True
    assert ops.capabilities() == {"installation": True, "update_checks": True, "live_installation": False}
    unavailable = PublicCatalog(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    )
    assert (await box.operations(unavailable).updates())[
        "reason"
    ] == "catalog_unavailable"


def test_helper_updates_the_package_in_place(root):
    with_release(root.catalog, "1.0.1", "b" * 64)
    root.installed["kalinka-plugin-demo"] = "1.0.0"
    code, result = root.apply(
        INSTALL | {"action": "update", "version": "1.0.1", "artifact_sha256": "b" * 64}
    )
    assert code == 0 and result["status"] == "ok", result
    assert result["action"] == "update" and result["version"] == "1.0.1"
    assert root.installed["kalinka-plugin-demo"] == "1.0.1"
    assert root.calls[1] == ["systemctl", "stop", "kalinka.service"]
    assert root.calls[2][-1].endswith("/kalinka-plugin-demo_1.0.1_all.deb")


async def test_routes_report_and_apply_updates(box):
    from fastapi import FastAPI

    from kalinka_server.plugin_management.inventory import Discovery, PluginInventory
    from kalinka_server.plugin_management.route import register_plugin_routes

    catalog = await loaded_catalog(with_release(document(), "1.0.1", "b" * 64))
    box.installed["kalinka-plugin-demo"] = "1.0.0"
    app = FastAPI()
    register_plugin_routes(
        app,
        PluginInventory(lambda: Discovery((), True)),
        catalog,
        enabled=lambda: True,
        operations=box.operations(catalog),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        updates = (await client.get("/server/plugins/updates")).json()
        assert [u["version"] for u in updates["updates"]] == ["1.0.1"]
        capabilities = (await client.get("/server/plugins")).json()["capabilities"]
        assert capabilities["installation"] and capabilities["update_checks"]
        response = await client.post("/server/plugins/demo/install")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "already_installed"
        response = await client.post("/server/plugins/demo/update")
        assert response.status_code == 202, response.text
        assert response.json()["operation"] == "update"
        assert box.request()["action"] == "update"


# ---------------------------------------------------------------------------
# Beside a running Core


from contextlib import contextmanager

from kalinka_server.plugin_management.operations import ACK_FILE, LiveTools

UPDATE = INSTALL | {"action": "update"}


class LiveRoot(Root):
    """The helper's view of a box whose Core keeps running: what pip would
    change, what the venv holds, whether Core answers the unload."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.report = {"install": [{"metadata": {"name": "kalinka-plugin-demo", "version": "1.0.0"}}]}
        self.venv = {"kalinka-plugin-sdk": "3.21.0", "httpx": "0.27.0"}
        self.wheel_extensions = False
        self.installed_extensions = False
        self.acks = True
        self.steps = []

    def run(self, command):
        self.steps.append(json.loads((self.state / RESULT_FILE).read_text())["step"])
        return super().run(command)

    @contextmanager
    def marker(self):
        self.calls.append(["marker", "on"])
        yield
        self.calls.append(["marker", "off"])

    def tools(self) -> LiveTools:
        return LiveTools(
            extract_wheel=lambda package, scratch: scratch / "demo.whl",
            pip_report=lambda wheel: self.report,
            installed_distributions=lambda: dict(self.venv),
            has_extension_modules=lambda wheel: self.wheel_extensions,
            installed_has_extension_modules=lambda name: self.installed_extensions,
            pip_install=lambda wheel: self.calls.append(["pip", "install", wheel.name]),
            wait_for_unload=self._wait_for_unload,
            marker=self.marker,
            before_stop=lambda: self.calls.append(["before-stop"]),
        )

    def _wait_for_unload(self, state_dir, operation_id):
        self.calls.append(["wait-for-unload", str(state_dir), operation_id])
        return self.acks

    def apply_live(self, request):
        (self.state / REQUEST_FILE).write_text(json.dumps(request))
        code = apply(
            self.state,
            fetch_catalog=self.fetch_catalog,
            download=self.download,
            run=self.run,
            package_version=self.installed.get,
            live=True,
            tools=self.tools(),
            clock=self.clock,
        )
        result = json.loads((self.state / RESULT_FILE).read_text())
        return code, result


@pytest.fixture
def live(tmp_path):
    return LiveRoot(tmp_path)


def _names(calls):
    return [call[0] for call in calls]


def test_a_live_install_leaves_core_running(live):
    code, result = live.apply_live(INSTALL)

    assert code == 0 and result["status"] == "ok", result
    assert "systemctl" not in _names(live.calls)
    apt = next(i for i, c in enumerate(live.calls) if c[0] == "apt-get" and "-s" not in c)
    assert live.calls[apt - 1] == ["marker", "on"]
    assert live.calls[apt + 1] == ["marker", "off"]
    assert ["pip", "install", "demo.whl"] in live.calls
    assert live.steps == ["checking", "installing_package"]
    assert (result["id"], result["step"], result["step_index"], result["step_count"]) == (
        NOW.isoformat(), "installing_environment", 4, 5
    )


def test_a_live_update_waits_for_core_to_unload_the_plugin(live):
    live.installed["kalinka-plugin-demo"] = "0.9.0"

    code, result = live.apply_live(UPDATE)

    assert result["status"] == "ok"
    assert ["wait-for-unload", str(live.state), NOW.isoformat()] in live.calls
    assert live.steps == ["checking", "installing_package"]
    assert result["step_count"] == 6 and result["step_index"] == 5


def test_a_core_that_does_not_unload_gets_the_restart_instead(live):
    live.installed["kalinka-plugin-demo"] = "0.9.0"
    live.acks = False
    seen = []

    def run(command):
        seen.append(json.loads((live.state / RESULT_FILE).read_text())["status"])
        return live.run(command)

    (live.state / REQUEST_FILE).write_text(json.dumps(UPDATE))
    apply(
        live.state,
        fetch_catalog=live.fetch_catalog,
        download=live.download,
        run=run,
        package_version=live.installed.get,
        live=True,
        tools=live.tools(),
        clock=live.clock,
    )

    names = _names(live.calls)
    assert names[names.index("before-stop"):] == ["before-stop", "systemctl", "apt-get", "systemctl"]
    assert [c for c in live.calls if c[0] == "systemctl"] == [
        ["systemctl", "stop", "kalinka.service"],
        ["systemctl", "start", "kalinka.service"],
    ]
    assert "marker" not in names and "pip" not in names
    assert seen == ["running", "restarting", "restarting", "restarting"]
    assert json.loads((live.state / RESULT_FILE).read_text())["status"] == "ok"


def test_core_is_started_again_even_when_the_fallback_install_fails(live):
    live.installed["kalinka-plugin-demo"] = "0.9.0"
    live.acks = False
    live.fail = "apt"

    code, result = live.apply_live(UPDATE)

    assert result["status"] == "failed"
    assert ["systemctl", "start", "kalinka.service"] in live.calls


@pytest.mark.parametrize(
    "change, restarts",
    [
        pytest.param({}, False, id="only the plugin"),
        pytest.param(
            {"plans": {"install": "Inst libfoo (1 stable [amd64])\nInst kalinka-plugin-demo (1.0.0 local-deb [all])\n"}},
            True,
            id="apt brings another package",
        ),
        pytest.param(
            {"report": {"install": [{"metadata": {"name": "kalinka-plugin-demo", "version": "1.0.0"}}, {"metadata": {"name": "HTTPX", "version": "0.28.0"}}]}},
            True,
            id="pip changes an installed distribution",
        ),
        pytest.param(
            {"report": {"install": [{"metadata": {"name": "kalinka-plugin-demo", "version": "1.0.0"}}, {"metadata": {"name": "brand-new", "version": "1.0"}}]}},
            False,
            id="pip adds a distribution nothing imported",
        ),
        pytest.param({"wheel_extensions": True}, True, id="the new wheel has compiled modules"),
        pytest.param({"installed_extensions": True}, True, id="the old version has compiled modules"),
    ],
)
def test_what_a_running_process_cannot_take_in_restarts(live, change, restarts):
    live.installed["kalinka-plugin-demo"] = "0.9.0"
    for attribute, value in change.items():
        setattr(live, attribute, value)

    live.apply_live(UPDATE)

    assert (["systemctl", "stop", "kalinka.service"] in live.calls) is restarts
    assert (["pip", "install", "demo.whl"] in live.calls) is not restarts


def test_a_fresh_install_may_carry_compiled_modules(live):
    live.wheel_extensions = True

    live.apply_live(INSTALL)

    assert "systemctl" not in _names(live.calls)
    assert ["pip", "install", "demo.whl"] in live.calls


def test_a_live_removal_leaves_core_running(live):
    live.installed["kalinka-plugin-demo"] = "1.0.0"

    code, result = live.apply_live(UNINSTALL)

    assert result["status"] == "ok"
    assert "systemctl" not in _names(live.calls)
    assert live.steps == ["checking", "removing_package"]
    assert live.calls[-3:] == [
        ["marker", "on"],
        ["apt-get", "-o", "DPkg::Lock::Timeout=300", "remove", "-y", "kalinka-plugin-demo"],
        ["marker", "off"],
    ]
    assert (result["step_index"], result["step_count"]) == (3, 3)


# ---------------------------------------------------------------------------
# Core following the helper


class Lifecycle:
    def __init__(self):
        self.calls = []
        self.fail_load = False

    async def unload(self, name, *, gone):
        self.calls.append(("unload", name, gone))

    async def suspend(self, name):
        self.calls.append(("suspend", name))

    async def abandon(self, name, message):
        self.calls.append(("abandon", name, message))

    async def load(self, name, *, purge):
        self.calls.append(("load", name, purge))
        if self.fail_load:
            raise RuntimeError("setup failed")


class LiveBox(Box):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.lifecycle = Lifecycle()
        self.live = True
        self.triggers = 0
        self.forgotten = 0

    def operations(self, catalog, host=HOST, renderers=(), deadline_s=5.0):
        return PluginOperations(
            catalog,
            CatalogCompatibility(lambda: host, lambda: NOW),
            lambda: tuple(renderers),
            state_dir=lambda: str(self.state),
            supported=lambda: self.supported,
            live_supported=lambda: self.live,
            lifecycle=lambda: self.lifecycle,
            package_version=self.installed.get,
            newer=lambda candidate, installed: Version(candidate) > Version(installed),
            restart=self.restart,
            restarting=lambda: self.restarting,
            live_trigger=self._trigger,
            live_pending=lambda: False,
            live_forget=self._forget,
            clock=lambda: self.now,
            poll_s=0.01,
            deadline_s=deadline_s,
        )

    def _trigger(self):
        self.triggers += 1

    def _forget(self):
        self.forgotten += 1

    def helper_says(self, status, step=None, **more):
        request = self.request()
        steps = module.STEPS[request["action"]]
        record = module._record_of(OperationRequest.model_validate(request), 0.0)
        record.update(status=status, step=step, step_index=steps.index(step) + 1 if step else 0, **more)
        module._write_json(self.state / RESULT_FILE, record, 0o644)

    def ack(self):
        try:
            return json.loads((self.state / ACK_FILE).read_text())["id"]
        except OSError:
            return None


@pytest.fixture
def live_box(tmp_path):
    return LiveBox(tmp_path)


async def _until(condition, timeout_s=3.0):
    for _ in range(int(timeout_s / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


async def test_a_live_install_is_applied_and_loaded(live_box):
    ops = live_box.operations(await loaded_catalog(document()))

    answer = await ops.request("install", "demo")

    assert answer["status"] == "applying" and live_box.triggers == 1 and live_box.restarts == 0
    assert ops.operation()["status"] == "running" and ops.operation()["step"] is None
    live_box.helper_says("ok", "installing_environment")
    await _until(lambda: ops.operation()["status"] == "ok")
    assert live_box.lifecycle.calls == [("load", "demo", True)]
    record = ops.operation()
    assert (record["step"], record["step_index"], record["step_count"]) == ("loading", 5, 5)
    assert ops.last_result() == record


async def test_a_live_update_unloads_when_the_helper_asks(live_box):
    live_box.installed["kalinka-plugin-demo"] = "0.9.0"
    doc = with_release(document(), "1.1.0", DIGEST)
    ops = live_box.operations(await loaded_catalog(doc))

    await ops.request("update", "demo")
    assert live_box.lifecycle.calls == []

    live_box.helper_says("running", "preparing")
    await _until(lambda: live_box.ack() is not None)
    assert live_box.ack() == NOW.isoformat()
    assert live_box.lifecycle.calls == [("suspend", "demo")]

    live_box.helper_says("ok", "installing_environment")
    await _until(lambda: ops.operation()["status"] == "ok")
    assert live_box.lifecycle.calls[-1] == ("load", "demo", True)


async def test_a_live_removal_unloads_before_asking(live_box):
    live_box.installed["kalinka-plugin-demo"] = "1.0.0"
    ops = live_box.operations(await loaded_catalog(document()))

    answer = await ops.request("uninstall", "demo")

    assert answer["status"] == "applying"
    assert live_box.lifecycle.calls == [("unload", "demo", True)]
    assert (ops.operation()["step"], ops.operation()["step_index"]) == ("preparing", 1)
    live_box.helper_says("ok", "removing_package")
    await _until(lambda: ops.operation()["status"] == "ok")
    assert live_box.lifecycle.calls == [("unload", "demo", True)]


async def test_a_refused_removal_loads_the_plugin_back(live_box):
    live_box.installed["kalinka-plugin-demo"] = "1.0.0"
    live_box.restarting = True
    ops = live_box.operations(await loaded_catalog(document()))

    assert await refused(ops.request("uninstall", "demo")) == (409, "operation_in_progress")

    assert live_box.lifecycle.calls == [("unload", "demo", True), ("load", "demo", False)]
    assert ops.operation() == {"status": "idle"}


async def test_a_helper_that_restarts_ends_the_follow(live_box):
    ops = live_box.operations(await loaded_catalog(document()))
    await ops.request("install", "demo")

    live_box.helper_says("restarting", "checking")
    await _until(lambda: ops._following.done())

    assert live_box.lifecycle.calls == []
    assert ops.operation()["status"] == "restarting"


async def test_a_failed_update_loads_the_old_files_back(live_box):
    live_box.installed["kalinka-plugin-demo"] = "0.9.0"
    ops = live_box.operations(await loaded_catalog(with_release(document(), "1.1.0", DIGEST)))
    await ops.request("update", "demo")
    live_box.helper_says("running", "preparing")
    await _until(lambda: live_box.ack() is not None)

    live_box.helper_says("failed", "installing_package", error="apt-get did not complete.")
    await _until(lambda: ops._following.done())

    assert live_box.lifecycle.calls == [("suspend", "demo"), ("load", "demo", False)]
    assert ops.operation()["error"] == "apt-get did not complete."


async def test_a_failed_removal_loads_the_plugin_back(live_box):
    live_box.installed["kalinka-plugin-demo"] = "1.0.0"
    ops = live_box.operations(await loaded_catalog(document()))
    await ops.request("uninstall", "demo")

    live_box.helper_says("failed", "removing_package", error="The package is still installed.")
    await _until(lambda: ops._following.done())

    assert live_box.lifecycle.calls == [("unload", "demo", True), ("load", "demo", False)]
    assert ops.operation()["error"] == "The package is still installed."


async def test_a_request_refused_while_another_is_submitted_touches_neither(live_box):
    live_box.installed["kalinka-plugin-demo"] = "0.9.0"
    ops = live_box.operations(await loaded_catalog(with_release(document(), "1.1.0", DIGEST)))
    outcomes = await asyncio.gather(
        ops.request("update", "demo"), ops.request("update", "demo"), return_exceptions=True
    )

    applied = [o for o in outcomes if isinstance(o, dict)]
    assert [o["status"] for o in applied] == ["applying"]
    assert [o.code for o in outcomes if isinstance(o, Refusal)] == ["operation_in_progress"]

    live_box.helper_says("ok", "installing_environment")
    await _until(lambda: ops.operation()["status"] == "ok")
    assert live_box.lifecycle.calls == [("load", "demo", True)]


async def test_a_helper_whose_record_keeps_moving_is_waited_for(live_box):
    ops = live_box.operations(await loaded_catalog(document()), deadline_s=0.05)
    await ops.request("install", "demo")

    for tick in range(1, 6):
        live_box.helper_says("running", "downloading", updated_at=float(tick))
        await asyncio.sleep(0.03)
    live_box.helper_says("ok", "installing_environment", updated_at=9.0)
    await _until(lambda: ops.operation()["status"] == "ok")

    assert live_box.lifecycle.calls == [("load", "demo", True)]


async def test_a_plugin_that_will_not_set_up_fails_the_operation(live_box):
    live_box.lifecycle.fail_load = True
    ops = live_box.operations(await loaded_catalog(document()))
    await ops.request("install", "demo")

    live_box.helper_says("ok", "installing_environment")
    await _until(lambda: ops.operation()["status"] == "failed")

    assert ops.operation()["error"] == "demo did not set up (RuntimeError)."


async def test_a_helper_that_never_answers_fails_the_operation(live_box):
    ops = live_box.operations(await loaded_catalog(document()), deadline_s=0.05)
    await ops.request("install", "demo")

    await _until(lambda: ops.operation()["status"] == "failed")

    assert ops.operation()["error"] == "The installer did not answer."
    # The trigger it never consumed goes, or every later request would be refused.
    assert live_box.forgotten == 1


async def test_a_request_whose_helper_never_ran_is_not_left_for_a_restart(
    live_box, tmp_path
):
    """The restart's helper runs while the request is still fresh, so only
    its being gone keeps the install from happening then."""
    ops = live_box.operations(await loaded_catalog(document()), deadline_s=0.05)
    await ops.request("install", "demo")
    await _until(lambda: ops._following.done())

    root = Root(tmp_path)
    assert (
        apply(
            root.state,
            fetch_catalog=root.fetch_catalog,
            download=root.download,
            run=root.run,
            package_version=root.installed.get,
            clock=root.clock,
        )
        == 0
    )
    assert root.calls == [] and root.downloads == []
    assert ops.operation()["error"] == "The installer did not answer."


async def test_a_helper_that_stands_still_is_given_up_on(live_box):
    ops = live_box.operations(await loaded_catalog(document()), deadline_s=0.05)
    await ops.request("install", "demo")
    live_box.helper_says("running", "downloading", updated_at=1.0)

    await _until(lambda: ops.operation()["status"] == "failed")

    assert ops.operation()["error"] == "The installer did not finish."
    assert ops.operation()["step"] == "downloading"


async def test_a_stalled_update_leaves_the_plugin_listed_in_error(live_box):
    live_box.installed["kalinka-plugin-demo"] = "0.9.0"
    ops = live_box.operations(
        await loaded_catalog(with_release(document(), "1.1.0", DIGEST)), deadline_s=0.05
    )
    await ops.request("update", "demo")
    live_box.helper_says("running", "preparing", updated_at=1.0)

    await _until(lambda: ops._following.done())

    assert ops.operation()["error"] == "The installer did not finish."
    assert live_box.lifecycle.calls == [
        ("suspend", "demo"),
        ("abandon", "demo", module.ABANDONED),
    ]


async def test_one_live_operation_at_a_time(live_box):
    ops = live_box.operations(await loaded_catalog(document()))
    await ops.request("install", "demo")
    (live_box.state / REQUEST_FILE).unlink()

    assert await refused(ops.request("install", "demo")) == (409, "operation_in_progress")


async def test_without_the_live_units_a_request_restarts_as_before(live_box):
    live_box.live = False
    ops = live_box.operations(await loaded_catalog(document()))

    answer = await ops.request("install", "demo")

    assert answer["status"] == "restarting" and live_box.restarts == 1
    assert ops.capabilities()["live_installation"] is False


def test_nothing_ran_yet_is_idle(live_box):
    ops = live_box.operations(PublicCatalog(""))
    assert ops.operation() == {"status": "idle"}
    assert ops.last_result() is None
