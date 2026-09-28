"""What the deb's maintainer scripts do on an upgrade rather than a removal.

Both behaviours here were misreported on a real 4.3.0 -> 4.3.1 upgrade: the
prerm announced a removal that was not happening, and the renderer installer
blamed itself for an apt failure that belonged to another half-configured
package. Neither broke the install; both told the operator something untrue.

The release installer that auto-upgrade reruns is here too, for what it asks
apt to bring: on a headless box, one flag decides between the packages Kalinka
uses and a graphics stack that nothing on it will ever draw with.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PRERM = REPO / "packages" / "kalinka-server" / "DEBIAN" / "prerm"
INSTALL_RENDERER = REPO / "scripts" / "install-renderer.sh"
INSTALL_RELEASE = REPO / "scripts" / "install-release.sh"


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


def test_the_renderer_takes_no_recommends(tmp_path):
    """install-release.sh runs it first, so whatever it lets in reaches every
    box the release install keeps recommends off."""
    binv = _stub_bin(tmp_path, installed_version="0.4.0")
    log = tmp_path / "apt-get.log"
    (binv / "apt-get").write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\n')

    result = _renderer_install_block(tmp_path, binv)

    assert result.returncode == 0, result.stderr
    assert "--no-install-recommends" in log.read_text().split()


EXTRAS = {"libchromaprint-tools", "build-essential", "python3-dev"}

EITHER_PATH = pytest.mark.parametrize(
    "bundle_fails", [False, True], ids=["apt", "dpkg-fallback"]
)

_RELEASE_STUBS = {
    # A fetch prints the release; a download leaves an empty .deb behind.
    "curl": """#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; esac
  shift
done
if [ -n "$out" ]; then : > "$out"; else cat "$STUB_STATE/release.json"; fi
""",
    # Records what it installed by name, the way dpkg-query below reads it. A
    # simulation takes no lock and installs nothing, so it is not logged.
    "apt-get": """#!/usr/bin/env bash
if [ "$1" = -s ]; then
  grep -qxF "${!#}" "$STUB_STATE/unknown" && exit 100
  exit 0
fi
echo "$*" >> "$STUB_STATE/apt-get.log"
names=()
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift ;;
    -*|install|update) ;;
    *) names+=("$1") ;;
  esac
  shift
done
for name in "${names[@]}"; do
  case "$name" in
    *.deb) [ "$STUB_BUNDLE_FAILS" = 1 ] && exit 100 ;;
    *) [ "$STUB_NAMES_FAIL" = 1 ] && exit 100 ;;
  esac
  grep -qxF "$name" "$STUB_STATE/unknown" && exit 100
done
for name in "${names[@]}"; do
  case "$name" in *.deb) ;; *) echo "$name" >> "$STUB_STATE/installed" ;; esac
done
exit 0
""",
    "dpkg": """#!/usr/bin/env bash
echo "$*" >> "$STUB_STATE/dpkg.log"
exit 1
""",
    "dpkg-query": """#!/usr/bin/env bash
