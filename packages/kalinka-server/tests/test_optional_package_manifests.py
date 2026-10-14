"""Each plugin that offers optional packages ships its allow-list.

install_pending installs only what a manifest under
/opt/kalinka/allowed_packages names, so an offer the packages do not ship
is a "Restart with install" that never installs anything. These run the
plugin deb builds and the bundle RPM build with their packaging tools
stubbed, and read the manifests they staged.
"""

import inspect
import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from kalinka_plugin_sdk import optional_packages_manifest

REPO = Path(__file__).resolve().parents[3]
PACKAGES = REPO / "packages"
VERSION = "1.2.3"


def offering_plugins() -> dict[str, type]:
    """{PLUGIN_ID: plugin class} for each plugin in this tree with an offer."""
    plugins = {}
    for entry in entry_points(group="kalinka.plugins"):
        plugin = entry.load()
        in_tree = _package_dir(plugin).parent == PACKAGES
        if in_tree and getattr(plugin, "OPTIONAL_PACKAGES", {}):
            plugins[plugin.PLUGIN_ID] = plugin
    return plugins


def _package_dir(plugin: type) -> Path:
    return Path(inspect.getfile(plugin)).resolve().parents[2]


def _offer(plugin: type) -> dict:
    return optional_packages_manifest(plugin.PLUGIN_ID, plugin.OPTIONAL_PACKAGES)


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def _tools(tmp_path: Path) -> dict[str, str]:
    """A PATH whose python3 is this interpreter, except that building a wheel
    leaves an empty one, and whose dpkg-deb and rpmbuild only leave the
    package file the scripts look for."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(
        bin_dir / "python3",
        f"""if [ "$1 $2" = "-m build" ]; then
  mkdir -p dist && touch "dist/$(basename "$PWD")-{VERSION}-py3-none-any.whl"
else
  exec "{sys.executable}" "$@"
fi""",
    )
    _stub(bin_dir / "dpkg-deb", 'touch "${@: -1}"')
    _stub(
        bin_dir / "rpmbuild",
        f"""while [ $# -gt 0 ]; do
  case "$1 $2" in "--define _topdir "*) topdir="${{2#_topdir }}";; esac
  shift
done
mkdir -p "$topdir/RPMS/noarch"
touch "$topdir/RPMS/noarch/kalinka-server-{VERSION}-1.noarch.rpm"
""",
    )
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def _tree(tmp_path: Path) -> Path:
    """A copy of the repository's layout the build scripts can write into,
    reading every plugin's sources from this checkout."""
    tree = tmp_path / "repo"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "deb_version.sh", tree / "scripts")
    for source in PACKAGES.glob("kalinka-plugin-*"):
        package = tree / "packages" / source.name
        package.mkdir(parents=True)
        (package / "src").symlink_to(source / "src")
    return tree


def test_this_tree_has_plugins_that_offer_packages():
    assert {"localfiles", "jamendo"} <= set(offering_plugins())


@pytest.mark.parametrize("plugin_id", sorted(offering_plugins()))
def test_a_plugins_deb_ships_its_allow_list(plugin_id, tmp_path):
    plugin = offering_plugins()[plugin_id]
    source = _package_dir(plugin)
    package = _tree(tmp_path) / "packages" / source.name
    (package / "debian").symlink_to(source / "debian")
    (package / "scripts").mkdir()
    shutil.copy(source / "scripts" / "build_deb.sh", package / "scripts")
    wheel = f"{source.name.replace('-', '_')}-{VERSION}-py3-none-any.whl"
    _stub(
        package / "scripts" / "build_wheel.sh", f'mkdir -p dist && touch "dist/{wheel}"'
    )

    result = subprocess.run(
        ["bash", "scripts/build_deb.sh"],
        cwd=package,
        env=_tools(tmp_path),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    shipped = package / "pkgroot" / "opt" / "kalinka" / "allowed_packages"
    assert json.loads((shipped / f"{plugin_id}.json").read_text()) == _offer(plugin)


def test_the_bundle_rpm_ships_every_allow_list(tmp_path):
    server = _tree(tmp_path) / "packages" / "kalinka-server"
    (server / "scripts").mkdir(parents=True)
    shutil.copy(
        PACKAGES / "kalinka-server" / "scripts" / "build_rpm.sh", server / "scripts"
    )
    version = server / "src" / "kalinka_server" / "_version.py"
    version.parent.mkdir(parents=True)
    version.write_text(f"__version__ = version = '{VERSION}'\n")

    result = subprocess.run(
        ["bash", str(server / "scripts" / "build_rpm.sh")],
        env=_tools(tmp_path),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    staged = server / "build-rpm-stage" / "allowed_packages"
    shipped = {
        manifest.stem: json.loads(manifest.read_text())
        for manifest in staged.glob("*.json")
    }
    assert shipped == {
        plugin_id: _offer(plugin) for plugin_id, plugin in offering_plugins().items()
    }
