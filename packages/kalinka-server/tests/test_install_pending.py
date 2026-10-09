"""Tests for the bootstrap-side install_pending helper.

The helper has a subtle non-local invariant: the canonical
``pending_installs.json`` must be moved aside *before* pip runs so a
long install can be interrupted (systemd timeout, power loss, apt
upgrade) without re-attempting the same install on the next boot. These
tests pin that contract down.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from kalinka_server import install_pending

PIP_INSTALL = install_pending._pip_install


def _seed_manifest(manifests_dir: Path) -> None:
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / "lf.json").write_text(
        json.dumps(
            {
                "plugin": "lf",
                "schema": 1,
                "packages": {
                    "numpy": {
                        "pip_spec": "numpy==1.26.4",
                        "description": "x",
                    },
                    "tokenizers": {
                        "pip_spec": "tokenizers==0.22.2",
                        "description": "x",
                    },
                },
            }
        )
    )


def _write_pending(pending_path: Path, keys: list[str]) -> None:
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(
        json.dumps(
            {"schema": 1, "requested": keys, "requested_at": time.time()}
        )
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Prepare manifest + pending file; stub pip to record invocations.

    Yields a dict with paths and a list that records pip invocations.
    """
    manifests = tmp_path / "manifests"
    _seed_manifest(manifests)
    pending = tmp_path / "pending.json"
    last_install = tmp_path / "last.json"

    invocations: list[tuple[str, bool]] = []

    def fake_pip(spec: str):
        # Capture whether the *canonical* pending file is still on disk
        # at the moment pip runs. The contract is that it should already
        # have been reserved (moved aside) before this point.
        invocations.append((spec, pending.exists()))
        r = types.SimpleNamespace()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    monkeypatch.setattr(install_pending, "_pip_install", fake_pip)

    yield {
        "tmp": tmp_path,
        "manifests": manifests,
        "pending": pending,
        "last_install": last_install,
        "invocations": invocations,
    }


def _run(env: dict) -> int:
    return install_pending.main(
        [
            "install_pending",
            str(env["pending"]),
            str(env["manifests"]),
            str(env["last_install"]),
        ]
    )


def test_pending_file_is_reserved_before_pip_runs(env):
    """Contract: by the time pip is invoked, the canonical pending file
    is already gone — so a kill mid-pip won't trigger a retry on the
    next boot."""
    _write_pending(env["pending"], ["numpy"])
    rc = _run(env)
    assert rc == 0
    assert len(env["invocations"]) == 1
    pip_spec, pending_visible_during_pip = env["invocations"][0]
    assert pip_spec == "numpy==1.26.4"
    assert pending_visible_during_pip is False, (
        "pending_installs.json should be moved aside before pip runs"
    )
    assert not env["pending"].exists(), "pending file should be gone at end"


def test_success_renames_to_done(env):
    _write_pending(env["pending"], ["numpy"])
    _run(env)
    archives = sorted(env["tmp"].glob("pending.done.*"))
    assert len(archives) == 1, f"expected exactly one .done archive, got {archives}"
    assert env["last_install"].exists()


def test_failure_renames_to_failed(env, monkeypatch):
    def failing_pip(spec):
        r = types.SimpleNamespace()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "ERROR: simulated failure"
        return r

    monkeypatch.setattr(install_pending, "_pip_install", failing_pip)
    _write_pending(env["pending"], ["numpy"])
    rc = _run(env)
    # Helper exits 0 even on pip failure (audit recorded, no retry).
    assert rc == 0
    assert sorted(env["tmp"].glob("pending.failed.*"))
    audit = json.loads(env["last_install"].read_text())
    assert audit["results"][0]["status"] == "failed"


def test_pip_installs_wheels_only(monkeypatch):
    """A source build held kalinka.service's start for as long as a compile
    takes on a Pi, and needs a toolchain on the box."""
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(install_pending.subprocess, "run", fake_run)

    PIP_INSTALL("numpy==2.4.2")

    [cmd] = commands
    assert cmd[:4] == [sys.executable, "-m", "pip", "install"]
    assert "--only-binary=:all:" in cmd
    assert cmd[-1] == "numpy==2.4.2"


ABSENT_SPEC = "kalinka-absent-package==1.0"
ABSENT_PAGE = "/simple/kalinka-absent-package/"
# A source release only, which a wheels-only pip passes over.
SDIST_ONLY = (
    b'<a href="kalinka_absent_package-1.0.tar.gz">'
    b"kalinka_absent_package-1.0.tar.gz</a>"
)


@contextlib.contextmanager
def index_answering(status: int, monkeypatch, variable: str = "PIP_INDEX_URL"):
    """Point pip, through ``variable``, at an index on this machine that
    answers every page with ``status``; yields the paths it was asked for."""
    asked: list[str] = []

    class Index(BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append(self.path)
            body = SDIST_ONLY if status == 200 else b""
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Index) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monkeypatch.setenv(variable, f"http://127.0.0.1:{server.server_port}/simple")
        try:
            yield asked
        finally:
            server.shutdown()


