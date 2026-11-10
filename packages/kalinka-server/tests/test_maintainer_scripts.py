"""What the deb's maintainer scripts do on an upgrade rather than a removal.

Both behaviours here were misreported on a real 4.3.0 -> 4.3.1 upgrade: the
prerm announced a removal that was not happening, and the renderer installer
blamed itself for an apt failure that belonged to another half-configured
package. Neither broke the install; both told the operator something untrue.

The release installer that auto-upgrade reruns is here too, for what it asks
apt to bring: on a headless box, one flag decides between the packages Kalinka
uses and a graphics stack that nothing on it will ever draw with.

The postinst makes the server's id, which the app keeps a server's pins under,
before Core first starts, and an upgrade must keep it.

The server's and the renderer's postinst configure where systemd is not
running, or not installed, as in a container or a chroot, and still enable the
units an image build boots with.
"""

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PRERM = REPO / "packages" / "kalinka-server" / "DEBIAN" / "prerm"
INSTALL_RENDERER = REPO / "scripts" / "install-renderer.sh"
INSTALL_RELEASE = REPO / "scripts" / "install-release.sh"
INSTALL_SUPERVISOR = REPO / "scripts" / "install-supervisor.sh"


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
        "disable --now kalinka-plugins.path",
        "disable --now kalinka-journal-reader.socket",
        "disable --now kalinka-mounts.path",
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


def _renderer_block(tmp_path, start, end, env, prelude=""):
    """Run one block of install-renderer.sh from start up to end, extracted
    because the script reaches it only after a release lookup."""
    text = INSTALL_RENDERER.read_text()
    block = text[text.index(start) : text.index(end)]
    harness = tmp_path / "block.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'die() { echo "error: $*" >&2; exit 1; }\n' + prelude + block
    )
    return subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )


SUPERVISOR_BLOCK = ("# --- supervisor (opt-in)", 'echo\necho ">> Done.')


@pytest.mark.parametrize("wanted", ["1", "0", None])
def test_a_renderer_box_gets_a_supervisor_only_on_request(tmp_path, wanted):
    """The app sets up a renderer-only box's sound card through its supervisor."""
    log = tmp_path / "supervisor.log"
    (tmp_path / "install-supervisor.sh").write_text(
        f'echo "KALINKA_REPO=$KALINKA_REPO" >> "{log}"\n'
    )
    env = {} if wanted is None else {"KALINKA_SUPERVISOR": wanted}
    result = _renderer_block(
        tmp_path, *SUPERVISOR_BLOCK, env, prelude='REPO="owner/repo"\n'
    )

    assert result.returncode == 0, result.stderr
    if wanted == "1":
        assert log.read_text() == "KALINKA_REPO=owner/repo\n"
    else:
        assert not log.exists()


def test_a_supervisor_is_refused_before_anything_installs_where_it_has_no_package(
    tmp_path,
):
    start = 'if [ "${KALINKA_SUPERVISOR:-0}" = 1 ] && [ "$FORMAT" != deb ]'
    for fmt, code in (("rpm", 1), ("deb", 0)):
        result = _renderer_block(
            tmp_path,
            start,
            "have python3 || die",
            {"KALINKA_SUPERVISOR": "1"},
            prelude=f"FORMAT={fmt}\n",
        )
        assert result.returncode == code, result.stderr
        if code:
            assert "Debian-based systems only" in result.stderr


EXTRAS = {"libchromaprint-tools", "nfs-common"}
# A second name, for what the installer does with each of several.
STAND_IN = "kalinka-stand-in"

EITHER_PATH = pytest.mark.parametrize(
    "bundle_fails", [False, True], ids=["apt", "dpkg-fallback"]
)

_RELEASE_STUBS = {
    # A fetch prints the release, and so does a lookup saved to a file, as
    # install-supervisor.sh makes; a download leaves an empty .deb behind, or
    # an installer that logs its own run.
    "curl": """#!/usr/bin/env bash
out="" url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -H|--retry) shift ;;
    -*) ;;
    *) url="$1" ;;
  esac
  shift
done
echo "$url" >> "$STUB_STATE/curl.log"
case "$out" in
  "") cat "$STUB_STATE/release.json" ;;
  *.json) cat "$STUB_STATE/release.json" > "$out" ;;
  *.sh) echo 'echo "${0##*/}" >> "$STUB_STATE/scripts.log"' > "$out" ;;
  *) : > "$out" ;;
esac
""",
    # Records what it installed by name, the way dpkg-query below reads it. A
    # simulation takes no lock and installs nothing, so it is not logged.
    "apt-get": """#!/usr/bin/env bash
if [ "$1" = -s ]; then
  grep -qxF "${!#}" "$STUB_STATE/unknown" && exit 100
  exit 0
fi
echo "$*" >> "$STUB_STATE/apt-get.log"
[ "${STUB_REPAIR_FAILS:-0}" = 1 ] && [[ " $* " == *" -f "* ]] && exit 100
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
[ "$1" = --print-architecture ] && { echo arm64; exit 0; }
echo "$*" >> "$STUB_STATE/dpkg.log"
exit 1
""",
    "dpkg-query": """#!/usr/bin/env bash
pkg="${!#}"
grep -qxF "$pkg" "$STUB_STATE/installed" || exit 1
case "$2" in
  *db:Status-Status*) printf installed ;;
  *Status*) printf 'install ok installed' ;;
  *) echo "   $pkg 9.9.9" ;;
esac
""",
    "sudo": '#!/usr/bin/env bash\nexec "$@"\n',
    "systemctl": '#!/usr/bin/env bash\necho "$*" >> "$STUB_STATE/systemctl.log"\n',
}


