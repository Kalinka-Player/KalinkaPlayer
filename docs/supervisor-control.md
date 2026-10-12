# Supervisor control page and API

`kalinka-supervisor` serves a control page and a small HTTP API on port
**8001**. From there, a browser or the app can:

- see how the box is doing;
- restart Core;
- reboot the box or power it off;
- reinstall Kalinka;
- remove a plugin package, so a plugin that breaks Core can go without Core's help;
- on a Raspberry Pi, set the box up for its sound card, such as a DAC HAT;
- open nearby setup, to move the box to another Wi-Fi network;
- set up an SSH administrator login through the dashboard form;
- tell which supervisor and protocol version it is talking to.

The page and API run in the supervisor process, not in Core, so they keep
working when Core is hung, failed or still starting. They read Core's
configuration and identity files, but never need Core to be running.

The supervisor runs on every image box, with or without radios. Nearby BLE
setup is one component inside it, started when an adapter and a Wi-Fi
interface are present; see [the supervisor](supervisor.md). A box that runs
only a renderer can have one too, installed with the renderer; it then offers
no server controls.

## The control page

Open `http://<box>:8001/` in a browser. The page is built into the supervisor
binary and loads nothing from the internet, so it works offline and when
Core's files are damaged. It follows the app's dark style.

- **Status.** Whether Core is running, starting, stopped or failed, with a
  link to Kalinka itself while it runs. Notices explain anything in the way:
  - a running upgrade or reinstall;
  - a pending shutdown;
  - a failed Core;
  - a last reinstall that failed, with its log;
  - a plugin removal under way, or a last one that failed, with its log.
- **Controls.** Restart server, Restart the box, Power off and Reinstall
  Kalinka. Each asks for confirmation first, then follows the result:
  - **Restart server:** until Core runs again.
  - **Restart the box:** until the box answers again; then it reloads.
  - **Power off:** until it stops answering.
  - **Reinstall:** shows the installer's output live, until it succeeds or
    fails.
- **Sound card**, on a Raspberry Pi. The DAC the box found, what the boot
  configuration sets up, and the ALSA card that came up. A list of every card
  the box can be set up for, with the ones it detected first, and **Apply**,
  which restarts the box once when the boot configuration has to change. See
  [the sound card](#the-sound-card).
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
  - **Plugins:** every installed `kalinka-plugin-*` Debian package other than the SDK, each with a **Remove** button, followed by any plugin in Core's Python environment that no package accounts for, read from its `dist-info` metadata the way Core's own inventory finds them, without importing anything. Remove asks for confirmation, then follows the removal with its output until it succeeds or fails, and Core starts again either way. There is no install here: installing is Core's job, from the app, and the supervisor only takes away what is already on the box.

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
  constants. The SSH form accepts a password for one fixed account, and
  `set_soundcard` accepts one sound card name from the supervisor's own
  catalog, which the box must offer: its overlay has to be on the boot
  partition. Nothing accepts a command or a path. The one package name
  accepted, by `remove_plugin`, must be an installed `kalinka-plugin-*`
  package other than the SDK, and the removal unit checks it again and
  refuses to take any other package along.
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
- **The right box.** An action must repeat the box's `server_id`, or its
  `renderer_id` on a box that runs only a renderer, so a stale address left
  by a DHCP change cannot act on a different Kalinka box. This is a guard,
  not a credential.
- **Bounded requests.** 1 KiB bodies with no unknown fields, 8 KiB headers,
  and timeouts on reading, writing and idling. Responses are never cached.
- **Hardware stays read-only to the supervisor.** It reads the device tree,
  ALSA's card list and the boot partition, and reads a few registers on the
  HAT's I2C bus. Every change to the boot configuration runs in a helper
  unit outside its sandbox.
- **An audit line per action:** the path, the peer address and the outcome
  code. The body is never logged.
- **Nearby setup on demand.** `wifi_setup` opens the BLE setup that a box
  otherwise offers only while it is offline. Its pairing is Just Works, which
  does not prove who is pairing, so while it is open anyone within Bluetooth
  range could move the box to a network of their own. It closes after 10
  minutes, and asking for it takes the same LAN access that can already
  reboot or reinstall the box. `KALINKA_BLE_SETUP=0` turns it off along with
  the rest of nearby setup.

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
 "renderer_id": "5c0ffee0-1d2e-4a3b-8c4d-5e6f7a8b9c0d",
 "actions": ["restart_core", "reboot", "poweroff", "reinstall", "set_soundcard", "remove_plugin",
             "wifi_setup"],
 "poweroff_timer": true}
