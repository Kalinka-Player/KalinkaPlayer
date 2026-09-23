"""What the deb's maintainer scripts do on an upgrade rather than a removal.

Both behaviours here were misreported on a real 4.3.0 -> 4.3.1 upgrade: the
prerm announced a removal that was not happening, and the renderer installer
blamed itself for an apt failure that belonged to another half-configured
package. Neither broke the install; both told the operator something untrue.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PRERM = REPO / "packages" / "kalinka-server" / "DEBIAN" / "prerm"
INSTALL_RENDERER = REPO / "scripts" / "install-renderer.sh"


def _run(script, *args, env=None):
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def _sandboxed_prerm(tmp_path):
    """The prerm under test, unable to reach the machine it runs on.

    It is written to run as root against fixed paths: it disables a systemd
    unit and uninstalls /opt/kalinka. Run as-is, the removal case prompts the
    tester's polkit agent and — on a box that has Kalinka installed — would
    really uninstall it. Only the two roots are rewritten, into tmp_path, and
    systemctl becomes a stub that records its arguments.
    """
    script = tmp_path / "prerm"
    script.write_text(PRERM.read_text().replace("/opt/kalinka", f"{tmp_path}/opt"))
    binv = tmp_path / "bin"
    binv.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    stub = binv / "systemctl"
    stub.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{systemctl_log}"\n')
    stub.chmod(0o755)
    return script, {"PATH": f"{binv}:{os.environ['PATH']}"}, systemctl_log


@pytest.mark.parametrize("action", ["upgrade", "failed-upgrade"])
def test_prerm_removes_nothing_on_an_upgrade(action, tmp_path):
    """It runs as root on a real box, so the guard has to come before anything
    it could act on — nothing is uninstalled and nothing is announced."""
    script, env, systemctl_log = _sandboxed_prerm(tmp_path)

    result = _run(script, action, "4.3.2", env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "Removing" not in result.stdout + result.stderr
    assert not systemctl_log.exists()


def test_prerm_still_reports_a_real_removal(tmp_path):
    """The message belongs to removal; only the upgrade path is silenced. No
    venv under the sandbox root, so it stops before touching pip."""
    script, env, systemctl_log = _sandboxed_prerm(tmp_path)

    result = _run(script, "remove", env=env)

    assert "Removing Kalinka Server..." in result.stdout
    assert systemctl_log.read_text().splitlines() == [
        "disable --now kalinka-restart.path",
        "disable --now kalinka-journal-reader.socket",
    ]


# ---------------------------------------------------------------- renderer


def _stub_bin(tmp_path, *, installed_version, apt_fails=True):
    """A PATH where apt fails and dpkg reports what is really installed."""
    binv = tmp_path / "bin"
    binv.mkdir(exist_ok=True)
    (binv / "apt-get").write_text(
        "#!/usr/bin/env bash\n" f"exit {1 if apt_fails else 0}\n"
    )
    # dpkg-deb -f <file> <field>: the field name is the third argument.
    (binv / "dpkg-deb").write_text(
        "#!/usr/bin/env bash\n"
        'case "$3" in Package) echo kalinka-renderer;; Version) echo 0.4.0;; esac\n'
    )
    (binv / "dpkg-query").write_text(
        "#!/usr/bin/env bash\n" f"printf '%s' '{installed_version}'\n"
    )
    for stub in binv.iterdir():
        stub.chmod(0o755)
    return binv


def _renderer_install_block(tmp_path, binv):
    """Run just the apt branch of install-renderer.sh, with its inputs bound.

    The script reaches that branch only after a release lookup over the
    network, so the branch is extracted rather than driven end to end.
    """
    text = INSTALL_RENDERER.read_text()
    start = text.index('  if ! $SUDO apt-get "${APT_OPTS[@]}" install -y')
    end = text.index("else\n  echo \">> Installing with dnf ...\"")
    block = text[start:end]
    harness = tmp_path / "block.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'die() { echo "error: $*" >&2; exit 1; }\n'
        'SUDO=""\nAPT_OPTS=()\nTMPDIR_DL="/tmp"\n'
        'NAME="kalinka-renderer-0.4.0.debian-13.arm64.deb"\n'
        'PLATFORM="debian-13"\nARCH="arm64"\nTAG="kalinka-renderer-v0.4.0"\n'
        + block
    )
    return subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{binv}:{os.environ['PATH']}"},
    )


def test_an_apt_failure_is_not_the_renderers_when_it_is_installed(tmp_path):
    """apt configures other pending packages in the same run; one of those
    failing must not send the operator off reinstalling a renderer that is
    already there at the right version."""
    binv = _stub_bin(tmp_path, installed_version="0.4.0")

    result = _renderer_install_block(tmp_path, binv)

    assert result.returncode == 0, result.stderr
    assert "another package's" in result.stderr
    assert "could not install" not in result.stderr


def test_a_renderer_that_really_failed_still_reports_it(tmp_path):
    binv = _stub_bin(tmp_path, installed_version="0.3.0")

    result = _renderer_install_block(tmp_path, binv)

    assert result.returncode == 1
    assert "apt could not install" in result.stderr