def _published(tmp_path, tag, *assets, unknown=(), installed=()):
    """A PATH on which every GitHub lookup answers with one release of
    ``assets``, and the state directory the stubs record their calls in."""
    binv = tmp_path / "bin"
    binv.mkdir()
    for name, text in _RELEASE_STUBS.items():
        (binv / name).write_text(text)
        (binv / name).chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    release = {
        "tag_name": tag,
        "assets": [
            {"name": n, "browser_download_url": f"https://x.invalid/{n}"}
            for n in assets
        ],
    }
    (state / "release.json").write_text(json.dumps([release]))
    (state / "unknown").write_text("".join(f"{name}\n" for name in unknown))
    (state / "installed").write_text("".join(f"{name}\n" for name in installed))
    return binv, state


def _github_lookups(state):
    return [
        url
        for url in (state / "curl.log").read_text().splitlines()
        if url.startswith("https://api.github.com/")
    ]


def _install_release(
    tmp_path,
    *,
    bundle_fails=False,
    names_fail=False,
    unknown=(),
    bundle_assets=(),
    app_assets=(),
    installed=(),
    env=None,
    piped=False,
    stand_in=False,
):
    """install-release.sh end to end, against one published release and no
    package database.

    apt logs every call. ``bundle_fails`` makes it refuse the downloaded debs,
    which sends the script down its dpkg -i fallback; ``names_fail`` makes it
    refuse every install by name, as a lock it waited out does; ``unknown``
    names packages it cannot install, failing any call that asks for one — what
    a distribution without that package, or with a conflicting one, does.
    ``app_assets`` are published beside the bundle, where the app repo's
    lookup finds them too; ``installed`` are packages already on the box.
    ``piped`` feeds the script to bash on stdin, as ``curl … | sudo bash`` does.
    ``stand_in`` adds ``STAND_IN`` to the packages it asks for by name.
    """
    binv, state = _published(
        tmp_path,
        "kalinka-v9.9.9",
        "kalinka-server_9.9.9_all.deb",
        "kalinka-plugin-sdk_9.9.9_all.deb",
        *bundle_assets,
        *app_assets,
        unknown=unknown,
        installed=installed,
    )
    # A reinstall removes the server's environment and a display edits the boot
    # config, so the script only reaches their copies here.
    script = tmp_path / "install-release.sh"
    text = (
        INSTALL_RELEASE.read_text()
        .replace("/opt/kalinka", f"{tmp_path}/opt/kalinka")
        .replace("/boot/firmware", f"{tmp_path}/boot/firmware")
    )
    if stand_in:
        extras = "declare -A EXTRAS=(\n"
        assert extras in text
        text = text.replace(extras, f'{extras}  [{STAND_IN}]="the stand-in is lost"\n')
    script.write_text(text)

    result = subprocess.run(
        ["bash"] if piped else ["bash", str(script)],
        input=script.read_text() if piped else None,
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
            **(env or {}),
        },
    )
    log = state / "apt-get.log"
    calls = [c.split() for c in log.read_text().splitlines()] if log.exists() else []
    installed = set((state / "installed").read_text().split()) - set(installed)
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
    has to come either way. Nothing else does: no install builds from source,
    so no box needs a compiler."""
    result, calls, installed = _install_release(tmp_path, bundle_fails=bundle_fails)

    assert result.returncode == 0, result.stderr
    assert any(EXTRAS <= set(c) for c in calls)
    assert installed == EXTRAS
    assert _notes(result) == []


@EITHER_PATH
def test_the_names_go_in_before_the_bundle(bundle_fails, tmp_path):
    """The server's postinst restarts kalinka.service, which looks for fpcalc
    only as it starts."""
    result, calls, _ = _install_release(tmp_path, bundle_fails=bundle_fails)

    assert result.returncode == 0, result.stderr
    named = next(i for i, c in enumerate(calls) if EXTRAS <= set(c))
    bundle = next(i for i, c in enumerate(calls) if any(a.endswith(".deb") for a in c))
    assert named < bundle


def test_what_is_already_there_is_not_upgraded(tmp_path):
    """Auto-upgrade reruns the installer, and the operating system's packages
    are their owner's to update, not Kalinka's."""
    result, calls, _ = _install_release(tmp_path)

    assert result.returncode == 0, result.stderr
    [named] = [c for c in calls if EXTRAS <= set(c)]
    assert "--no-upgrade" in named


def test_a_refused_install_is_not_asked_again_per_package(tmp_path):
    """A held dpkg lock is waited out for five minutes a call; asking once more
    per package kept the unattended upgrade waiting for each of them."""
    result, calls, installed = _install_release(
        tmp_path, names_fail=True, stand_in=True
    )

    assert result.returncode == 0, result.stderr
    assert len([c for c in calls if (EXTRAS | {STAND_IN}) & set(c)]) == 1
    assert installed == set()
    assert len(_notes(result)) == len(EXTRAS | {STAND_IN})
    assert "Done." in result.stdout


@EITHER_PATH
def test_a_package_apt_cannot_install_costs_only_itself(bundle_fails, tmp_path):
    """One name missing from a distribution, or held back by a conflicting
    package, must not take the others down with it, nor the install: the
    operator is told what it costs instead."""
    result, _, installed = _install_release(
        tmp_path,
        bundle_fails=bundle_fails,
        unknown=["libchromaprint-tools"],
        stand_in=True,
    )

    assert result.returncode == 0, result.stderr
    assert installed == (EXTRAS - {"libchromaprint-tools"}) | {STAND_IN}
    [note] = _notes(result)
    assert "libchromaprint-tools" in note and "fpcalc" in note
    assert "Done." in result.stdout


def test_each_package_that_did_not_come_is_named(tmp_path):
    result, _, installed = _install_release(
        tmp_path, unknown=sorted(EXTRAS | {STAND_IN}), stand_in=True
    )

    assert result.returncode == 0, result.stderr
    assert installed == set()
    notes = _notes(result)
    assert sorted(note.split()[2] for note in notes) == sorted(EXTRAS | {STAND_IN})
    assert any("libchromaprint-tools" in n and "fpcalc" in n for n in notes)
    assert "apt-get install --no-install-recommends" in result.stderr


KIOSK_ARM64 = "kalinka-kiosk_9.9.9_arm64.deb"
KIOSK_AMD64 = "kalinka-kiosk_9.9.9_amd64.deb"


def _bundle_debs(calls):
    return {
        a.rsplit("/", 1)[-1] for c in calls for a in c if a.endswith(".deb")
    }


@EITHER_PATH
def test_upnp_is_installed_with_the_bundle(bundle_fails, tmp_path):
    upnp_deb = "kalinka-plugin-upnp_9.9.9_all.deb"
    result, calls, _ = _install_release(
        tmp_path, bundle_fails=bundle_fails, bundle_assets=(upnp_deb,)
    )

    assert result.returncode == 0, result.stderr
    assert upnp_deb in _bundle_debs(calls)
    if bundle_fails:
        dpkg_calls = [
            line.split()
            for line in (tmp_path / "state" / "dpkg.log").read_text().splitlines()
        ]
        assert upnp_deb in _bundle_debs(dpkg_calls)
        assert any("install" in call and "-f" in call for call in calls)


def test_the_display_comes_only_when_asked_for(tmp_path):
    """It brings Mesa and GStreamer, which a headless box has no use for."""
    result, calls, _ = _install_release(tmp_path, app_assets=(KIOSK_ARM64,))

    assert result.returncode == 0, result.stderr
    assert KIOSK_ARM64 not in _bundle_debs(calls)


def test_the_display_asked_for_is_this_machines_build(tmp_path):
    result, calls, _ = _install_release(
        tmp_path,
        app_assets=(KIOSK_AMD64, KIOSK_ARM64),
        env={"KALINKA_DISPLAY": "1"},
    )

    assert result.returncode == 0, result.stderr
    debs = _bundle_debs(calls)
    assert KIOSK_ARM64 in debs
    assert KIOSK_AMD64 not in debs


def test_an_installed_display_is_upgraded(tmp_path):
    """Auto-upgrade reruns the installer with no switches."""
    result, calls, _ = _install_release(
        tmp_path, app_assets=(KIOSK_ARM64,), installed=("kalinka-kiosk",)
    )

    assert result.returncode == 0, result.stderr
    assert KIOSK_ARM64 in _bundle_debs(calls)


def test_no_display_for_this_machine_still_installs_the_rest(tmp_path):
    result, calls, _ = _install_release(
        tmp_path, app_assets=(KIOSK_AMD64,), env={"KALINKA_DISPLAY": "1"}
    )

    assert result.returncode == 0, result.stderr
    assert "kalinka-server_9.9.9_all.deb" in _bundle_debs(calls)
    assert "no kalinka-kiosk package for arm64" in result.stderr


# The lines of DietPi's RPi config.txt the display cares about.
DIETPI_BOOT_CONFIG = """\
#-------GPU memory splits-------
gpu_mem_256=16
gpu_mem_512=16
gpu_mem_1024=16