```

- `version` is the package version.
- `poweroff_timer` advertises support for Core's inactivity timer. Older
  supervisors omit it; apps hide the timer controls on those boxes.
- `server_id` is Core's identity from `/var/lib/kalinka/server_id`, or `null`
  before Core has created it.
- `renderer_id` is the renderer's identity from
  `/var/lib/kalinka-renderer/renderer_id`, or `null` where no renderer has
  created one. It is the `renderer_id` Core lists the renderer under.
- `actions` lists what this box can do. A box without Core offers no
  `restart_core` or `reinstall`, and only a Raspberry Pi offers
  `set_soundcard`.

**Negotiation** works like the renderer's Hello range. A client that speaks
protocols `a` to `b` uses `v = min(b, protocol)`. It is compatible when
`v >= max(a, min_protocol)`, and then calls the `/v{v}/` paths. Within a
protocol, new actions and fields only get added. A breaking change raises
`protocol`, and the older path prefix stays served for as long as
`min_protocol` allows.

A refused connection on port 8001 means the box has no supervisor, or one
older than this API. Ordinary Core installations have no supervisor.

## Endpoints

### Inactivity power-off

Core persists `base_config.server.box_power_off_enabled` (default `false`)
and `base_config.server.box_power_off_minutes` (whole minutes, 1–1440,
default 30). KalinkaAI shows the switch and duration beside **Power off
streamer** in Management, only for a matching supervisor advertising timer
support and a Core exposing these settings. Changes use the usual Apply flow.

Core measures continuous inactivity with a monotonic clock. Paused, stopped
and empty playback count as idle; playing or buffering resets the clock.
An error or failed playback read resets it too. Events include playback
owned by input plugins. Changing the timer settings or restarting Core starts
a full new interval. Demo mode never authorizes automatic shutdown.

Supervisor polls Core's `GET /server/power-off-timer` every five seconds on
Core's configured local interface and port. The uncached response is
`{"server_id":"<Core identity>","due":false}`. A fresh `due: true` for the
identity stored on this box requests the same guarded power-off operation as
the manual action. Upgrades, reinstall, plugin removal, setup and other busy
operations still block shutdown. Core is checked again after these guards,
so resuming playback during admission cancels the request. A later attempt
requires another fresh decision. An absent, older, unreachable or unrecognized
Core never triggers power-off. Core exposes only the decision; it never
invokes systemd.

### Control API

| Method | Path | Answer |
|---|---|---|
| GET, HEAD | `/` | The control page, with `/app.js`, `/style.css`, `/logo.svg` and `/icon.svg` |
| POST | `/` | Dashboard SSH form; HTML errors or a 303 redirect on success, outside the versioned API |
| GET, HEAD | `/info` | Discovery, above |
| GET, HEAD | `/v1/status` | The status object below |
| GET, HEAD | `/v1/dashboard` | The dashboard object below |
| GET, HEAD | `/v1/reinstall/log` | The last 64 KiB of the last reinstall's output, as text; empty if none has run |
| GET, HEAD | `/v1/plugin-removal/log` | The same for the last plugin removal |
| GET, HEAD | `/v1/soundcard` | The sound card object below |
| POST | `/v1/actions/restart_core` | 202 `{"action": "restart_core"}` once systemd has queued the restart |
| POST | `/v1/actions/reboot` | 202 `{"action": "reboot"}`, then the box reboots |
| POST | `/v1/actions/poweroff` | 202 `{"action": "poweroff"}`, then the box powers off |
| POST | `/v1/actions/reinstall` | 202 `{"action": "reinstall"}` once the reinstall has been queued |
| POST | `/v1/actions/remove_plugin` | 202 `{"action": "remove_plugin", "package": "..."}` once the removal has been queued; the body also names the `package` |
| POST | `/v1/actions/set_soundcard` | 202 `{"action": "set_soundcard", "reboot": true}` once the card is set up; when `reboot` is true, the box then restarts |
| POST | `/v1/actions/wifi_setup` | 202 `{"action": "wifi_setup"}` once nearby setup is open |
| OPTIONS | any API path | CORS preflight |

`GET /v1/status` returns:

```json
{"core": "active", "core_port": 8000, "upgrading": false,
 "setup": "waiting", "pending": null,
 "reinstall": {"state": "failed", "finished_at": "2026-10-07T09:30:00Z"},
 "plugin_removal": {"state": "succeeded", "package": "kalinka-plugin-qobuz",
                    "finished_at": "2026-10-12T15:02:00Z"},
 "wifi_setup": {"available": false, "window_seconds": 600}}
