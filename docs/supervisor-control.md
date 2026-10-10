# Supervisor control page and API

`kalinka-supervisor` serves a control page and a small HTTP API on port
**8001**. From there, a browser or the app can:

- see how the box is doing;
- restart Core;
- reboot the box or power it off;
- reinstall Kalinka;
- set up an SSH administrator login through the dashboard form;
- tell which supervisor and protocol version it is talking to.

The page and API run in the supervisor process, not in Core, so they keep
working when Core is hung, failed or still starting. They read Core's
configuration and identity files, but never need Core to be running.

The supervisor runs on every image box, with or without radios. Nearby BLE
setup is one component inside it, started when an adapter and a Wi-Fi
interface are present; see [the supervisor](supervisor.md).

## The control page

Open `http://<box>:8001/` in a browser. The page is built into the supervisor
binary and loads nothing from the internet, so it works offline and when
Core's files are damaged. It follows the app's dark style.

- **Status.** Whether Core is running, starting, stopped or failed, with a
  link to Kalinka itself while it runs. Notices explain anything in the way:
  - a running upgrade or reinstall;
  - a pending shutdown;
  - a failed Core;
  - a last reinstall that failed, with its log.
- **Controls.** Restart server, Restart the box, Power off and Reinstall
  Kalinka. Each asks for confirmation first, then follows the result:
  - **Restart server:** until Core runs again.
  - **Restart the box:** until the box answers again; then it reloads.
  - **Power off:** until it stops answering.
  - **Reinstall:** shows the installer's output live, until it succeeds or
    fails.
- **Resources.** Memory and CPU for Kalinka as a whole, then per service:
  - **Services:** the server, the renderer and the supervisor, plus any other
    Kalinka unit while it runs, such as an upgrade or the Wi-Fi helper.
  - **Memory:** the proportional set size (PSS) of the processes in a unit's
    control group. Pages they share, such as those of the server's forked
    workers, count once. Where the kernel does not report PSS, resident memory
    stands in.
  - **CPU:** averaged over the last minute, as a share of the whole box.
- **This box.** Name, DietPi and Debian version, kernel, uptime, load,
  processors, temperature, nearby setup, memory and disk.
- **Software and plugins.**
  - **Packages:** every installed `kalinka*` Debian package, read from dpkg's
    database.
  - **Plugins:** every plugin in Core's Python environment, read from its
    `dist-info` metadata the way Core's own inventory finds them, without
    importing anything.

**SSH access** is a dashboard form, outside the versioned API. Enter and
confirm a new password of 12–128 printable ASCII characters, then submit
**Set password and enable SSH**. It creates `kalinka-admin` with a home,
a Bash shell and membership in `sudo`, sets its password, and enables and
starts the installed Dropbear or OpenSSH service. An already running SSH
server takes precedence. Existing accounts are kept; a pre-existing
`kalinka-admin` is updated only if it belongs to this setup flow. Repeating
the form resets that account's password. Custom SSH authentication policies
are kept, so password authentication must be allowed for this login.

The form posts to `/` and requires the dashboard's exact Origin and the
box's identity. Success redirects back to the page with connection
instructions; errors return the page with an explanation and empty password
fields. `/info` and `/v1/actions/` are unchanged. Test and unprivileged runs
simulate setup and label the result accordingly.

## Trust model

**The page and API have no authentication.** The box sits on a trusted local
network, as Core's own API already does: any LAN client can already restart
Core, upgrade it through `PUT /server/upgrade`, or change its settings. This
was a deliberate decision. What the supervisor does instead is narrow who can
reach it and what they can make it do:

- **A closed set of actions.** Unit names, targets and the installer URL are
  constants. The SSH form accepts a password for one fixed account. Nothing
  accepts a command, a path or a package name.
- **Local peers only.** The peer address must be loopback, link-local,
  private (RFC 1918) or in 100.64.0.0/10, where VPN overlays such as Tailscale
  put LAN peers. A forwarded port does not expose the API to the internet.
  Anything else gets 403 `not_local`.
- **No help for hostile web pages.** A page in the user's browser is not
  covered by the trusted-LAN premise.
  - API actions require `Content-Type: application/json`, parsed strictly, which
    forces a CORS preflight.
  - Only pages the box serves may call the API: the supervisor's own page, and
    the web player Core serves on the same host.
  - The `Host` header must be an IP address, `localhost`, a single-label name,
    or end in `.local`, `.lan`, `.home.arpa` or `.internal`. Public DNS cannot
    answer for these, so a DNS-rebinding page cannot reach the API.
  - Every response forbids framing (`frame-ancestors 'none'`, `X-Frame-Options:
    DENY`), so another site cannot overlay the controls and steal a click. A
    content security policy keeps the page to its own files.