#-------Display---------
hdmi_blanking=1

# Enable KMS/DRM display driver, recommended when a GUI application or desktop is used.
#dtoverlay=vc4-kms-v3d,noaudio
dtoverlay=hifiberry-dac
"""

REBOOT = "reboot for the display to start"


def _boot_config(tmp_path, text):
    config = tmp_path / "boot" / "firmware" / "config.txt"
    config.parent.mkdir(parents=True)
    config.write_text(text)
    return config


def _kms_lines(config):
    return [line for line in config.read_text().splitlines() if "vc4-kms-v3d" in line]


def test_the_display_switches_its_driver_on(tmp_path):
    """flutter-pi draws through KMS alone, which DietPi ships commented out
    beside a 16 MB GPU split."""
    config = _boot_config(tmp_path, DIETPI_BOOT_CONFIG)

    result, _, _ = _install_release(
        tmp_path, app_assets=(KIOSK_ARM64,), env={"KALINKA_DISPLAY": "1"}
    )

    assert result.returncode == 0, result.stderr
    assert _kms_lines(config) == ["dtoverlay=vc4-kms-v3d,noaudio"]
    assert "gpu_mem" not in config.read_text()
    assert "dtoverlay=hifiberry-dac" in config.read_text()
    assert REBOOT in result.stdout


def test_a_config_without_dietpis_kms_line_gets_one_for_every_board(tmp_path):
    config = _boot_config(tmp_path, "[pi4]\ndtparam=audio=off\n")

    result, _, _ = _install_release(
        tmp_path, app_assets=(KIOSK_ARM64,), env={"KALINKA_DISPLAY": "1"}
    )

    assert result.returncode == 0, result.stderr
    assert config.read_text().endswith("\n[all]\ndtoverlay=vc4-kms-v3d,noaudio\n")
    assert len(_kms_lines(config)) == 1


def test_a_driver_already_on_is_left_as_it_is(tmp_path):
    """Auto-upgrade reruns the installer on every box with the display."""
    config = _boot_config(tmp_path, "[all]\ndtoverlay=vc4-kms-v3d\n")

    result, _, _ = _install_release(
        tmp_path, app_assets=(KIOSK_ARM64,), installed=("kalinka-kiosk",)
    )

    assert result.returncode == 0, result.stderr
    assert config.read_text() == "[all]\ndtoverlay=vc4-kms-v3d\n"
    assert REBOOT not in result.stdout


@pytest.mark.parametrize(
    "env, assets",
    [({}, (KIOSK_ARM64,)), ({"KALINKA_DISPLAY": "1"}, (KIOSK_AMD64,))],
    ids=["not-asked-for", "no-package-for-this-machine"],
)
def test_without_the_display_the_boot_config_is_left_alone(env, assets, tmp_path):
    config = _boot_config(tmp_path, DIETPI_BOOT_CONFIG)

    result, _, _ = _install_release(tmp_path, app_assets=assets, env=env)

    assert result.returncode == 0, result.stderr
    assert config.read_text() == DIETPI_BOOT_CONFIG


def test_a_machine_that_boots_without_config_txt_still_gets_the_display(tmp_path):
    result, calls, _ = _install_release(
        tmp_path, app_assets=(KIOSK_ARM64,), env={"KALINKA_DISPLAY": "1"}
    )

    assert result.returncode == 0, result.stderr
    assert KIOSK_ARM64 in _bundle_debs(calls)
    assert not (tmp_path / "boot").exists()


@pytest.mark.parametrize("piped", [False, True], ids=["file", "piped"])
def test_a_supervisor_box_installs_however_the_installer_is_run(piped, tmp_path):
    """Piped into bash, the script has no path of its own, and looking beside
    it for the supervisor's installer stopped the install before apt ran."""
    result, calls, _ = _install_release(
        tmp_path,
        app_assets=(KIOSK_ARM64,),
        installed=("kalinka-supervisor",),
        env={"KALINKA_DISPLAY": "1"},
        piped=piped,
    )

    assert result.returncode == 0, result.stderr
    assert {"kalinka-server_9.9.9_all.deb", KIOSK_ARM64} <= _bundle_debs(calls)
    scripts = (tmp_path / "state" / "scripts.log").read_text()
    assert scripts == "install-supervisor.sh\n"


