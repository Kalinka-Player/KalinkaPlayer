# Development

How to build the Debian packages and run the server from a source checkout.

## Debian Package
Deb packages are provided in the [Releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases) section. The whole app bundle (server, plugins, SDK) is pure Python and arch-independent (`_all.deb`); only the renderer ships per-arch builds, from its own `kalinka-renderer-v*` releases.

### Building Debian packages
The build produces **separate** `.deb` packages — one for the server and one per plugin — and collects them in the top-level `debs/` directory. All of them are `Architecture: all` and install on any machine.

#### Prerequisites
Install the required system dependencies:
```bash
sudo apt install python3 python3-venv python3-pip make git dpkg-dev
```
Python 3.11+ is required (production runs 3.13). The server packages are pure Python; only the renderer needs a C++ toolchain (`make renderer-deb`, see `packages/kalinka-renderer/scripts/build_deb.sh` for its dependencies).

#### Build process
Clone the repository and build — there's no virtualenv to set up by hand, the build provisions its own:
```bash
git clone https://github.com/Kalinka-Player/KalinkaPlayer.git
cd KalinkaPlayer
make build-all-deb
```
On first run `make build-all-deb` creates a `.venv` with the wheel-build toolchain (or reuses an already-active `$VIRTUAL_ENV`), builds the server and every plugin, and moves the artifacts into `debs/`. To build against a specific interpreter, pass it explicitly: `make build-all-deb PYTHON=/path/to/python3.13`.

You can also build pieces individually: `make kalinka-server-deb`, `make kalinka-plugins-deb`, or `make renderer-deb`; `make build-env` just provisions the venv without building anything. Run `make help` to list all targets.

The app bundle — the server and the first-party plugins — shares one version, derived from a single `kalinka-vX.Y.Z` git tag via setuptools-scm (one tag per release). The plugin SDK is versioned independently by its own SemVer; plugins pin its major version (currently `>=3,<4`), so backwards-compatible minor/patch SDK bumps don't break them — only a major bump is breaking. See [RELEASING.md](../RELEASING.md) for the full release and version-bump procedure.

#### Cleaning build artifacts
```bash
make clean
```
This removes compiled objects, shared libraries and Python build artifacts.

#### Installation
Install the server first, then the plugins you want:
```bash
sudo dpkg -i debs/kalinka-server_*.deb
sudo dpkg -i debs/kalinka-plugin-*.deb
sudo apt install -f   # install any missing dependencies
```
The renderer is not part of that bundle — build and install it separately (`make renderer-deb`, then `sudo apt install ./packages/kalinka-renderer/kalinka-renderer-*.deb`), or take a published one with `./scripts/install-renderer.sh`, if this machine should play audio itself. The server runs as the `kalusr` system user and the renderer as `kalrndr`, the only one of the two in the `audio` group.

At startup `kalinka.service` runs `/opt/kalinka/bootstrap.sh`, which creates `/opt/kalinka/venv` and pip-installs every wheel found under `/opt/kalinka/wheels/`. Plugins ship their wheel there and trigger a server restart, so they're picked up automatically (see [`docs/plugin-deb-packaging.md`](plugin-deb-packaging.md)).

#### Service management
- Restart: `sudo systemctl restart kalinka.service`
- Check status: `systemctl status kalinka.service`
- View logs: `journalctl -u kalinka.service`

# Running from source (development)

`make dev-setup` + `make dev-run` get you a running server from a source checkout — no root, no systemd, and nothing written to the system's `/etc` or `/var`. Everything lives in a per-user "fakeroot" under `$KALINKA_PREFIX` (default `~/kalinka`), and the editable installs mean Python edits are picked up on the next restart.

The dev venv needs **Python 3.11+** (production runs 3.13). `make dev-setup` creates the venv with `python3` and refuses to proceed against anything older than 3.11.

1. Clone the repo and install the system prerequisites (including Python 3.11+):
```bash
git clone https://github.com/Kalinka-Player/KalinkaPlayer.git
cd KalinkaPlayer
sudo apt install python3 python3-venv
```
   To use a specific interpreter, pass it explicitly: `make dev-setup PYTHON=/path/to/python3.13`.
   To also build and run the renderer locally (audio playback), see `make renderer-build` — that one needs the C++ toolchain (`g++ cmake pkg-config protobuf-compiler libprotobuf-dev libboost-dev libcurlpp-dev libcurl4-openssl-dev libflac++-dev libasound2-dev libspdlog-dev libfmt-dev`).
2. One-step setup. Creates a virtualenv at `.venv` with `python3` (or **reuses an already-active `$VIRTUAL_ENV`** — it never makes a second venv), installs the SDK, server and all bundled plugins editable, and seeds the fakeroot directory tree. It writes no config: the server starts on its defaults and keeps only the settings you change:
```bash
make dev-setup
```
3. Run the server in the foreground (Ctrl-C to stop):
```bash
make dev-run
```
   It prints where everything lives and tees output to a log file, so you have logs to grep instead of only stdout:
   - config:        `~/kalinka/etc/kalinka/kalinka_conf.cfg`
   - state & DB:     `~/kalinka/var/lib/kalinka/`
   - logs:          `~/kalinka/var/log/kalinka/server.log`
   - music drop-off: `~/kalinka/srv/kalinka/music`

   Forward server flags with `ARGS` (e.g. `make dev-run ARGS=--debug`), and relocate the whole tree with `make dev-run KALINKA_PREFIX=/path/to/root`.

   Logs here carry the full `date time LEVEL thread name: message` format. Under systemd both the server and the renderer switch to the terser format journald expects — no timestamp or level of their own, since journald records those itself — and `KALINKA_LOG_FORMAT=journal make dev-run` shows you that format from a source checkout (`full` forces the other direction). It is not auto-detected here because `dev-run` pipes output through `tee`.
4. **Restart to pick up changes.** Python edits go live on restart — either click **Restart** in the app (this works without systemd: `dev-run` watches the restart trigger in the fakeroot and relaunches) or Ctrl-C and re-run `make dev-run`. After editing renderer C++, rebuild with `make renderer-build` and restart the renderer binary.
   Enabling an optional feature (Smart Search) in **Settings** and hitting **Restart** also just works: `dev-run` installs the requested optional packages into the venv before relaunching — the same flow `kalinka.service` runs at boot in production.
5. Open the Kalinka app: the server appears in its list under the name you configured. Pick it and tap **Connect**, or use **Enter Address Manually** with `<host>:8000`.

# Testing

```bash
make test
```
Runs the SDK, server and plugin test suites, each from its own package directory, and the retrieval benchmark's own tests. The server's play queue suite is slow and flaky, so it runs on its own with `make test-playqueue`. The renderer has its own C++ test set under `packages/kalinka-renderer/tests` (built when GoogleTest is installed). Every pull request runs the same targets in CI.

Two larger checks are opt-in because they are slow and need the outside world. `make system-test` runs the full-stack indexing test (a server of its own, a Samba share in podman, real enrichment and embedding) — see [tests/system/README.md](../tests/system/README.md). `make bench-sdd` measures what semantic search actually retrieves: it indexes the 706 recordings of the Song Describer Dataset through the shipped pipeline and scores the `/ai_search` endpoint against the 1106 human captions that came with them, reporting retrieval quality and a per-stage timing breakdown — see [benchmarks/sdd/RUNBOOK.md](../benchmarks/sdd/RUNBOOK.md). That directory also holds `device_probe.py`, a single file to copy onto a Raspberry Pi (or any target) to find out what one track costs it to embed and where those seconds go; the reference measurement is [benchmarks/sdd/results_device.md](../benchmarks/sdd/results_device.md).