- **The right box.** An action must repeat the box's `server_id`, so a stale
  address left by a DHCP change cannot act on a different Kalinka box. This
  is a guard, not a credential.
- **Bounded requests.** 1 KiB bodies with no unknown fields, 8 KiB headers,
  and timeouts on reading, writing and idling. Responses are never cached.
- **An audit line per action:** the path, the peer address and the outcome
  code. The body is never logged.

The SSH form grants administrator access under this same trusted-LAN model:
anyone who can reach the dashboard can set the administrator's password.
The page uses HTTP, so this password should not be reused elsewhere. Form
posts require an exact same-origin header, including the port; absent or
`null` origins are refused because ordinary HTML forms do not preflight.
The password reaches a fixed helper through a pipe, never through command
arguments, environment variables, logs or a temporary file. Only the normal
system password hash persists.

SSH setup shares the controller's operation lock and refuses requests during
package installs and network setup. `systemd-run` executes the packaged helper as a
short-lived `kalinka-ssh-setup.service`, leaving the supervisor's filesystem
sandbox intact. systemd bounds the helper's runtime to 20 seconds; the
dashboard waits up to 30 seconds. Other actions also refuse while that unit
is busy, including after a supervisor restart.

## Discovering the supervisor

`GET /info` is unversioned, and its shape never changes:

```json
{"name": "kalinka-supervisor", "version": "0.2.0",
 "protocol": 1, "min_protocol": 1,
 "server_id": "ee4d496c-3f6c-4388-a02e-f6a2f17829cd",
 "actions": ["restart_core", "reboot", "poweroff", "reinstall"]}
```

- `version` is the package version.
- `server_id` is Core's identity from `/var/lib/kalinka/server_id`, or `null`
  before Core has created it.
- `actions` lists what this supervisor can do.

**Negotiation** works like the renderer's Hello range. A client that speaks
protocols `a` to `b` uses `v = min(b, protocol)`. It is compatible when
`v >= max(a, min_protocol)`, and then calls the `/v{v}/` paths. Within a
protocol, new actions and fields only get added. A breaking change raises
`protocol`, and the older path prefix stays served for as long as
`min_protocol` allows.

A refused connection on port 8001 means the box has no supervisor, or one
older than this API. Ordinary Core installations have no supervisor.

## Endpoints

| Method | Path | Answer |
|---|---|---|
| GET, HEAD | `/` | The control page, with `/app.js`, `/style.css`, `/logo.svg` and `/icon.svg` |
| POST | `/` | Dashboard SSH form; HTML errors or a 303 redirect on success, outside the versioned API |
| GET, HEAD | `/info` | Discovery, above |
| GET, HEAD | `/v1/status` | The status object below |
| GET, HEAD | `/v1/dashboard` | The dashboard object below |
| GET, HEAD | `/v1/reinstall/log` | The last 64 KiB of the last reinstall's output, as text; empty if none has run |
| POST | `/v1/actions/restart_core` | 202 `{"action": "restart_core"}` once systemd has queued the restart |
| POST | `/v1/actions/reboot` | 202 `{"action": "reboot"}`, then the box reboots |
| POST | `/v1/actions/poweroff` | 202 `{"action": "poweroff"}`, then the box powers off |
| POST | `/v1/actions/reinstall` | 202 `{"action": "reinstall"}` once the reinstall has been queued |
| OPTIONS | any API path | CORS preflight |

`GET /v1/status` returns:

```json
{"core": "active", "core_port": 8000, "upgrading": false,
 "setup": "waiting", "pending": null,
 "reinstall": {"state": "failed", "finished_at": "2026-10-07T09:30:00Z"}}
```

| Field | Meaning |
|---|---|
| `core` | `kalinka.service`'s systemd ActiveState |
| `upgrading` | Whether an upgrade or reinstall is installing packages |
| `setup` | Nearby setup: `running`, `waiting` or `off` |
| `pending` | `reboot` or `poweroff` once accepted, otherwise `null` |
| `reinstall.state` | `running`, `succeeded`, `failed`, or `idle` if none has run |
| `reinstall.finished_at` | When the last reinstall ended, or `null` |