def test_a_reinstall_repairs_a_box_already_on_the_release(tmp_path):
    """The supervisor's Reinstall: apt skips a deb at the installed version
    unless told otherwise, and bootstrap only recreates an environment that
    is missing, so a plain rerun would leave a broken box broken."""
    venv = tmp_path / "opt" / "kalinka" / "venv"
    (venv / "bin").mkdir(parents=True)

    result, calls, _ = _install_release(tmp_path, env={"KALINKA_REINSTALL": "1"})

    assert result.returncode == 0, result.stderr
    bundle = [c for c in calls if any(a.endswith(".deb") for a in c)]
    assert bundle and all("--reinstall" in c for c in bundle)
    assert not any("--reinstall" in c for c in calls if c not in bundle)
    assert not venv.exists()
    systemctl = (tmp_path / "state" / "systemctl.log").read_text().splitlines()
    assert systemctl[0] == "stop kalinka.service"
    assert systemctl[-1] == "--no-block start kalinka.service"


def test_a_failed_reinstall_does_not_leave_the_server_stopped(tmp_path):
    """Both apt and the dpkg fallback refuse: the script fails, but the server
    is started again, and its bootstrap rebuilds the environment it lost."""
    (tmp_path / "opt" / "kalinka" / "venv").mkdir(parents=True)

    result, _, _ = _install_release(
        tmp_path,
        bundle_fails=True,
        env={"KALINKA_REINSTALL": "1", "STUB_REPAIR_FAILS": "1"},
    )

    assert result.returncode != 0
    systemctl = (tmp_path / "state" / "systemctl.log").read_text().splitlines()
    assert systemctl[-1] == "--no-block start kalinka.service"


