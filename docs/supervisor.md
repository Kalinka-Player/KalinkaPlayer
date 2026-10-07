# Kalinka Supervisor

`kalinka-supervisor` replaces the experimental Python provisioning service.
It provides nearby box setup and a LAN [control page and API](supervisor-control.md)
on port 8001. They show how the box is doing, and restart Core, reboot or power
off the box, or reinstall Kalinka. It runs independently
of Core, its Python interpreter, its virtual environment and its plugins.
Core still owns its HTTP API, mDNS, its identity and the existing OOBE. Nearby
setup needs no app protocol change; see
[the BLE contract and test flow](ble-provisioning.md).

## Build and install

The source is in `packages/kalinka-supervisor`. Go 1.25 or newer is required
at build time. Runtime needs a Linux kernel, BlueZ, system D-Bus and the chosen
network backend; neither Go nor Python is needed on the target.

```sh
make supervisor-test
make supervisor-build
packages/kalinka-supervisor/build/kalinka-supervisor --version
GOARCH=arm64 packages/kalinka-supervisor/build.sh /tmp/kalinka-supervisor-arm64
make supervisor-deb
```

`GO=/path/to/go` selects a toolchain. `VERSION=0.1.0` sets the binary/package
version. The Debian package is written under the module's `build/` directory.
It depends on BlueZ and networking tools, with `network-manager | ifupdown` as
alternatives. An existing DietPi ifupdown installation satisfies that dependency;
installing the supervisor does not require migrating DietPi to NetworkManager.
Generic ifupdown hosts are not supported: outside DietPi, install
`network-manager` explicitly even if ifupdown satisfies apt's alternative.
Without the selected backend's executable, nearby setup stays off and the
journal says what to install; the rest of the supervisor keeps running, and
setup starts by itself once the backend appears.
The DietPi backend also requires DietPi's own `dietpi-network apply --no-restart`
API. The package enables and starts the service. Pi image builds additionally
enable UART Bluetooth, firmware and the radio settings needed for first boot.

All image targets install an architecture-specific Debian package containing the same static Go service. `--backend auto`
selects DietPi when `/boot/dietpi/dietpi-network` exists, otherwise NetworkManager.
Use `--backend dietpi` or `--backend nm` to select explicitly. The service runs
on every box, radios or not. Nearby setup is a component inside it. It starts
while the adapter (`--adapter`, default `hci0`) and a wireless interface exist,
and stops when either goes away. This is checked every two seconds, so a radio
that appears after boot needs no restart. A setup failure restarts only that
component, after 10 seconds. `KALINKA_BLE_SETUP=0` turns setup off and
`KALINKA_CONTROL_API=0` turns the control API off. Both are read from the
environment or `/boot/dietpi.txt`, and with both off the service exits.

## Current boundaries

| Package | Responsibility |
|---|---|
| `internal/protocol` | Existing v1 validation, framing, status encoding and redacted credentials |
| `internal/machine` | Setup ownership, cancellation, progress, offline policy and handoff |
| `internal/wifi` | Native NetworkManager D-Bus and DietPi supplicant/ifupdown transactions |
| `internal/gatt` | BlueZ exports, pairing policy and advertisement lifecycle |
| `internal/system` | Private directories, process lock, systemd readiness and watchdog |
| `internal/provision` | Nearby setup as one restartable component: its radio gate, recovery and BLE loop |
| `internal/control` | The control page and API: action policy, systemd operations, reinstall record, HTTP layer, embedded page and listener binding |
| `internal/dashboard` | What runs on the box and what it costs, from /proc, cgroups, dpkg and Core's environment; never from Core itself |
| `internal/coreconf` | Core's interface, port and identity, read from its files without Core running |
| `internal/dbusx` | The system-bus call surface shared by NetworkManager and systemd |
| `cmd/kalinka-supervisor` | Options, component supervision and process lifecycle |

The machine depends on a small networking interface, not D-Bus or shell tools.
Backend failures return fixed reason codes. Cancellation does not open a new
attempt until the previous backend has finished rollback. NetworkManager stages
a new profile and persists it only after DHCP; DietPi snapshots its fixed set
of configuration files and restores them on failure or interrupted startup.
The old Python DietPi rollback directory is recovered before accepting commands.

State lives in `/var/lib/kalinka-supervisor`, runtime sockets/lock in
`/run/kalinka-supervisor`, both private. Non-root laptop runs use the user's
configuration and runtime directories. `--state-dir` and `--runtime-dir` can
isolate a test. Retain the state directory between restarts: it holds rollback
records. Do not run multiple receivers with different state directories on the
same adapter/interface.