pkg="${!#}"
grep -qxF "$pkg" "$STUB_STATE/installed" || exit 1
case "$2" in *Status*) printf 'install ok installed' ;; *) echo "   $pkg 9.9.9" ;; esac
""",
    "sudo": '#!/usr/bin/env bash\nexec "$@"\n',
}


def _install_release(tmp_path, *, bundle_fails=False, names_fail=False, unknown=()):
    """install-release.sh end to end, against one published release and no
    package database.

    apt logs every call. ``bundle_fails`` makes it refuse the downloaded debs,
    which sends the script down its dpkg -i fallback; ``names_fail`` makes it
    refuse every install by name, as a lock it waited out does; ``unknown``
    names packages it cannot install, failing any call that asks for one — what
    a distribution without that package, or with a conflicting one, does.
    """
    binv = tmp_path / "bin"
    binv.mkdir()
    for name, text in _RELEASE_STUBS.items():
        (binv / name).write_text(text)
        (binv / name).chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    assets = [
        {"name": n, "browser_download_url": f"https://x.invalid/{n}"}
        for n in ("kalinka-server_9.9.9_all.deb", "kalinka-plugin-sdk_9.9.9_all.deb")
    ]
    release = [{"tag_name": "kalinka-v9.9.9", "assets": assets}]
    (state / "release.json").write_text(json.dumps(release))
    (state / "unknown").write_text("".join(f"{name}\n" for name in unknown))
    (state / "installed").write_text("")

    result = subprocess.run(
        ["bash", str(INSTALL_RELEASE)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{binv}:{os.environ['PATH']}",
            "STUB_STATE": str(state),
            "STUB_BUNDLE_FAILS": "1" if bundle_fails else "0",
            "STUB_NAMES_FAIL": "1" if names_fail else "0",
            "KALINKA_WEB": "0",
            "KALINKA_RENDERER": "0",
        },
    )
    log = state / "apt-get.log"
    calls = [c.split() for c in log.read_text().splitlines()] if log.exists() else []
    installed = set((state / "installed").read_text().split())
    return result, calls, installed


def _notes(result):
    return [
        line for line in result.stderr.splitlines() if "could not be installed" in line
    ]


@EITHER_PATH
def test_no_install_takes_every_recommend(bundle_fails, tmp_path):
    """Recommends of the whole transaction follow fpcalc's ffmpeg to Mesa and
    a 118 MB libLLVM, on a box with no display."""
    result, calls, _ = _install_release(tmp_path, bundle_fails=bundle_fails)

    assert result.returncode == 0, result.stderr
    installs = [c for c in calls if "install" in c]
    assert len(installs) >= 2
    assert not any("--install-recommends" in c for c in calls)
    assert all("--no-install-recommends" in c for c in installs)


@EITHER_PATH
def test_what_kalinka_wants_is_asked_for_by_name(bundle_fails, tmp_path):
    """DietPi skips recommends, and a failed apt run skipped them too; fpcalc
    and the toolchain have to come either way."""
    result, calls, installed = _install_release(tmp_path, bundle_fails=bundle_fails)

    assert result.returncode == 0, result.stderr
    assert any(EXTRAS <= set(c) for c in calls)
    assert installed == EXTRAS
    assert _notes(result) == []


@EITHER_PATH
def test_the_names_go_in_before_the_bundle(bundle_fails, tmp_path):
    """The server's postinst restarts kalinka.service, which looks for fpcalc
    and builds a pending Smart Search install only as it starts."""
    result, calls, _ = _install_release(tmp_path, bundle_fails=bundle_fails)

    assert result.returncode == 0, result.stderr
    named = next(i for i, c in enumerate(calls) if EXTRAS <= set(c))
    bundle = next(i for i, c in enumerate(calls) if any(a.endswith(".deb") for a in c))
    assert named < bundle


def test_what_is_already_there_is_not_upgraded(tmp_path):
    """Auto-upgrade reruns the installer, and python3-dev pins python3 by exact
    version: upgrading it would upgrade the interpreter the server runs on."""
    result, calls, _ = _install_release(tmp_path)

    assert result.returncode == 0, result.stderr
    [named] = [c for c in calls if EXTRAS <= set(c)]
    assert "--no-upgrade" in named


def test_a_refused_install_is_not_asked_again_per_package(tmp_path):
    """A held dpkg lock is waited out for five minutes a call; asking once more
    per package kept the unattended upgrade waiting for each of them."""
    result, calls, installed = _install_release(tmp_path, names_fail=True)

    assert result.returncode == 0, result.stderr
    assert len([c for c in calls if EXTRAS & set(c)]) == 1
    assert installed == set()
    assert len(_notes(result)) == len(EXTRAS)
    assert "Done." in result.stdout


@EITHER_PATH
def test_a_package_apt_cannot_install_costs_only_itself(bundle_fails, tmp_path):
    """One name missing from a distribution, or held back by a pinned python3,
    must not take the others down with it, nor the install: the operator is
    told what it costs instead."""
    result, _, installed = _install_release(
        tmp_path, bundle_fails=bundle_fails, unknown=["python3-dev"]
    )

    assert result.returncode == 0, result.stderr
    assert installed == EXTRAS - {"python3-dev"}
    [note] = _notes(result)
    assert "python3-dev" in note and "Smart Search" in note
    assert "Done." in result.stdout


def test_each_package_that_did_not_come_is_named(tmp_path):
    result, _, installed = _install_release(tmp_path, unknown=sorted(EXTRAS))

    assert result.returncode == 0, result.stderr
    assert installed == set()
    notes = _notes(result)
    assert sorted(note.split()[2] for note in notes) == sorted(EXTRAS)
    assert any("libchromaprint-tools" in n and "fpcalc" in n for n in notes)
    assert "apt-get install --no-install-recommends" in result.stderr