def test_an_upgrade_keeps_the_environment(tmp_path):
    venv = tmp_path / "opt" / "kalinka" / "venv"
    (venv / "bin").mkdir(parents=True)

    result, calls, _ = _install_release(tmp_path)

    assert result.returncode == 0, result.stderr
    assert not any("--reinstall" in c for c in calls)
    assert venv.exists()
    assert not (tmp_path / "state" / "systemctl.log").exists()


def test_a_reinstall_installs_the_renderer_again(tmp_path):
    binv = _stub_bin(tmp_path, installed_version="0.4.0")
    log = tmp_path / "apt-get.log"
    (binv / "apt-get").write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\n')
    text = INSTALL_RENDERER.read_text()
    start = text.index("  APT_OPTS=(-o DPkg::Lock::Timeout=300)")
    end = text.index("else\n  echo \">> Installing with dnf ...\"")
    harness = tmp_path / "block.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'die() { echo "error: $*" >&2; exit 1; }\n'
        'SUDO=""\nTMPDIR_DL="/tmp"\nNAME="kalinka-renderer-0.4.0.debian-13.arm64.deb"\n'
        'PLATFORM="debian-13"\nARCH="arm64"\nTAG="kalinka-renderer-v0.4.0"\n'
        + text[start:end]
    )

    result = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{binv}:{os.environ['PATH']}",
            "KALINKA_REINSTALL": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "--reinstall" in log.read_text().split()


FETCH_AND_RUN = {
    "upgrade": REPO / "packages" / "kalinka-server" / "scripts" / "upgrade.sh",
    "upgrade-renderer": REPO / "packages" / "kalinka-renderer" / "scripts" / "upgrade-renderer.sh",
    "reinstall": REPO / "packages" / "kalinka-supervisor" / "reinstall.sh",
}


@pytest.mark.parametrize("script", FETCH_AND_RUN.values(), ids=FETCH_AND_RUN.keys())
def test_a_fetched_installer_runs_where_nobody_else_can_write(script, tmp_path):
    """The installers run any install-*.sh they find beside themselves, as
    root: fetched into the shared /tmp, any local user could put one there."""
    shared = tmp_path / "tmp"
    shared.mkdir()
    planted = {"install-renderer.sh", "install-supervisor.sh"}
    for name in planted:
        (shared / name).write_text("echo planted\n")
    binv = tmp_path / "bin"
    binv.mkdir()
    log = tmp_path / "installer.log"
    report = 'd="$(dirname "$0")"; stat -c %a "$d" > "$LOG"; ls -A "$d" >> "$LOG"'
    (binv / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
        f"echo '{report}' > \"$out\"\n"
    )
    (binv / "curl").chmod(0o755)

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{binv}:{os.environ['PATH']}",
            "TMPDIR": str(shared),
            "LOG": str(log),
            "KALINKA_RENDERER_UPGRADE_TRIGGER": str(tmp_path / "no-request"),
        },
    )

    assert result.returncode == 0, result.stderr
    mode, *beside = log.read_text().split()
    assert mode == "700"
    assert beside == ["install.sh"]
    assert {p.name for p in shared.iterdir()} == planted


def test_releases_are_looked_up_where_the_project_publishes_them(
    tmp_path, monkeypatch
):
    """The old owner's repositories only redirect to the project's, and one
    created again under either name would be installed from instead, unsigned."""
    monkeypatch.delenv("KALINKA_REPO", raising=False)
    monkeypatch.delenv("KALINKA_WEB_REPO", raising=False)

    result, _, _ = _install_release(tmp_path, env={"KALINKA_WEB": "1"})

    assert result.returncode == 0, result.stderr
    assert _github_lookups(tmp_path / "state") == [
        "https://api.github.com/repos/Kalinka-Player/KalinkaPlayer/releases?per_page=30",
        "https://api.github.com/repos/Kalinka-Player/KalinkaAI/releases?per_page=30",
    ]
    assert "release in Kalinka-Player/KalinkaPlayer ..." in result.stdout
    assert "app packages in Kalinka-Player/KalinkaAI ..." in result.stdout


def test_a_renderer_box_looks_up_its_release_where_the_project_publishes_it(
    tmp_path, monkeypatch
):
    """A renderer box runs it on its own, with no release installer above it to
    pass a repository down."""
    monkeypatch.delenv("KALINKA_REPO", raising=False)
    monkeypatch.delenv("KALINKA_SUPERVISOR", raising=False)
    binv, state = _published(
        tmp_path,
        "kalinka-renderer-v9.9.9",
        "kalinka-renderer-9.9.9.debian-13.arm64.deb",
    )

    result = _run(
        INSTALL_RENDERER,
        env={"PATH": f"{binv}:{os.environ['PATH']}", "STUB_STATE": str(state)},
    )

    assert result.returncode == 0, result.stderr
    assert _github_lookups(state) == [
        "https://api.github.com/repos/Kalinka-Player/KalinkaPlayer/releases?per_page=30",
    ]