```

| Field | Meaning |
|---|---|
| `core` | `kalinka.service`'s systemd ActiveState |
| `upgrading` | Whether an upgrade or reinstall is installing packages |
| `setup` | Nearby setup: `running`, `waiting` or `off` |
| `pending` | `reboot` or `poweroff` once accepted, otherwise `null` |
| `reinstall.state` | `running`, `succeeded`, `failed`, or `idle` if none has run |
| `reinstall.finished_at` | When the last reinstall ended, or `null` |
| `plugin_removal.state` | The same for the last plugin removal |
| `plugin_removal.package` | The package the last removal was asked for, or `null` |
| `plugin_removal.finished_at` | When the last removal ended, or `null` |
| `wifi_setup.available` | Whether `wifi_setup` can reach nearby setup now. It is `false` while setup is off, recovering an interrupted join, or bringing its radio up, even when `setup` is `running` |
| `wifi_setup.window_seconds` | How long `wifi_setup` keeps nearby setup open |

`GET /v1/dashboard` returns `host`, `services`, `kalinka` (the totals),
`packages`, `plugins` and `python`. A figure the box cannot provide is `null`;
for example, CPU stays `null` until two samples a few seconds apart exist.
Memory is in bytes and CPU in percent of the whole box.

An action's body names the box. On a box with Core it is
`{"server_id": "<id from /info>"}`, and may add the `renderer_id` too. On a
box that runs only a renderer it is `{"renderer_id": "<id from /info>"}`.
While `/info` reports neither, it is `{}`. `remove_plugin` adds `"package"`,
and no other action accepts it:

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

**`remove_plugin`** writes the package name to `/var/lib/kalinka-supervisor/plugin-removal-request`, a file only root reads, and starts `kalinka-plugin-removal.service`, which ships with the supervisor package. The unit's script checks the name again, that dpkg lists the package as installed, and that apt's simulation would remove nothing else, since removing a package the server depends on would take the server with it. It then stops Core, runs `apt-get remove`, and starts Core again with `--no-block`, failure or not, so the box is never left silent. The plugin's own prerm uninstalls it from Core's environment; its settings in Core's configuration stay. Output goes to `/var/log/kalinka-supervisor/plugin-removal.log` and the result to `/var/lib/kalinka-supervisor/plugin-removal-result`, which `plugin_removal` reports. While the unit runs, `upgrading` is true and every other action waits. The unit does not wait for a Wi-Fi join, as a restart of Core does not. A box without Core offers no `remove_plugin`.

**`wifi_setup`** opens [nearby setup](ble-provisioning.md) for 10 minutes
(`wifi_setup.window_seconds`), even while the box is online, so the app on a
phone near it can connect the box to another Wi-Fi network. Nothing is
forgotten: the box stays on its current network until a phone starts a join,
its Wi-Fi leaves that network while it tries the new one, and a join that
fails rolls back to it. When the 10 minutes are over, an online box stops
advertising. A scan, join or handoff already underway finishes first; a phone
that is connected but has not started one loses setup. Asking again starts
the 10 minutes over.

The action needs nearby setup running with its radio up, which takes a
Bluetooth adapter, a Wi-Fi interface and `KALINKA_BLE_SETUP` left on;
`wifi_setup.available` says when that is. If setup restarts during the 10
minutes, for instance after a Bluetooth error, it opens again for what is
left of them. It is how a box whose old network still works, or one kept
online by an Ethernet cable, changes networks without SSH. A box that runs
only a renderer offers it too.

## The sound card

A Raspberry Pi plays through a DAC HAT once its firmware loads the HAT's
device-tree overlay, named by a `dtoverlay=` line in
`/boot/firmware/config.txt`. The firmware also loads it from the HAT's ID
chip: from the chip itself on Raspberry Pi's own DACs, or from the
firmware's table of boards, which lists most HiFiBerry boards. Any other HAT
needs that line, which the supervisor writes for the card chosen in the app
or on the page.

`GET /v1/soundcard` returns what the box knows:

```json
{"supported": true, "name": "HiFiBerry Digi2 Pro",
 "hat": {"vendor": "HiFiBerry", "product": "Digi2 Pro", "product_id": "0x0000",
         "product_ver": "0x0000", "uuid": "5af941bb-4dcf-4eac-82a8-e36e84fcabef"},
 "configured": "hifiberry-digi-pro", "configured_by": "hat",
 "active": {"id": "sndrpihifiberry", "name": "snd_rpi_hifiberry_digi",
            "pcm": "hw:CARD=sndrpihifiberry,DEV=0"},
 "usb": [], "suggested": [],
 "options": [{"name": "hifiberry-digi-pro", "label": "HiFiBerry Digi+ Pro or Digi2 Pro",
              "vendor": "HiFiBerry", "kind": "digital"}]}
