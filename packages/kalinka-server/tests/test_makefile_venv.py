"""The Makefile's Python targets run the dev venv's interpreter, not PATH's."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, NamedTuple, Tuple

import pytest

REPO = Path(__file__).resolve().parents[3]
SERVER = REPO / "packages" / "kalinka-server"
MAKE = shutil.which("make")

pytestmark = pytest.mark.skipif(MAKE is None, reason="needs make")

STUB = """#!/bin/sh
printf '%s\\t%s\\t%s\\n' {name} "$PWD" "$*" >> "$KALINKA_TEST_LOG"
{body}
"""
VENV_PYTHON = """case "$1" in
    -c) echo 3.13 ;;
    --version) echo Python 3.13.0 ;;
esac"""


class Call(NamedTuple):
    program: str
    cwd: Path
    args: str


class FakeVenv(NamedTuple):
    path: Path
    log: Path
    shell_path: str
    home: Path

    def run(self, *targets: str) -> Tuple[subprocess.CompletedProcess, List[Call]]:
        result = subprocess.run(
            [
                MAKE,
                *targets,
                f"VENV={os.path.relpath(self.path, REPO)}",
                f"KALINKA_PREFIX={self.home / 'kalinka'}",
            ],
            cwd=REPO,
            env={
                "PATH": self.shell_path,
                "HOME": str(self.home),
                "KALINKA_TEST_LOG": str(self.log),
            },
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        calls = [
            Call(program, Path(cwd).resolve(), args)
            for program, cwd, args in (
                line.split("\t", 2) for line in self.log.read_text().splitlines()
            )
        ]
        assert [call for call in calls if call.program == "bare-python"] == []
        return result, calls

    def make(self, *targets: str) -> List[Call]:
        result, calls = self.run(*targets)
        assert result.returncode == 0, result.stdout + result.stderr
        return calls


def _stub(path: Path, name: str, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STUB.format(name=name, body=body))
    path.chmod(0o755)


@pytest.fixture
def fake_venv(tmp_path):
    venv = tmp_path / "venv"
    _stub(venv / "bin" / "python", "venv-python", VENV_PYTHON)
    _stub(venv / "bin" / "pip", "venv-pip")
    # First on PATH, so a recipe's bare `python` is caught whatever the host has.
    for bare in ("python", "python3"):
        _stub(tmp_path / "path" / bare, "bare-python", "exit 127")
    log = tmp_path / "calls.log"
    log.touch()
    return FakeVenv(
        path=venv,
        log=log,
        shell_path=f"{tmp_path / 'path'}:/usr/bin:/bin",
        home=tmp_path,
    )


def test_make_test_runs_each_suite_from_its_package_in_the_venv(fake_venv):
    calls = fake_venv.make("test")

    suites = {call.cwd for call in calls if call.args.startswith("-m pytest tests/ ")}
    packages = {tests.parent for tests in REPO.glob("packages/kalinka-plugin-*/tests")}
    assert suites == packages | {SERVER}
    assert Call("venv-python", REPO, "-m pytest benchmarks/sdd/tests -v") in calls


def test_make_test_playqueue_and_lint_run_in_the_venv(fake_venv):
    calls = fake_venv.make("test-playqueue", "lint")

    assert Call("venv-python", SERVER, "-m pytest tests/test_playqueue.py -v") in calls
    assert any(call.args.startswith("-m flake8 ") for call in calls)
    assert Call("venv-python", REPO, "scripts/check_credential_logging.py") in calls


@pytest.mark.parametrize("target", ["test", "test-playqueue", "lint"])
def test_a_missing_venv_says_how_to_get_one(fake_venv, target):
    missing = fake_venv._replace(path=fake_venv.home / "no-venv")

    result, calls = missing.run(target)

    assert result.returncode != 0
    assert "make dev-setup" in result.stdout
    assert "VENV=" in result.stdout
    assert calls == []


def test_dev_setup_installs_the_test_tools_after_the_import_check(fake_venv):
    calls = fake_venv.make("dev-setup")

    commands = [(call.program, call.args) for call in calls]
    import_check = commands.index(("venv-python", "-c import kalinka_server.__main__"))
    dev_tools = [
        index
        for index, (program, args) in enumerate(commands)
        if program == "venv-pip" and "-r requirements-dev.txt" in args
    ]
    assert dev_tools and dev_tools[0] > import_check
