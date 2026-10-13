"""What every server install pulls in.

onnxruntime and tokenizers run only the shared text embedder, and the
plugins that embed offer them as optional packages, so a box that never
searches by mood carries neither. The server and its plugins must start
without them.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

SERVER_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
RUNTIME = ("onnxruntime", "tokenizers")

WITHOUT_THE_RUNTIME = f"""
import sys
from importlib.metadata import entry_points

for name in {RUNTIME!r}:
    sys.modules[name] = None

import kalinka_server.server

for plugin in entry_points(group="kalinka.plugins"):
    plugin.load()
"""


def test_the_embedder_runtime_is_not_a_server_dependency():
    project = tomllib.loads(SERVER_PYPROJECT.read_text())["project"]
    required = {
        canonicalize_name(Requirement(requirement).name)
        for requirement in project["dependencies"]
    }

    assert required & set(RUNTIME) == set()


def test_the_server_and_its_plugins_load_without_the_embedder_runtime():
    """In a process where neither package can be imported, as in a venv
    built from the server's dependencies alone."""
    result = subprocess.run(
        [sys.executable, "-c", WITHOUT_THE_RUNTIME],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