```

| Field | Meaning |
|---|---|
| `supported` | `false` off a Raspberry Pi, or without `/boot/firmware/config.txt`; every other field is then empty |
| `name` | The DAC as a person would call it: from the HAT's ID chip when that HAT is the sound card, else the configured card's label, else ALSA's card name; `null` when none is known. Another board's chip, such as a PoE or M.2 HAT's, never names it |
| `hat` | What the HAT's ID chip says, from `/proc/device-tree/hat`; `null` without one |
| `configured` | The catalogued card the boot configuration sets up, else the one the firmware sets up from the HAT's ID chip; `null` for neither |
| `configured_by` | `boot_config` or `hat`, what sets `configured` up; `null` when nothing is configured |
| `active` | The ALSA card that came up for it: `id`, `name` and `pcm`, the name the renderer opens it by |
| `usb` | The USB audio cards plugged in, in the same shape. They play with nothing set up, so no choice here concerns them; the output's own settings pick one |
| `suggested` | The cards the hardware points to, most likely first |
| `options` | Every card this box can be set up for: `name`, `label`, `vendor` and `kind` (`dac`, `digital`, `amp`, `onboard` or `none`) |

**Detection** reads, in order:

1. **ALSA's card list.** The first card that is not the Pi's own output, USB
   or virtual is the HAT's. For the Pi's own outputs, `active` is the output
   chosen.
2. **The boot configuration.** The first catalogued overlay in `config.txt`.
   On DietPi, `CONFIG_SOUNDCARD` in `/boot/dietpi.txt` tells the Pi's two own
   outputs apart.
3. **The HAT's ID chip.** The firmware sets a board up from its chip when
   its table, `overlays/hat_map.dtb`, lists the board's UUID, or its vendor
   and product, and the overlay the table names is on the card. That card is
   then `configured` by `hat`, and nothing is suggested. Raspberry Pi's and
   IQaudIO's chips name their overlay themselves, so nothing is suggested
   either. For a HiFiBerry board the table does not list, as on older
   firmware, the supervisor maps the product to its overlay, as HiFiBerry's
   own tools do.
4. **The I2C bus**, only while no card came up and no chip settled it. The
   supervisor reads a few registers on `/dev/i2c-1`, as HiFiBerry's own
   detection does. A chip that answers names the DAC chip, not the board, so
   the suggestion lists every catalogued board built on it. The images
   switch the bus on; elsewhere it may be off, and the full list remains.

**`set_soundcard`** takes `{"server_id": "…", "soundcard": "<name>"}`, with
a name from `options`. The supervisor runs
`/usr/lib/kalinka-supervisor/soundcard.sh apply <name> [<hat-card>]` as the
transient `kalinka-soundcard-apply.service`, outside its sandbox, and waits
for it, for at most 90 seconds. `<hat-card>` is the card the firmware sets up
from the HAT's ID chip, when there is one:

- **On DietPi**, the cards DietPi knows go through its own
  `dietpi-set_hardware soundcard`, which also writes `CONFIG_SOUNDCARD`.
- **Elsewhere, and for cards DietPi does not know** such as `i2s-dac`, the
  helper edits `config.txt` itself: it removes every audio overlay it could
  have written, switches the Pi's own audio off, and adds the card's
  `dtoverlay=` line where every board reads it.
- **The card the firmware sets up from the HAT's ID chip** is left to the
  chip: the helper removes any audio overlay and `force_eeprom_read=0` that
  would load a second card, and touches nothing else. Any other card on such
  a box adds `force_eeprom_read=0`, so the chip's own overlay does not load
  on top. Without such a chip, that line is added for the boards HiFiBerry's
  tools treat so, and removed for any other card.
- `none` removes the HAT's overlay. It is never handed to DietPi, whose
  handling of it removes ALSA.

The reply says whether the boot configuration changed. When it did, the box
restarts after the reply, exactly as for `reboot`: `pending` is `reboot`
meanwhile. The firmware loads the overlay at that boot, and the card appears
in `active` and in the renderer's list of output devices.

At boot on a Pi running DietPi, `kalinka-soundcard.service` runs the same helper for the
card `CONFIG_SOUNDCARD` names on the card, before the renderer starts, and
restarts once if that changed the boot configuration. While it runs, every
action is refused as `busy`. While it still waits for the network, only
`set_soundcard` is, since the two would contend for the helper.

When `force_eeprom_read=0` hides the HAT's ID chip, the helper remembers
which card the chip set up. Choosing that card again hands it back to the
chip rather than writing an overlay that would load a second copy.

## Refusals

Every error has Core's shape, `{"detail": {"code": "...", "message": "..."}}`:

| Status | Code | When |
|---|---|---|
| 400 | `invalid_request` | The body is not a JSON object with only the box's identity and, for `set_soundcard`, a `soundcard` name, or is over 1 KiB |
| 403 | `not_local` | The peer is not on a local network |
| 403 | `host_not_allowed` | The `Host` name could be a DNS-rebinding page's |
| 403 | `origin_not_allowed` | A page this box did not serve |
| 404 | `not_found` | An unknown path, or an action this box does not offer |
| 405 | `method_not_allowed` | The wrong method; `Allow` names the right one |
| 409 | `wrong_server` | `server_id` or `renderer_id` is missing, or does not name this box |
| 409 | `busy` | Another action, the SSH helper or a sound card helper is still running, or, for `set_soundcard`, the boot hook is still waiting to apply its card |
| 409 | `shutting_down` | A reboot or power-off has been accepted |
| 409 | `upgrade_in_progress` | `kalinka-reinstall.service`, `kalinka-plugin-removal.service`, `kalinka-upgrade.service` or `kalinka-renderer-upgrade.service` is running, stopping or queued |
| 400 | `invalid_package` | `remove_plugin` named something other than a `kalinka-plugin-*` package, or named the SDK |
| 409 | `not_installed` | `remove_plugin` named a package dpkg does not list as installed |
| 409 | `network_change_in_progress` | Reboot, power-off, reinstall, `set_soundcard` or `wifi_setup` while nearby setup is joining a network or rolling one back |
| 409 | `setup_unavailable` | `wifi_setup` while `wifi_setup.available` is `false`: nearby setup is turned off, lacks a Bluetooth adapter or Wi-Fi interface, or is starting or restarting |
| 409 | `setup_in_use` | `wifi_setup` while a phone is scanning or handing off through nearby setup |
| 415 | `unsupported_media_type` | An action without `Content-Type: application/json` |
| 422 | `unknown_soundcard` | A sound card this box does not offer |
| 429 | `too_soon` | A `restart_core` within 15 s of one that succeeded; `Retry-After` says when |
| 500 | `log_unreadable` | The reinstall or removal log exists but could not be read |
| 500 | `soundcard_failed` | The sound card helper failed; the supervisor's journal has its output |
| 503 | `unavailable` | systemd did not answer, or refused |

The upgrade refusal protects package installs. The upgrade and reinstall
units run apt or pip with no timeout, and cutting them off can leave a broken
installation. A wedged one needs SSH or a power cycle. Core itself starting
does **not** block a reboot or a reinstall, because a `bootstrap.sh` stuck
installing packages is one of the main reasons to use either.

Reboot, power-off, reinstall, a sound card change and `wifi_setup` wait for a
Wi-Fi join or rollback to finish. A restart of Core does not, as it leaves the
network alone.

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
- **Sound card**, on a Raspberry Pi, shows the DAC's name and opens a picker
  with the detected cards first. After the restart a change needs, the app
  points the box's own renderer at the new card.

An output's own settings show the same **Sound card** row for a box that
runs only a renderer and has a supervisor.

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

   **A renderer's box.** For a renderer whose `local` is true, the box is
   Core's own. For any other, `GET /renderer/list` gives its `address`, the
   IPv4 address it connected from. Probe `http://<address>:8001/info`, and
   use the box only if its `renderer_id` is the renderer's own.
