# Install test

`make install-test` runs `scripts/install-release.sh` from this checkout on throwaway Debian 13 systems, one container each, against the latest published `kalinka-v*` release, the way [option D of the installation guide](../../docs/installation.md#d-install-with-one-command) has its reader run it.

| System | What it runs |
|---|---|
| `debian`: a fresh `debian:trixie`, which installs recommends, with the `ca-certificates`, `curl` and `python3` the guide asks for | Installs the release before the latest, asked for by its version, then runs the script again with none, which upgrades it in place to the latest and keeps the server's id. Then puts the systemd package's `systemctl` back and runs this checkout's server and renderer postinsts' `configure`. |
| `dietpi`: the same image with DietPi's apt settings (no recommends, no suggests), its base system's `curl` and CA certificates, and what the guide asks for | Installs the latest with `KALINKA_RENDERER=0 KALINKA_WEB=0` and an `apt-get` that fails its first install of the downloaded packages, so the `dpkg -i` + `apt-get -f install` fallback has to leave a working install. Then runs the script again piped into `bash`, as `curl … \| bash` does, with the renderer installer out of reach: that only warns, and the browser player comes this time. Last, it takes `systemctl` away and runs this checkout's server postinst's `configure`. |

After each run the script has to have succeeded and named the release it installed. The release's packages have to be installed at its version with nothing left half-configured, the browser player and the renderer installed or not as the switches say, and `fpcalc` there for AcoustID: the script asks for it by name, since it takes no recommends. Where a run installed the server, the server is then started as `kalinka.service` starts it and has to answer `/server/version` with the release's version.

It is opt-in because it is slow and needs the outside world: podman or docker, and network access to GitHub's releases, the Debian mirrors, and PyPI and piwheels for the server's first start. Without podman or docker the target says so and runs nothing. Without a token GitHub's API allows an address 60 calls an hour, and a system makes up to seven, so a few runs in a row use the hour up: each system first asks how many calls are left and, if too few, says so and waits for the limit to reset. Set `GITHUB_TOKEN` to raise the limit; it is passed into the containers, as are `KALINKA_REPO` and `KALINKA_WEB_REPO` to test against a fork's releases.

```
make install-test
```

Each system's whole output lands in `tmp/install-test/<system>.log` (`INSTALL_TEST_LOGS` puts it elsewhere). A failure ends with `FAIL <step>: <what>`, and one where the server does not answer carries the tail of its output.

No systemd runs in the containers. `policy-rc.d` keeps maintainer scripts from starting anything. While the published packages install, `/usr/bin/systemctl` is `systemctl-recorder`, diverted so that the systemd package bluez brings in installs its own beside it: a released postinst may still need a `systemctl` that answers everything. The recorder notes what the packages asked for, and the cases check that the server's and the renderer's postinst asked for their service to be enabled. The server is started by hand from its installed unit: the directories systemd would make, `ExecStartPre` as root, `ExecStart` as its user, but without the unit's sandboxing.

The last step of each system runs this checkout's postinsts, which `run.sh` mounts beside the scripts, as dpkg would run them, where systemd is not running: on `debian` with the systemd package's own `systemctl`, after which `kalinka.service` and `kalinka-renderer.service` have to be linked into `multi-user.target.wants`, and on `dietpi` with no `systemctl` at all. Either way `configure` has to succeed.

| File | Runs |
|---|---|
| `run.sh` | On the host: one container per system, and the logs |
| `system.sh` | In the container: sets the system up, then hands over to `cases.py` |
| `cases.py` | In the container: each system's steps and checks |
| `apt-get-failing-once` | In the container, as `/usr/local/sbin/apt-get` for the step that forces the fallback |
| `systemctl-recorder` | In the container, as `/usr/bin/systemctl` until the last step |

`scripts/tests/test_install_test.py` checks the target, `run.sh`, the stand-ins and the helpers in `cases.py` without a container, with the other installer tests.
