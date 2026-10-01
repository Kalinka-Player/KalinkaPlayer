"""Every file that states the oldest Python Kalinka runs on states the same one.

They once said 3.8, 3.10 and 3.11 at once, and the Debian package accepted
an interpreter the server could not import on.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PLUGIN_TEMPLATE_PYPROJECT = (
    REPO
    / "template"
    / "cookiecutter-kalinka-plugin"
    / "{{cookiecutter.plugin_name}}"
    / "pyproject.toml"
)

FLOOR_STATEMENTS = [
    ("packages/*/pyproject.toml", r'requires-python = ">=(\d+\.\d+)"'),
    (
        "packages/*/pyproject.toml",
        r'"Programming Language :: Python :: 3",\s*'
        r'"Programming Language :: Python :: (\d+\.\d+)"',
    ),
    ("packages/kalinka-plugin-sdk/debian/control.in", r"python3 \(>= (\d+\.\d+)\)"),
    ("packages/kalinka-plugin-upnp/debian/control.in", r"python3 \(>= (\d+\.\d+)\)"),
    ("packages/kalinka-server/DEBIAN/control.in", r"python3 \(>= (\d+\.\d+)\)"),
    ("packages/kalinka-server/rpm/kalinka-server.spec", r"python3 >= (\d+\.\d+)"),
    ("packages/kalinka-server/pyproject.toml", r'python_version = "(\d+\.\d+)"'),
    ("packages/kalinka-server/pyproject.toml", r"target-version = \['py(\d)(\d+)'\]"),
    ("packages/kalinka-plugin-dummydevice/README.md", r"Python (\d+\.\d+)\+"),
    ("packages/kalinka-plugin-musiccast/README.md", r"Python (\d+\.\d+)\+"),
    (
        "packages/kalinka-plugin-localfiles/src/kalinka_plugin_localfiles/"
        "procedural_artwork/README.md",
        r"Python >= (\d+\.\d+)",
    ),
    ("Makefile", r"(?:Python|but) >= (\d+\.\d+)"),
    ("Makefile", r"version_info\[:2\] >= \((\d+), (\d+)\)"),
    ("docs/development.md", r"Python (\d+\.\d+)\+"),
    ("docs/development.md", r"older than (\d+\.\d+)"),
    ("template/cookiecutter-kalinka-plugin/README.md", r"Python (\d+\.\d+)\+"),
    (
        "template/cookiecutter-kalinka-plugin/README.md",
        r'Python version \(default: "(\d+\.\d+)"\)',
    ),
    (
        "template/cookiecutter-kalinka-plugin/README.md",
        r"python_version \[(\d+\.\d+)\]",
    ),
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


def test_a_generated_plugin_states_its_floor_only_in_requires_python():
    # Fixed minor-version classifiers would contradict any python_version but one.
    pyproject = PLUGIN_TEMPLATE_PYPROJECT.read_text()
    assert 'requires-python = ">={{ cookiecutter.python_version }}"' in pyproject
    assert re.findall(r"Programming Language :: Python :: 3\.\d+", pyproject) == []