`GET /v1/dashboard` returns `host`, `services`, `kalinka` (the totals),
`packages`, `plugins` and `python`. A figure the box cannot provide is `null`;
for example, CPU stays `null` until two samples a few seconds apart exist.
Memory is in bytes and CPU in percent of the whole box.

An action's body is `{"server_id": "<id from /info>"}`, or `{}` while
`/info` reports `null`:

```sh
curl -H 'Content-Type: application/json' \
  -d '{"server_id": "ee4d496c-3f6c-4388-a02e-f6a2f17829cd"}' \
  http://192.168.1.50:8001/v1/actions/restart_core
```

**`restart_core`** resets a failed `kalinka.service`, then restarts it. The
reset recovers a Core that systemd gave up on after too many failed starts.
It is a plain restart. Unlike Core's `PUT /server/restart`, it does not ask
plugins for packages to install; installs that are already queued still run
in `bootstrap.sh`. The app keeps using Core's endpoint to apply settings, and
uses this one when Core does not answer.

**`reboot` and `poweroff`** reply first and act second. The supervisor checks
that systemd answers and knows the target. It then writes the 202 with
`Connection: close` and flushes it before it starts the shutdown, so the
reply reaches the client before the network goes down. These two actions are
terminal: until the box goes down, every later action is answered
`shutting_down`. If systemd refuses after the reply, which is unlikely, the
audit line says `failed` and `pending` returns to `null`.

**`reinstall`** starts `kalinka-reinstall.service` and returns once it is
queued. That oneshot unit ships with the supervisor package, not with Core,
so it works when Core's own files are what is broken:

1. It fetches the published installer from `https://kalinkaplayer.com/install.sh`.
2. It runs it with `KALINKA_REINSTALL=1`, which turns the installer from an
   upgrade into a repair:
   - Every Kalinka package, the renderer included, is installed again at the
     release's version: `apt-get --reinstall`, which restores their files and
     reruns their maintainer scripts.
   - The server is stopped and its Python environment removed. The server's
     postinst starts it again, and `bootstrap.sh` rebuilds the environment
     from the shipped wheels. If the install fails, the installer still
     starts the server on its way out, so the box is never left with it
     stopped.
   - Settings, the library and the music are kept.
   - Optional extras that were installed into the environment come back the
     next time the server is restarted from the app.
   - The supervisor itself is upgraded only when a newer one exists, as on
     any upgrade.
3. Its output goes to `/var/log/kalinka-supervisor/reinstall.log`. When it
   ends, the unit writes its result to
   `/var/lib/kalinka-supervisor/reinstall-result`. systemd forgets a oneshot's
   result once it unloads the unit; this file is what `reinstall.state`
   reports.

The reinstall runs as long as it needs; apt and source builds on a Pi have no
safe bound. While it runs, `upgrading` is true and every other action waits.

## Refusals

Every error has Core's shape, `{"detail": {"code": "...", "message": "..."}}`:

| Status | Code | When |
|---|---|---|
| 400 | `invalid_request` | The body is not a JSON object with only `server_id`, or is over 1 KiB |
| 403 | `not_local` | The peer is not on a local network |
| 403 | `host_not_allowed` | The `Host` name could be a DNS-rebinding page's |
| 403 | `origin_not_allowed` | A page this box did not serve |
| 404 | `not_found` | An unknown path or action |
| 405 | `method_not_allowed` | The wrong method; `Allow` names the right one |
| 409 | `wrong_server` | `server_id` is missing, or does not name this box |
| 409 | `busy` | Another action is still being carried out |
| 409 | `shutting_down` | A reboot or power-off has been accepted |
| 409 | `upgrade_in_progress` | `kalinka-reinstall.service`, `kalinka-upgrade.service` or `kalinka-renderer-upgrade.service` is running, stopping or queued |
| 409 | `network_change_in_progress` | Reboot, power-off or reinstall while nearby setup is joining a network or rolling one back |
| 415 | `unsupported_media_type` | An action without `Content-Type: application/json` |
| 429 | `too_soon` | A `restart_core` within 15 s of one that succeeded; `Retry-After` says when |
| 500 | `log_unreadable` | The reinstall log exists but could not be read |
| 503 | `unavailable` | systemd did not answer, or refused |

The upgrade refusal protects package installs. The upgrade and reinstall
units run apt or pip with no timeout, and cutting them off can leave a broken
installation. A wedged one needs SSH or a power cycle. Core itself starting
does **not** block a reboot or a reinstall, because a `bootstrap.sh` stuck
installing packages is one of the main reasons to use either.