## Hardening applied

- Static `CGO_ENABLED=0` binaries; locked Go modules with `go.sum`; no runtime
  dependency installation or imports from Core.
- `Secret` redacts formatting, structured logs and JSON/text serialization.
  Tests cover formatting the containing command as well. Password buffers are
  cleared when possible. Go strings/D-Bus marshaling can retain copies until
  garbage collection; this is not a guaranteed memory-erasure boundary.
- Credentials travel over encrypted GATT and local OS IPC, never subprocess
  arguments. Supplicant receives a derived WPA2 key through its Unix socket.
  NetworkManager receives settings through D-Bus. Raw command/backend errors
  and profile settings are not logged.
- Only BlueZ's current unique D-Bus owner may call GATT read/write and pairing
  methods. Property enumeration exposes a change marker, not credential data.
  Commands are bounded to 512 bytes, partial writes expire, and one phone owns
  each operation/result. Long reads use bounded per-reader snapshots.
- OS calls, scans, joins and rollback have deadlines. Subprocess cancellation
  kills the process group. Setup recovers pending transactions before BLE
  becomes available. The process restarts a failed setup component itself;
  systemd restarts the process and watches the setup loop, whose stall stops
  the watchdog ping.
- The control page and API answer local peers only, cannot be framed by other
  sites, and offer a closed set of typed actions;
  [their trust model](supervisor-control.md#trust-model) lists the rest.
- Reinstall runs in its own unit, `kalinka-reinstall.service`, which the
  supervisor package ships with its script. It never runs a script from Core's
  installation, which may be what is broken, and it is not confined by the
  supervisor's sandbox, as installing packages needs the whole system.
- The root unit uses `ProtectSystem=strict`, private temporary storage,
  `ProtectHome`, `NoNewPrivileges`, restricted address families and explicit
  writable networking/state paths. `/run` and DietPi's configuration paths must
  remain writable for ifupdown and DietPi's own tool. The unit sets
  `G_CHECK_ROOTFS_RW_VERIFIED=1` because DietPi's global mount check would reject
  this intentionally read-only root; actual file writes still fail closed.
  DHCP/supplicant start through `kalinka-wifi@.service`, outside the
  supervisor's process group and mount sandbox, and survive its normal restart.
  This helper runs native `ifup --force`/`ifdown --force`, accepting both `auto`
  and `allow-hotplug` configurations. It conflicts with the native `ifup@` unit;
  rollback stops both before restoring files. Normal boot still uses DietPi's
  existing networking configuration. This is a privileged
  network service, not a sandbox against a compromised root/kernel.

BLE Just Works encryption does not prove ownership or prevent active
man-in-the-middle attacks. This iteration authorizes nearby setup while offline;
it does not authorize future destructive recovery. Disabling setup through
`KALINKA_BLE_SETUP=0` still works in DietPi configuration or the unit environment.

## Package updates today

Core's existing Python update checker watches `kalinka-supervisor-v*` alongside Core and renderer releases. A newer supervisor alone makes the existing update action available. The root-side upgrade script still runs `install-release.sh`; that calls `install-supervisor.sh` only when the supervisor is already installed. Ordinary Core installations do not acquire a supervisor implicitly.

The helper chooses the matching `amd64` or `arm64` package, verifies its release checksum and Debian package identity, refuses downgrades, then lets apt install it. The package's post-install hook reloads systemd, re-enables the unit and restarts the supervisor, which also starts it on boxes where older packages left it off for lack of radios. Neither step can fail the Core upgrade that installs the package. Persistent setup rollback records and Wi-Fi daemons survive that restart. A checksum fetched from the same HTTPS release detects corruption; it is not an independent publisher signature.

Package releases remain cached when the feed rotates, but renderer/supervisor
update offers require a known Core bundle target for the existing upgrade API.
A cold feed containing only package releases therefore offers no upgrade until
a bundle target is known.

Supervisor releases use their own tags and workflow; image builds consume the package job's artifact. See [image builds](../packages/kalinka-image/README.md#ci-and-e2e-artifacts). The update-check integration ships with the next Core release; images built with an older published Core need that Core update before supervisor-only notifications appear.

## Becoming a supervisor

The supervisor runs on every box, with nearby setup as one component and the
control page and API as another. Keep these additions behind interfaces
separate from the provisioning machine:

1. A health collector checks link/address, Core's systemd state and HTTP health,
   disk space and recent fixed-category failures. It should distinguish missing
   network, failed Core and an unreachable external service. A failure opens a
   diagnostic state, not an automatic reinstall loop.
2. A recovery policy exposes a small list of typed actions: retry networking,
   restart Core, restore a known-good release, and repair Core's environment.
   No endpoint accepts arbitrary commands, paths or package URLs. The control
   API's `Controller` already admits one action at a time, refuses during
   installs, keeps reboot, power-off and reinstall off a Wi-Fi join or
   rollback, and audits outcomes without secrets. Restarting Core, rebooting,
   powering off and an online reinstall are delivered; the remaining actions
   join it.
3. The embedded control page and its dashboard keep working when Core is
   absent. BLE can advertise an additional recovery capability without
   changing the existing Wi-Fi UUIDs. The page's actions are served without
   authentication: the box is on a trusted LAN, a decision recorded with
   [its trust model](supervisor-control.md#trust-model). Each further action
   that can destroy data restates that decision in its own threat model before
   it ships.
4. Today's reinstall repairs Core online: it fetches the published release and
   rebuilds Core's environment from its wheels, so it needs the network and a
   working system Python. Offline repair uses an independently stored, verified
   release bundle and Python runtime when interpreter recovery is required. Build a replacement
   environment alongside the current one, verify it, switch atomically, health
   check and roll back. A wheel cache alone cannot repair a broken interpreter.
   Supervisor must never execute its own code from Core's environment.
5. Keep the existing scripts responsible for updates until a separate release-checking implementation is justified. A later Go release source can implement the same release-family/version interface without coupling it to BLE or health collection. The unprivileged Core service cannot write the supervisor executable; the existing authorized root upgrade unit can replace its Debian package.
6. Before adding release checks to Go, support offline recovery from one retained known-good Core build. Stage verified packages, wheels and any required Python runtime outside Core's venv. Promote a candidate only after a sustained healthy period **and actual successful use**, such as playback; process startup alone is not enough. Keep the previous known-good build until that confirmation, make promotion atomic, and never overwrite it with a failed update. A recovery request restores that exact retained build without checking GitHub or upgrading packages. Bound retained storage and version the manifest so future update ownership can change without losing recovery data.
7. Updating the supervisor itself remains package-managed. A future signed-release updater needs its own rollback and startup-health mechanism; a broken supervisor cannot restore itself by executing its broken binary. systemd or an independent installer must retain that responsibility.

Beyond the control page's four actions, no offline release restore, local
admin socket or self-updater exists yet. These need their own threat model and
failure-injection tests before they gain root actions.

Bluetooth playback stays in a separate component. This service unregisters
only its own GATT application, advertisement and agent; it never shuts down
BlueZ or powers off the adapter. A future pairing coordinator should arbitrate
the shared default agent and adapter alias when audio pairing is introduced.
BLE setup and Classic Bluetooth A2DP can share hardware while using separate
connections/profiles; neither belongs in the other's lifecycle.

## Verification and limits

`make supervisor-test` runs the race detector and `go vet`. Tests cover captured
Python protocol outputs, credential redaction, phone ownership across disconnect,
rollback before retry, persistence recovery, DietPi control operations,
NetworkManager profile staging, the setup component's gate and restarts, and
the control API (see [its testing notes](supervisor-control.md#testing)).
`tests/capture_reference.py` documents how the frozen Python fixture was
produced; Python is not required to run the Go tests.

Local `run-test.sh` uses real encrypted BLE and simulated Wi-Fi and control
actions. `run-live.sh` uses real NetworkManager and changes the laptop network,
with the control API off. The read-only host NetworkManager smoke test is
opt-in, documented in the BLE guide.

A passing recorder test or ARM64 cross-build does not validate a DietPi radio,
DHCP hooks or the unit's writable-path allowlist on a running image. Before
shipping, exercise the actual DietPi service on a Pi, including power loss,
wrong credentials, failed DHCP, saved-network preservation and reboot recovery.
A VM with USB Wi-Fi and Bluetooth passed through can cover much of this but
cannot validate the Pi's UART Bluetooth firmware and shared radio behavior.

The DietPi integration follows the upstream [network tool](https://github.com/MichaIng/DietPi/blob/master/dietpi/dietpi-network)
and [global filesystem check](https://github.com/MichaIng/DietPi/blob/master/dietpi/func/dietpi-globals).