def test_a_supervisor_looks_up_its_release_where_the_project_publishes_it(
    tmp_path, monkeypatch
):
    """install-release.sh runs it for a box that has one without passing its
    repository down."""
    monkeypatch.delenv("KALINKA_REPO", raising=False)
    binv, state = _published(tmp_path, "kalinka-v9.9.9")

    result = _run(
        INSTALL_SUPERVISOR,
        env={
            "PATH": f"{binv}:{os.environ['PATH']}",
            "STUB_STATE": str(state),
            "ALLOW_MISSING_RELEASE": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _github_lookups(state) == [
        "https://api.github.com/repos/Kalinka-Player/KalinkaPlayer/releases?per_page=100",
    ]


POSTINST = REPO / "packages" / "kalinka-server" / "DEBIAN" / "postinst"
BUILD_DEB = REPO / "packages" / "kalinka-server" / "scripts" / "build_deb.sh"
PLUGINS_DROPIN = (
    REPO / "packages" / "kalinka-server" / "scripts" / "kalinka-restart-plugins.conf"
)


def test_the_live_plugin_units_are_packaged_and_enabled(tmp_path):
    scripts = REPO / "packages" / "kalinka-server" / "scripts"
    assert (scripts / "kalinka-plugins.path").exists()
    service = (scripts / "kalinka-plugins.service").read_text()
    assert (
        "ExecStart=-/opt/kalinka/venv/bin/python "
        "-m kalinka_server.plugin_management.operations --live" in service
    )
    assert "ExecStartPost=-/bin/rm -f /run/kalinka/plugin-live-request" in service
    build = BUILD_DEB.read_text()
    assert "kalinka-plugins.path" in build and "kalinka-plugins.service" in build
    _, calls, _, _ = _sandboxed_configure(tmp_path)
    assert "systemctl enable kalinka-plugins.path" in calls
    assert "systemctl start kalinka-plugins.path" in calls


def test_restart_service_runs_the_plugin_helper_before_core():
    dropin = PLUGINS_DROPIN.read_text()
    assert (
        "ExecStartPre=-/opt/kalinka/venv/bin/python "
        "-m kalinka_server.plugin_management.operations" in dropin
    )
    assert "kalinka-restart.service.d/plugins.conf" in BUILD_DEB.read_text()


def _recorder(name, then=""):
    return f'#!/usr/bin/env bash\necho "{name} $*" >> "$STUB_STATE/calls.log"\n{then}'


RENDERER_POSTINST = REPO / "packages" / "kalinka-renderer" / "DEBIAN" / "postinst"
SERVER_UNITS = (
    "kalinka-restart.path",
    "kalinka-plugins.path",
    "kalinka-upgrade.path",
    "kalinka-journal-reader.socket",
    "kalinka-mounts.path",
)
SYSTEMD_RUNNING = "systemd running"
SYSTEMD_STOPPED = "systemd not running"
NO_SYSTEMCTL = "no systemctl"
# What systemctl answers where systemd is not PID 1: verbs on unit files work,
# anything that needs the manager does not.
UNBOOTED_SYSTEMCTL = """\
verb="" now=""
for arg; do
  case "$arg" in
    --now) now=1 ;;
    -*) ;;
    *) [ -n "$verb" ] || verb="$arg" ;;
  esac
done
case "$verb" in
  enable|disable|reenable|is-enabled|mask|unmask|preset) [ -n "$now" ] || exit 0 ;;
esac
echo "System has not been booted with systemd as init system (PID 1)." >&2
exit 1
"""
# What the postinsts run from the host where no systemctl may be found.
HOST_TOOLS = "bash cat chmod find grep mv python3 readlink sync tr".split()


def _sandbox_root(tmp_path):
    """Where _postinst_sandbox moves the system paths of a postinst."""
    return tmp_path / "root"


def _postinst_sandbox(
    tmp_path, postinst, systemd, units=(), on_systemctl="", args=("configure",)
):
    """*postinst* run with *args*, against _sandbox_root(tmp_path).

    Every system path in it is moved under that root, and what reaches past
    the files — users, systemd, udev — is a recorder. Only root can give a
    file away, so install drops the owner it is asked for and chown only
    records it. *systemd* is one of SYSTEMD_RUNNING, SYSTEMD_STOPPED, as in
    a container or chroot, and NO_SYSTEMCTL. *units* are unit files made in
    the root's /etc/systemd/system, and *on_systemctl* is shell the systemctl
    recorder runs. Returns the result, the recorded calls and the root.
    """
    root = _sandbox_root(tmp_path)
    script = tmp_path / "postinst"
    script.write_text(
        re.sub(
            r'(?<=[\s"=])/(?=(?:etc|opt|run|srv|sys|usr|var)/)',
            lambda _: f"{root}/",
            postinst.read_text(),
        )
    )
    unit_dir = root / "etc" / "systemd" / "system"
    unit_dir.mkdir(parents=True)
    for unit in units:
        (unit_dir / unit).touch()
    if systemd == SYSTEMD_RUNNING:
        (root / "run" / "systemd" / "system").mkdir(parents=True)

    binv = tmp_path / "bin"
    binv.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    stubs = {
        "id": "#!/usr/bin/env bash\nexit 1\n",
        "install": _recorder(
            "install",
            "args=()\n"
            "while [ $# -gt 0 ]; do\n"
            '  case "$1" in -o|-g) shift ;; *) args+=("$1") ;; esac\n'
            "  shift\n"
            "done\n"
            f'exec {shutil.which("install")} "${{args[@]}}"\n',
        ),
        **{
            name: _recorder(name)
            for name in (
                "adduser",
                "usermod",
                "gpasswd",
                "chown",
                "systemd-tmpfiles",
                "udevadm",
            )
        },
    }
    if systemd == SYSTEMD_RUNNING:
        stubs["systemctl"] = _recorder("systemctl", on_systemctl)
    elif systemd == SYSTEMD_STOPPED:
        stubs["systemctl"] = _recorder("systemctl", on_systemctl + UNBOOTED_SYSTEMCTL)
    for name, text in stubs.items():
        (binv / name).write_text(text)
        (binv / name).chmod(0o755)
    if systemd == NO_SYSTEMCTL:
        for tool in HOST_TOOLS:
            (binv / tool).symlink_to(shutil.which(tool))
        path = str(binv)
    else:
        path = f"{binv}:{os.environ['PATH']}"

    result = _run(script, *args, env={"PATH": path, "STUB_STATE": str(state)})
    log = state / "calls.log"
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls, root


def _sandboxed_configure(tmp_path, server_id=None, systemd=SYSTEMD_RUNNING):
    """The server postinst's configure, as _postinst_sandbox runs it.

    ``server_id`` is what the box already holds there, if anything. Returns
    the result, the recorded calls, the id path and what it held when
    kalinka.service was started.
    """
    path = _sandbox_root(tmp_path) / "var" / "lib" / "kalinka" / "server_id"
    if server_id is not None:
        path.parent.mkdir(parents=True)
        path.write_text(server_id)
    at_start = tmp_path / "id-at-start"
    result, calls, _ = _postinst_sandbox(
        tmp_path,
        POSTINST,
        systemd,
        units=("kalinka.service", *SERVER_UNITS),
        on_systemctl='[ "$*" != "restart kalinka.service" ] || '
        f'cat "{path}" > "{at_start}" 2>/dev/null || true\n',
    )
    return result, calls, path, at_start.read_text() if at_start.exists() else None


@pytest.mark.parametrize(
    "left", [None, "", "\n", "not-a-uuid\n"], ids=["none", "empty", "blank", "malformed"]
)
def test_a_fresh_install_has_its_server_id_before_core_first_starts(left, tmp_path):
    """The supervisor reports it from install on, rather than once Core has
    run; an empty file, which a power cut mid-write can leave, is no id, nor
    is anything else Core would replace as malformed."""
    result, calls, path, at_start = _sandboxed_configure(tmp_path, server_id=left)

    assert result.returncode == 0, result.stderr
    server_id = path.read_text().strip()
    assert uuid.UUID(server_id).version == 4
    assert at_start == path.read_text()
    assert f"chown kalusr:kalusr {path}.new" in calls
    assert not path.with_name("server_id.new").exists()
    if left is None:
        assert f"install -d -m 0750 -o kalusr -g kalusr {path.parent}" in calls
        assert path.parent.stat().st_mode & 0o777 == 0o750


def test_an_upgrade_keeps_the_server_id(tmp_path):
    """A new one would orphan the renderer sessions Core holds and leave the
    app's pins under an id nobody has."""
    kept = "0b0c5a3e-8c1b-4a51-9a0d-6c9b1c7f1e2a\n"

    result, calls, path, at_start = _sandboxed_configure(tmp_path, server_id=kept)

    assert result.returncode == 0, result.stderr
    assert path.read_text() == kept
    assert at_start == kept
    assert not any(str(path) in call for call in calls)


def _systemctl_calls(calls):
    return [
        call.removeprefix("systemctl ")
        for call in calls
        if call.startswith("systemctl ")
    ]


def test_where_systemd_runs_the_server_units_are_enabled_and_started(tmp_path):
    result, calls, _, _ = _sandboxed_configure(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _systemctl_calls(calls) == [
        "daemon-reload",
        *(f"{verb} {unit}" for unit in SERVER_UNITS for verb in ("enable", "start")),
        "enable kalinka.service",
        "restart kalinka.service",
        "--no-pager --full status kalinka.service",
    ]


def test_where_systemd_is_not_running_the_server_units_are_still_enabled(tmp_path):
    """A container or a chroot. An image build gets the units it boots with
    from these links, through its own systemctl."""
    result, calls, _, _ = _sandboxed_configure(tmp_path, systemd=SYSTEMD_STOPPED)

    assert result.returncode == 0, result.stderr
    assert _systemctl_calls(calls) == [
        f"enable {unit}" for unit in (*SERVER_UNITS, "kalinka.service")
    ]
    assert "kalinka.service is enabled, not started" in result.stdout


def test_the_server_configures_where_there_is_no_systemctl(tmp_path):
    result, calls, path, _ = _sandboxed_configure(tmp_path, systemd=NO_SYSTEMCTL)

    assert result.returncode == 0, result.stderr
    assert _systemctl_calls(calls) == []
    assert "no systemctl here" in result.stdout
    assert uuid.UUID(path.read_text().strip()).version == 4
    assert "Installation complete." in result.stdout


TRIGGERED = ("triggered", "kalinka-server-restart")


@pytest.mark.parametrize(
    "systemd, asked",
    [
        (SYSTEMD_RUNNING, ["try-restart kalinka.service"]),
        (SYSTEMD_STOPPED, []),
        (NO_SYSTEMCTL, []),
    ],
)
def test_plugin_trigger_restarts_only_a_running_server(systemd, asked, tmp_path):
    """A Core stopped for a package operation is started once, afterwards, by
    the restart service that stopped it, not by every plugin package's
    trigger in between. Where systemd is not running there is no Core to
    restart, and a plugin installs without a word from systemctl."""
    result, calls, _ = _postinst_sandbox(tmp_path, POSTINST, systemd, args=TRIGGERED)

    assert result.returncode == 0, result.stderr
    assert _systemctl_calls(calls) == asked
    assert result.stderr == ""


def test_plugin_trigger_leaves_a_server_taking_the_plugin_in_itself_alone(tmp_path):
    """Kalinka's own helper, working beside a running Core, holds a marker
    while apt runs: Core unloads and loads the plugin itself."""
    marker = _sandbox_root(tmp_path) / "run" / "kalinka" / "plugin-live-operation"
    marker.parent.mkdir(parents=True)
    marker.touch()

    result, calls, _ = _postinst_sandbox(
        tmp_path, POSTINST, SYSTEMD_RUNNING, args=TRIGGERED
    )

    assert result.returncode == 0, result.stderr
    assert _systemctl_calls(calls) == []
    assert "taking the plugin in itself" in result.stdout


RENDERER_ENABLED = [
    "enable kalinka-renderer.service",
    "enable kalinka-renderer-upgrade.path",
]


@pytest.mark.parametrize(
    "systemd, asked",
    [
        (
            SYSTEMD_RUNNING,
            [
                "daemon-reload",
                *RENDERER_ENABLED,
                "start kalinka-renderer-upgrade.path",
                "restart kalinka-renderer.service",
            ],
        ),
        (SYSTEMD_STOPPED, RENDERER_ENABLED),
        (NO_SYSTEMCTL, []),
    ],
)
def test_the_renderer_configures_with_or_without_systemd(systemd, asked, tmp_path):
    result, calls, _ = _postinst_sandbox(tmp_path, RENDERER_POSTINST, systemd)

    assert result.returncode == 0, result.stderr
    assert _systemctl_calls(calls) == asked


SYSTEMD_GUARD = re.compile(r"(?<!! )\[ -d /run/systemd/system \]")
MANAGER_VERBS = set(
    "daemon-reload start restart try-restart reload stop status".split()
)


def _unguarded_manager_calls(script):
    """The lines of *script* whose systemctl call fails where systemd is not
    running, and would fail the script with it.

    Such a call belongs in a branch taken only when /run/systemd/system
    exists, which is what systemd itself checks, or has to let failure pass.
    """
    guarded = []
    found = []
    for line in script.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0] == "if":
            guarded.append(bool(SYSTEMD_GUARD.search(line)))
        elif words[0] == "elif":
            guarded[-1] = bool(SYSTEMD_GUARD.search(line))
        elif words[0] == "else":
            guarded[-1] = False
        elif words[0] == "fi":
            guarded.pop()
        if "systemctl" not in words or any(guarded) or line.endswith("|| true"):
            continue
        args = words[words.index("systemctl") + 1 :]
        verb = next((arg for arg in args if not arg.startswith("-")), "")
        if verb in MANAGER_VERBS or "--now" in args:
            found.append(line.strip())
    return found


@pytest.mark.parametrize(
    "postinst", [POSTINST, RENDERER_POSTINST], ids=["server", "renderer"]
)
def test_no_postinst_needs_systemd_running_to_configure(postinst):
    assert _unguarded_manager_calls(postinst.read_text()) == []


@pytest.mark.parametrize(
    "script, found",
    [
        ("systemctl daemon-reload\n", ["systemctl daemon-reload"]),
        ("systemctl enable --now a.path\n", ["systemctl enable --now a.path"]),
        ("systemctl --no-pager status a\n", ["systemctl --no-pager status a"]),
        ("systemctl enable a\n", []),
        ("systemctl try-restart a || true\n", []),
        (
            'if [ -n "$x" ] && [ -d /run/systemd/system ]; then\n'
            "  systemctl daemon-reload\nfi\n",
            [],
        ),
        (
            "if [ -d /run/systemd/system ]; then\n  :\nelse\n"
            "  systemctl restart a\nfi\n",
            ["systemctl restart a"],
        ),
        (
            "if [ -f a ]; then\n  if [ -d /run/systemd/system ]; then\n"
            "    systemctl start a\n  fi\n  systemctl start b\nfi\n",
            ["systemctl start b"],
        ),
        (
            "if ! [ -d /run/systemd/system ]; then\n  systemctl start a\nfi\n",
            ["systemctl start a"],
        ),
    ],
)
def test_the_guard_check_finds_what_a_stopped_systemd_refuses(script, found):
    assert _unguarded_manager_calls(script) == found