@pytest.fixture
def real_pip(env, monkeypatch):
    """This interpreter's own pip, with no config of this machine's, asked for
    a package no index carries, and told not to retry."""
    (env["manifests"] / "absent.json").write_text(
        json.dumps(
            {
                "plugin": "absent",
                "schema": 1,
                "packages": {
                    "absent": {"pip_spec": ABSENT_SPEC, "description": "x"}
                },
            }
        )
    )
    monkeypatch.setattr(install_pending, "_pip_install", PIP_INSTALL)
    for variable in ("PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_NO_INDEX"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    monkeypatch.setenv("PIP_RETRIES", "0")
    _write_pending(env["pending"], ["absent"])
    return env


def _only_result(env: dict) -> dict:
    [result] = json.loads(env["last_install"].read_text())["results"]
    return result


def _assert_failed_without_a_reason(env: dict) -> None:
    result = _only_result(env)
    assert result["status"] == "failed"
    assert "No matching distribution found" in result["stderr"]
    assert "reason" not in result


def test_a_package_with_no_wheel_is_named_in_the_reason(real_pip, monkeypatch):
    with index_answering(200, monkeypatch) as asked:
        assert _run(real_pip) == 0

    assert ABSENT_PAGE in asked
    result = _only_result(real_pip)
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert result["status"] == "failed"
    assert result["reason"] == (
        f"No prebuilt {ABSENT_SPEC} for Python {python} on {platform.machine()}"
    )


@pytest.mark.parametrize(
    "kernel_machine, python_is_64bit, wheel_machine",
    [
        ("aarch64", True, "aarch64"),
        ("x86_64", True, "x86_64"),
        ("aarch64", False, "armv7l"),
        ("x86_64", False, "i686"),
        ("armv7l", False, "armv7l"),
    ],
)
def test_the_reason_names_the_architecture_pip_wants_wheels_for(
    kernel_machine, python_is_64bit, wheel_machine
):
    """32-bit Raspberry Pi OS runs a 64-bit kernel on a Pi 4 or 5."""
    assert (
        install_pending._wheel_machine(kernel_machine, python_is_64bit)
        == wheel_machine
    )


@pytest.mark.parametrize("status", [403, 404, 429, 502, 504])
def test_an_index_that_does_not_list_the_package_is_not_blamed_on_the_machine(
    real_pip, monkeypatch, status
):
    """pip ends in the same not-found error whatever the index answered."""
    with index_answering(status, monkeypatch) as asked:
        assert _run(real_pip) == 0

    assert ABSENT_PAGE in asked
    _assert_failed_without_a_reason(real_pip)


@pytest.mark.parametrize(
    "extra_status, blamed", [(200, True), (404, False), (502, False)]
)
def test_a_404_is_an_answer_once_another_index_lists_the_package(
    real_pip, monkeypatch, extra_status, blamed
):
    """bootstrap.sh asks piwheels first and PyPI after it."""
    with index_answering(404, monkeypatch), index_answering(
        extra_status, monkeypatch, "PIP_EXTRA_INDEX_URL"
    ) as extra_asked:
        assert _run(real_pip) == 0

    assert ABSENT_PAGE in extra_asked
    if blamed:
        assert _only_result(real_pip)["reason"].startswith(
            f"No prebuilt {ABSENT_SPEC} for Python "
        )
    else:
        _assert_failed_without_a_reason(real_pip)


def test_an_unreachable_index_is_not_blamed_on_the_machine(
    real_pip, monkeypatch
):
    with socket.socket() as not_listening:
        not_listening.bind(("127.0.0.1", 0))
        port = not_listening.getsockname()[1]
        monkeypatch.setenv("PIP_INDEX_URL", f"http://127.0.0.1:{port}/simple")

        assert _run(real_pip) == 0

    _assert_failed_without_a_reason(real_pip)


def test_killed_mid_install_does_not_retry_on_next_boot(env, monkeypatch):
    """Simulate the case the user just hit: install_pending starts, pip
    runs, the process is killed (or the host loses power) before audit
    is written. The next boot must find no pending file and must NOT
    re-attempt the install."""

    # First run: pip "starts" but we simulate a kill by raising before
    # the audit is written. The pending file should already be moved
    # aside at that point.
    class _Killed(SystemExit):
        pass

    def killing_pip(spec):
        # Pending must be gone before we "die".
        assert not env["pending"].exists()
        raise _Killed("simulated kill")

    monkeypatch.setattr(install_pending, "_pip_install", killing_pip)
    _write_pending(env["pending"], ["numpy"])
    with pytest.raises(_Killed):
        _run(env)

    # Pending file is gone; an .in_progress.<ts> archive is left.
    assert not env["pending"].exists()
    assert sorted(env["tmp"].glob("pending.in_progress.*"))

    # Second run: bootstrap re-invokes install_pending. With no canonical
    # pending file, it must be a no-op — no pip call recorded.
    invocations_before = len(env["invocations"])
    monkeypatch.setattr(
        install_pending,
        "_pip_install",
        lambda spec: env["invocations"].append((spec, True)) or types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    rc = _run(env)
    assert rc == 0
    assert len(env["invocations"]) == invocations_before, (
        "no pip invocation should happen on the second boot — the previous "
        "attempt already claimed the pending file"
    )


def test_no_pending_file_is_a_noop(env):
    """Helper must not error when there's nothing to install."""
    rc = _run(env)
    assert rc == 0
    assert env["invocations"] == []
    assert not env["last_install"].exists()


def test_malformed_pending_is_moved_aside(env):
    """A pending file without a `requested` list is moved aside so the
    same parse error doesn't loop."""
    env["pending"].write_text(json.dumps({"schema": 1, "not_requested": 42}))
    rc = install_pending.main(
        [
            "install_pending",
            str(env["pending"]),
            str(env["manifests"]),
            str(env["last_install"]),
        ]
    )
    assert rc == 1
    assert not env["pending"].exists()
    assert sorted(env["tmp"].glob("pending.malformed.*"))