2. **Act.** Send the action with that `server_id`, or the `renderer_id` on a
   box without Core.
3. **Wait for the result:**
   - **`restart_core`:** first check `/v1/status`, and leave a Core that is
     `activating` to finish starting: on a Pi that takes up to a minute.
     After the restart, poll `/v1/status` until `core` is `active`, then
     reconnect to Core.
   - **`reboot`:** wait for `/info` to answer again.
   - **`reinstall`:** poll `/v1/status` until `reinstall.state` leaves
     `running`, showing `/v1/reinstall/log` meanwhile.
   - **`remove_plugin`:** the same with `plugin_removal.state` and
     `/v1/plugin-removal/log`, then wait for `core` to be `active` again.
   - **`poweroff`:** there is nothing to wait for.
   - **`set_soundcard`:** the reply arrives once the card is set up. When
     its `reboot` is true, wait as for `reboot`, then poll `/v1/soundcard`
     until `active` is set. Point the box's renderer at `active.pcm` by
     setting its `output.device` through Core's
     `PUT /renderer/{id}/config`.
   - **`wifi_setup`:** offer it only while `/v1/status` reports
     `wifi_setup.available`. There is nothing to wait for: send the user to
     **Set up a box** in the app, on a phone near the box, within
     `wifi_setup.window_seconds`.
