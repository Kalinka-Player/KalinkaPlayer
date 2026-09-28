"""Every file that states the oldest Python Kalinka runs on states the same one.

They once said 3.8, 3.10 and 3.11 at once, and the Debian package accepted
an interpreter the server could not import on.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

FLOOR_STATEMENTS = [
    ("packages/*/pyproject.toml", r'requires-python = ">=(\d+\.\d+)"'),
    ("packages/kalinka-plugin-sdk/debian/control.in", r"python3 \(>= (\d+\.\d+)\)"),
    ("packages/kalinka-server/DEBIAN/control.in", r"python3 \(>= (\d+\.\d+)\)"),
    ("packages/kalinka-server/rpm/kalinka-server.spec", r"python3 >= (\d+\.\d+)"),
    ("Makefile", r">= (\d+\.\d+)"),
    ("Makefile", r"version_info\[:2\] >= \((\d+), (\d+)\)"),
    ("docs/development.md", r"Python (\d+\.\d+)\+"),
    (
        "template/cookiecutter-kalinka-plugin/cookiecutter.json",
        r'"python_version": "(\d+\.\d+)"',
    ),
]


def _floors_stated():
    for pattern_path, pattern in FLOOR_STATEMENTS:
        paths = sorted(REPO.glob(pattern_path))
        assert paths, f"{pattern_path} matches no file"
        for path in paths:
            found = re.findall(pattern, path.read_text())
            assert found, f"{path.relative_to(REPO)} no longer states the floor"
            for version in found:
                if isinstance(version, tuple):
                    version = ".".join(version)
                yield str(path.relative_to(REPO)), version


def test_every_statement_of_the_python_floor_agrees():
    files_by_floor = {}
    for path, version in _floors_stated():
        files_by_floor.setdefault(version, set()).add(path)
    assert len(files_by_floor) == 1, "; ".join(
        f"{version} in {', '.join(sorted(paths))}"
        for version, paths in sorted(files_by_floor.items())
    )