Reboot, power-off and reinstall wait for a Wi-Fi join or rollback to finish.
A restart of Core does not, as it leaves the network alone.

## Where it listens

The page and API listen where Core does, on their own port, and follow Core's
configuration file. The file is reread when it changes, checked every two
seconds:

- **Interface.** `base_config.server.interface` set to `all` (the default)
  binds `0.0.0.0:8001`. A named interface binds that interface's IPv4 address.
  Until the interface has one, the API waits rather than failing.
- **Port.** If Core is configured to use the API's own port, the API
  releases it and waits until Core moves. The API must never stop Core from
  starting. `--listen-port` in the unit changes the API's port.
- **A broken file.** If Core's configuration is missing or unreadable, the
  API follows Core's defaults.

`systemctl status kalinka-supervisor` shows the current binding and the state
of nearby setup.

## Turning it off

Set `KALINKA_CONTROL_API=0` in `/etc/default/kalinka-supervisor` or in
`/boot/dietpi.txt`, then restart the supervisor. That turns off the page as
well. Nearby setup has its own switch, `KALINKA_BLE_SETUP=0`, and the
supervisor exits cleanly when both are off. A firewall must allow TCP 8001
from the LAN for the page to be reachable.

## Using it from the app

The Kalinka app shows the box under **Settings › General › Box**:
- **Box dashboard** opens this page in the browser. That covers the
  dashboard and reinstalling, so the app has no copy of either.
- **Restart the box** and **Power off** run through the app's own
  confirmation dialogs.

When Core stops answering, the app's "unavailable" card and the Settings
placeholder offer **Restart server** through the supervisor.

None of this appears for a demo server, for a server reached over HTTPS, or
for a box whose supervisor does not match the server. Another client should
follow the same steps:

1. **Find the box.** Probe `http://<core-host>:8001/info`. Use the box only if:
   - it answers as `kalinka-supervisor`;
   - its protocol range overlaps the client's;
   - its `server_id` is Core's own, which Core reports at
     `GET /renderer/sessions`.

   The app records that id under the server's address on every connect.
   While Core is down, it can then still tell its own box from another one
   that has taken over the address.
2. **Act.** Send the action with that `server_id`.
3. **Wait for the result:**
   - **`restart_core`:** first check `/v1/status`, and leave a Core that is
     `activating` to finish starting: on a Pi that takes up to a minute.
     After the restart, poll `/v1/status` until `core` is `active`, then
     reconnect to Core.
   - **`reboot`:** wait for `/info` to answer again.
   - **`reinstall`:** poll `/v1/status` until `reinstall.state` leaves
     `running`, showing `/v1/reinstall/log` meanwhile.
   - **`poweroff`:** there is nothing to wait for.
4. **Handle refusals.** Treat 409 and 429 as "not now": show the reason and
   keep the button. A 503 means systemd refused, so offer the action again.

The web player runs on Core's port. A request from it to port 8001 is
cross-origin, and the API allows exactly that origin.

## Extending it

New privileged operations, such as restoring a known-good release or
installing a single package, go through the same `Controller` in
`internal/control`. It admits one operation at a time, refuses during
installs, and coordinates with nearby setup. Adding one to `actions` keeps
protocol 1. An operation that has to run inside the supervisor rather than in
a unit of its own would need the unit's `ProtectSystem=strict` relaxed, which
belongs in that change's threat model.

## Testing

- **Unit tests.** `make supervisor-test` covers:
  - the policy and the HTTP layer;
  - the page and its security headers;
  - SSH form validation, origin checks, action serialization, secret handling and the helper with simulated account/service tools;
  - the host and origin rules;
  - the systemd calls against a fake bus;
  - the reinstall record and unit;
  - the dashboard against a staged box;
  - the binding, and the composed process serving and stopping.

  The installer's reinstall mode is covered in
  `packages/kalinka-server/tests/test_maintainer_scripts.py`.
- **Host check.** `KALINKA_TEST_HOST_SYSTEMD=1 go test ./internal/control`
  reads unit state from the host's real systemd, and never acts on it.
- **Laptop run.** `run-test.sh` serves the page with simulated actions. Any
  unprivileged run simulates them too, because only root may drive systemd.
  `run-live.sh` turns the page and API off, since as root they would act on the
  laptop.
- **VM.** The PC image's VM boot test:
  1. loads the page and reads `/info`;
  2. powers the guest off through `POST /v1/actions/poweroff`.

  This proves the supervisor can read Core's identity and drive systemd on a
  real image. The inspection step then finds the audit line in the guest's
  journal.