4. **Handle refusals.** Treat 409 and 429 as "not now": show the reason and
   keep the button. A 503 means systemd refused, so offer the action again.
   A 422 means the box cannot take that sound card; read `/v1/soundcard`
   again.

The web player runs on Core's port. A request from it to port 8001 is
cross-origin, and the API allows exactly that origin.

## Extending it

New privileged operations, such as restoring a known-good release, go
through the same `Controller` in `internal/control`. It admits one operation
at a time, refuses during installs, and coordinates with nearby setup. Adding
one to `actions` keeps protocol 1, as `remove_plugin` did. An operation that
takes a parameter follows that one's pattern: a narrow shape checked in the
controller, handed to a unit of its own through a root-only file, and checked
again by the unit's script. An operation that has to run inside the supervisor rather than in
a unit of its own would need the unit's `ProtectSystem=strict` relaxed, which
belongs in that change's threat model.

## Testing

- **Unit tests.** `make supervisor-test` covers:
  - the policy and the HTTP layer;
  - the page and its security headers;
  - SSH form validation, origin checks, action serialization, secret handling and the helper with simulated account/service tools;
  - plugin removal: the package rules, the request file, the unit and its script against simulated apt, dpkg and systemctl;
  - the host and origin rules;
  - the systemd calls against a fake bus;
  - the reinstall record and unit;
  - the sound card: the catalog against the overlays the images ship,
    detection against staged boxes and a fake I2C bus, and the packaged
    helper setting up every catalogued card so that detection reads it back;
  - the dashboard against a staged box;
  - the binding, and the composed process serving and stopping.

  The installer's reinstall mode is covered in
  `packages/kalinka-server/tests/test_maintainer_scripts.py`. The sound card
  helper's boot and apply modes, with DietPi's tool stubbed, are covered by
  `packages/kalinka-supervisor/tests/test_soundcard.sh`, which
  `make supervisor-test` runs too.
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
