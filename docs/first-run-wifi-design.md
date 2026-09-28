# First-run Wi-Fi — design

A Kalinka image gets onto Wi-Fi only if someone edits a file on its card before the first boot. That works for a person who flashes their own card and knows where to look. It does not work for a box sold ready-made, which comes with no card the buyer is meant to edit, no keyboard and no screen. It also fails on the PC image, whose settings partition is an EFI partition that desktops hide. The app cannot help either: its setup wizard starts by finding the server on the network, which is the very thing that is missing.

This document compares three ways out and picks two of them. Raspberry Pi Imager's own customisation is honoured on the Pi images now (§4). Bluetooth LE provisioning from the app is built for boxes nobody flashes (§5, §6). The hotspot with a captive page is not built (§7). Nothing here touches the audio path, the REST/WebSocket API, the renderer protocol or the plugin SDK.

Status: proposed, 2026-09-28. The decision stands on facts that were read from the code and from Debian 13's package indexes. Facts about DietPi, about Imager's output and about the radios could not be checked without the hardware. §10 lists them, and the first issue of the plan (§12) settles them before anything is built. Every memory and boot-time figure below is an estimate until that issue replaces it with a measurement.

## 1. Where things stand

### 1.1 How a box gets online today

| Image | Wi-Fi comes from | Who can do it |
|---|---|---|
| `rpi234`, `rpi5` (DietPi) | `AUTO_SETUP_NET_WIFI_ENABLED=1` in `dietpi.txt` and the network in `dietpi-wifi.txt`, on the FAT partition, before the first boot. DietPi's first boot applies them and removes the files. | Someone who can mount the card and edit two files. |
| `amd64` (Debian) | `kalinka-firstboot.conf` on the FAT partition, applied by [firstboot.sh](../packages/kalinka-image/overlays/debootstrap/usr/lib/kalinka-image/firstboot.sh) through NetworkManager, then shredded. | Someone who can mount a hidden EFI partition. The [installation guide](installation.md#on-the-pc-image) needs a page of `diskpart` and `diskutil` for it. |

Raspberry Pi Imager offers to set Wi-Fi, a user and SSH, and our images ignore all of it. The guide tells people to skip that screen ([installation.md](installation.md#2-write-it-to-the-card), step 4), even though it is where anyone who has set up Raspberry Pi OS types their Wi-Fi.

A Pi Zero 2 W has no Ethernet port. For the smallest supported board, "use a cable" means a USB adapter.

In the app, the wizard ([onboarding_screen.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/screens/onboarding_screen.dart)) starts at step 0, the `DiscoveryScreen`, which finds servers by mDNS or takes an address by hand. A box that is not on the network never appears there. The app ships for Android, Windows, Linux and the browser. It does not ship for iOS.

### 1.2 What there is to build on

- DietPi's first boot already turns `dietpi.txt` and `dietpi-wifi.txt` into a working Wi-Fi setup. Writing DietPi's own inputs reuses that path, and nothing has to replace it.
- [dietpi-conf.sh](../packages/kalinka-image/overlays/dietpi/usr/lib/kalinka-image/dietpi-conf.sh) reads and writes `dietpi.txt` the way DietPi does. [kalinka-soundcard.service](../packages/kalinka-image/overlays/dietpi/usr/lib/systemd/system/kalinka-soundcard.service) already drives a DietPi tool (`dietpi-set_hardware`) from a Kalinka unit.
- `firstboot.sh` has `configure_wifi` for NetworkManager and `create_account` for a sudo account with a hash or a key. [test_firstboot.sh](../packages/kalinka-image/tests/test_firstboot.sh) shows how to test such code with recorders in place of the real tools.
- The server listens on every address by default (`interface: all`). It also re-reads its interfaces every 10 seconds and announces a new address over mDNS ([service_discovery.py](../packages/kalinka-server/src/kalinka_server/service_discovery.py)). A box that joins Wi-Fi after the server started is therefore reachable and discoverable within seconds, with no restart.
- The server's identity is `/var/lib/kalinka/server_id`, the same value its mDNS record carries as `server_id`.
- The app already picks a platform implementation with a conditional import: `discovery_provider.dart` chooses between `discovery_notifier_io.dart` and a web stub.

## 2. Decisions

### 2.1 Honour Raspberry Pi Imager on the Pi images now

People already type their Wi-Fi into Imager. Applying it costs no new package, since `python3` and its `tomllib` are already on the image. It also removes the one instruction in the guide that is there only because we fall short. This ships as its own small change (issue 2), ahead of everything else.

### 2.2 Build Bluetooth LE provisioning for boxes nobody flashes

The box advertises over Bluetooth LE while it has no network. The app finds it, the phone pairs with it, and the box receives the Wi-Fi network and its password over the encrypted link. The box joins and reports its address, and the app carries on into the wizard. This is the only path that works for a ready-made box with no card to edit and no screen or keyboard to use. It asks the buyer only for the app and their Wi-Fi password.

### 2.3 Do not build the hotspot

A temporary access point with a captive page works from any phone, which is its one real advantage. It costs a privileged helper, a second web UI, packages the build refuses today, and a join flow that drops the phone halfway through. §7 gives the full argument.

### 2.4 Provisioning belongs to the image, not the server

Writing network configuration is a root job. The server runs as `kalusr` with `NoNewPrivileges=yes`, alongside third-party plugins, and it must stay unable to do that job. Provisioning is an appliance concern, like growing the root filesystem or applying a sound card. It lives in `packages/kalinka-image` and ships only on the images. A one-command install on someone's own machine has a keyboard and a network already.

### 2.5 Proximity is the authorisation; the link is encrypted

Anyone within Bluetooth range of an unprovisioned box, during the window after it starts, can put it on a network. Every commercial speaker makes the same trade. The window is short (§5.3) and it can be switched off (§5.7). It opens only on a box that finds no network at boot. That includes a provisioned box whose router starts more slowly than the box after a power cut, until the router's route arrives. The password itself travels only over a link that the phone and the box have paired and encrypted (§5.5).

### 2.6 What this leaves out, plainly

- **iPhone owners.** The app does not ship for iOS, so they have no Bluetooth path. With this design they can still use a network cable, Imager or the card files. On a Pi Zero 2 W a cable needs a USB adapter. An iOS build of the app would close the gap: `universal_ble` supports iOS, and the box side needs no change. That is the largest adoption cost left standing, and it is a limit of the app's platforms, not of this design.
- **Enterprise Wi-Fi** (802.1X, "sign in with a user name"). The network list shows such networks but they cannot be chosen, and they need a cable or a login. Home networks are WPA2/WPA3-Personal or open, and those are covered.
- **The same network.** The hand-off needs the phone on the network it just gave the box. Guest networks with client isolation already stop discovery today. The app says so when it happens (§6.3) rather than failing without a word.

## 3. The three paths compared

Disk sizes are Debian 13's `Installed-Size` for amd64. arm64 sizes differ slightly. Memory and boot times are estimates for a Pi Zero 2 W, the smallest board, and issue 1 measures them.

| | A. Imager customisation | B. Bluetooth LE from the app | C. Hotspot and captive page |
|---|---|---|---|
| Serves | People who flash their own Pi card with Imager | Anyone with the Android app, or a Linux or Windows desktop with Bluetooth, including buyers of a ready-made box | Any phone with a browser |
| New on the Pi images | Nothing | `bluez`, `python3-dbus-fast`, `pi-bluetooth` and Raspberry Pi's `bluez-firmware`; Bluetooth and the Wi-Fi modules switched on in DietPi | `hostapd`, `dnsmasq-base` (refused by the build today), `nftables` for the port-80 redirect, a root helper and a page |
| New on the PC image | Not offered | `bluez`, `python3-dbus-fast`. Intel and Realtek Bluetooth firmware is already in `firmware-iwlwifi` and `firmware-realtek`. | `hostapd`, `dnsmasq-base`, `nftables` |
| New in the app | Nothing | `universal_ble`; Android Bluetooth permissions | Nothing |
| Disk | A few kilobytes of scripts | About 9 MB: `bluez` 4.8, `python3-dbus-fast` 3.2, and the Sphinx JavaScript it hard-depends on, 1.1 (`libjs-sphinxdoc`, `libjs-jquery`, `libjs-underscore`). Up to 2 MB more for `libdw1t64` and `libelf1t64` where missing. GLib, which BlueZ needs, is already there through ffmpeg's `librsvg2-2`. | About 5 MB: `hostapd` 2.3, `dnsmasq-base` 1.1, `libnftables1` 1.1 and small netfilter libraries |
| Memory while working (estimate) | One short Python process at first boot | 18–23 MB: `bluetoothd` about 3, the daemon 15–20 | About 5 MB for `hostapd` and `dnsmasq`, plus whatever serves the page |
| Memory once on the network | None | None. Both processes exit; the kernel's Bluetooth modules stay loaded (under 1 MB). | None |
| Boot cost, box already on a network | None: the unit's condition is false | One shell check off the critical path; the Bluetooth firmware loads in parallel (1–2 s) | The same shell check |
| Boot cost, box with no network | 1–2 s before DietPi's first boot, first boot only | `bluetoothd` and the daemon take 2–3 s to start, off the critical path | The access point takes about 5 s to come up |
| When it goes wrong | The file stays on the card and the box boots without Wi-Fi. With path B on the image, the box then advertises for setup. | A wrong password is retried on the same screen. A box not found is switched off and on for a new window, or given a cable or the card files. | The phone leaves the hotspot mid-join, and a wrong password shows only once the user rejoins the hotspot |
| Platforms | Wherever Imager runs | Android now; Linux and Windows desktops with an adapter; iPhone once the app ships there | Any |

None of the three costs anything once the box is on its network. What separates them is who each one reaches, and what goes wrong while it runs.

## 4. Path A: Imager customisation

### 4.1 What Imager leaves on the card

Imager has written customisation in two forms. One is `custom.toml` on the FAT partition, which Raspberry Pi OS reads at its first boot. The other is `firstrun.sh`, with `systemd.run=` and `systemd.unit=kernel-command-line.target` added to `cmdline.txt`. The script calls `/usr/lib/raspberrypi-sys-mods/imager_custom` and `/usr/lib/userconf-pi/userconf` when they exist, and otherwise writes Raspberry Pi OS's files itself. DietPi reads neither form. Imager's output has changed between releases, so issue 1 records exactly what the current release writes for a **custom** image. It also records what today's image does with that output. If `systemd.run=` names a path that does not exist on DietPi, the first boot stops in `kernel-command-line.target`, and the guide's "skip it" is then keeping users from a box that never comes up.

### 4.2 One applier, two front-ends

`imager-custom.sh` in the DietPi overlay is the only code that applies anything. It writes only DietPi's own inputs: keys in the card's `dietpi.txt`, set with `dietpi_conf_set`, and the network in `dietpi-wifi.txt`. DietPi's first boot then applies them as if a person had edited the files. The applier also creates the login account with `create_account`, which moves from `firstboot.sh` into `overlays/common/usr/lib/kalinka-image/account.sh` so that both bases share one copy.

Two front-ends feed it, so it does not matter which form Imager wrote:

- **`custom.toml`**: `imager-toml.py` parses the file with the standard library's `tomllib` on the system interpreter and checks every value. It passes the values to the applier as NUL-separated pairs on a pipe, never on a command line, because any local user can read another process's arguments. `kalinka-imager-custom.service` runs it (§4.3).
- **`firstrun.sh`**: the two programs the script looks for, at the paths where it looks for them. Each one calls the applier. `userconf` creates the named account instead of renaming DietPi's `dietpi` user, which DietPi's own tools expect. Where `firstrun.sh` finds `imager_custom`, it writes no `authorized_keys` itself. It passes the keys as `imager_custom enable_ssh -k KEY…`, before `userconf` runs. `enable_ssh` therefore installs them for UID 1000, DietPi's `dietpi`, where the script would otherwise have written them. `userconf` then moves them over to the new account. Issue 1 confirms the order against the current script. The build fails if any package owns either path (`dpkg -S`), so a later `raspberrypi-sys-mods` cannot overwrite them without anyone noticing.

| Imager setting | `custom.toml` | `firstrun.sh` | Applied as |
|---|---|---|---|
| Host name | `system.hostname` | `imager_custom set_hostname` | `AUTO_SETUP_NET_HOSTNAME` |
| User and password | `user.name`, `user.password` (a crypt hash when `password_encrypted`) | `userconf NAME HASH` | A sudo account with that hash; `root` and `dietpi` stay locked |
| SSH keys | `ssh.authorized_keys` | `imager_custom enable_ssh -k` | The account's `authorized_keys` |
| SSH off | `ssh.enabled = false` | Only `enable_ssh` exists | `AUTO_SETUP_SSH_SERVER_INDEX=0`; otherwise Dropbear stays on, as DietPi ships it |
| Wi-Fi | `wlan.ssid`, `password`, `password_encrypted`, `hidden`, `country` | `imager_custom set_wlan` | `AUTO_SETUP_NET_WIFI_ENABLED=1`, `AUTO_SETUP_NET_WIFI_COUNTRY_CODE`, the network in `dietpi-wifi.txt` |
| Time zone, keyboard | `locale.timezone`, `locale.keymap` | `set_timezone`, `set_keymap` | `AUTO_SETUP_TIMEZONE`, `AUTO_SETUP_KEYBOARD_LAYOUT` |

Values go into `dietpi-wifi.txt` escaped for the shell, because DietPi sources that file. An SSID or a passphrase that contains a `'` must still arrive intact.

### 4.3 Order against DietPi's first boot

`kalinka-imager-custom.service` is a oneshot with `ConditionPathExists=/boot/firmware/custom.toml`. It runs after the boot partition is mounted and before `dietpi-preboot.service` and `dietpi-firstboot.service`, with `DefaultDependencies=no` so the ordering forms no cycle. Issue 2 confirms where it sits with `systemd-analyze verify`. It applies only while DietPi has never booted (`/boot/dietpi/.install_stage` is `-1`, which the build already checks), because the `AUTO_SETUP_*` keys mean nothing after that. A `custom.toml` found later is logged as not applied, and shredded. The `firstrun.sh` path runs earlier still, in its own boot that ends in a reboot. Either way, the applier finishes before DietPi's first boot reads its files.

### 4.4 Secrets

Once `custom.toml` has been applied it is shredded, as `kalinka-firstboot.conf` is, because FAT has no permissions to protect it with. A file that cannot be parsed is left in place. That matches the PC image, where a file still on the card after boot tells the user it was not read. With `password_encrypted`, Imager writes the Wi-Fi key as 64 hex digits, the key derived from the passphrase. Whether `dietpi-wifidb` accepts such a key is open (§10). If DietPi's first boot also takes the network from where DietPi keeps it on the root filesystem, the applier writes it there, and the key never touches the FAT. Otherwise it goes into `dietpi-wifi.txt`, which DietPi removes after the import.

### 4.5 Fallback and documentation

If nothing applies, the box boots without Wi-Fi, exactly as it does today. On an image that also carries path B, it then advertises for setup, so a failure in path A leads into path B. The card files stay documented and unchanged. The guide's Imager step changes from "skip it" to "set your Wi-Fi, host name and a user here", and the image README and the release-notes template change with it.

## 5. Path B: Bluetooth LE provisioning, the box

### 5.1 The Wi-Fi backend

`overlays/common/usr/lib/kalinka-image/wifi.sh` does four things, the same on both images: `scan`, `join`, `status` and `forget`. The base records which backend it uses as `KALINKA_WIFI_BACKEND` in `/etc/default/kalinka-image`. `firstboot.sh` already reads that file, and the DietPi base starts writing one too.

- `scan` prints one network per line, tab-separated: signal in dBm, security (`open`, `psk`, `sae` or `eap`), and the SSID as hex octets. SSIDs are 32 arbitrary bytes, and hex keeps tabs and newlines out of the parse.
- `join SSID_HEX SECURITY COUNTRY HIDDEN` reads the passphrase from standard input, never from `argv`. `SECURITY` is what the daemon's last scan reported for that SSID. A hidden network appears in no scan, so for one the daemon passes `psk` when the phone sent a passphrase and `open` when it did not. On success it prints the IPv4 address and exits 0. Otherwise its exit status names the reason, 1 to 5 in this order: `wrong_password`, `not_found`, `no_address`, `timeout`, `error`. The whole join is bounded at 45 seconds.
- `status` prints the network and address the box is on, or nothing.
- `forget SSID_HEX` removes a network the backend stored. The daemon uses it to take back a network whose join failed, so a wrong password does not stay behind and keep failing.

The **`nm`** backend, for the PC image, scans with `nmcli --terse device wifi list --rescan yes` and converts NetworkManager's 0–100 signal back to dBm on NetworkManager's own linear scale. It joins by writing a keyfile, then runs `nmcli --wait 45 connection up`. NetworkManager's state reason gives the failure reason. `configure_wifi` moves here and `firstboot.sh` sources `wifi.sh`, but the function as it stands cannot serve a join from the phone, and it is generalised on the way:

- `key-mgmt` follows `SECURITY`: `wpa-psk` for `psk`, `sae` for `sae`, and no `[wifi-security]` section for `open`. Today it always writes `wpa-psk`, which fails on a WPA3-only network and on an open one.
- A hidden network gets `hidden=true`.
- The SSID is written as a keyfile byte list (`ssid=83;116;117;…;`), since it arrives as hex and may hold any byte.
- The passphrase is escaped for GKeyFile: `\` as `\\`, and a leading space as `\s`. Written raw, as today, a passphrase with a backslash or a leading space reaches NetworkManager changed and fails as a wrong password.

The **`dietpi`** backend, for the Pi images, drives DietPi's tools the way `kalinka-soundcard.service` does. It sets the country with `dietpi-set_hardware wificountrycode`, adds the network to DietPi's Wi-Fi database and applies it with `dietpi-wifidb`. It then watches `wpa_supplicant` through `wpa_cli` for the result of the four-way handshake, and `wlan0` for a DHCP lease. Issue 1 pins down the exact calls.

The passphrase is passed through as typed, and the box derives nothing from it. A pre-hashed key would shut out WPA3-SAE networks, which need the passphrase itself.

### 5.2 The provisioning daemon

`kalinka-provision` is a small asyncio program on the system interpreter, using Debian's `python3-dbus-fast` (2.44.1 in trixie main) to talk to BlueZ. Its source lives in `packages/kalinka-image/src/kalinka_provision/`. That puts it inside the default scope of [check_credential_logging.py](../scripts/check_credential_logging.py) (`packages/*/src`). The build copies it to `/usr/lib/kalinka-image/`.

| Module | Job | Imports D-Bus |
|---|---|---|
| `protocol.py` | The JSON codec of §5.4: the size cap, validation, unknown fields ignored | No |
| `machine.py` | The state machine: phases, the one-phone lock, the timers, with a clock and a Wi-Fi runner passed in | No |
| `wifi.py` | Runs `wifi.sh` as a subprocess, with the passphrase on standard input | No |
| `gatt.py` | BlueZ over D-Bus: a `NoInputNoOutput` agent, the LE advertisement, one GATT service with three characteristics | Yes, the only module that does |
| `__main__.py` | Wiring and signals | No |

Everything except `gatt.py` is tested with the standard library's `unittest` under `make image-test`. That container has `python3` but neither `pytest` nor `dbus-fast`, which is why D-Bus stays at the edge. `make image-test` runs only when an image is released from a tag, not on pull requests. The Makefile's `test` target, which every pull request runs, therefore gains a line that runs them with `python -m unittest discover`, so a pull request that breaks the codec or the state machine fails its own checks.

The daemon logs the phases it passes through and the SSIDs it joins. It never logs a command's payload.

### 5.3 When it runs

Both images ship the same units. `bluetooth.service` is disabled at build time, because `bluez`'s postinst enables it.

| Unit | Kind | Job |
|---|---|---|
| `kalinka-provision-check.service` | enabled, `Type=exec` | `After=dietpi-preboot.service dietpi-firstboot.service kalinka-firstboot.service`, as `kalinka-soundcard.service` waits for DietPi; the base's absent units are ignored. Only then has DietPi imported the card's `dietpi.txt` into `/boot/dietpi.txt` and applied `dietpi-wifi.txt`, or `firstboot.sh` applied `kalinka-firstboot.conf`. Earlier, the check would read the build's copy of `dietpi.txt` and see no Wi-Fi configured on a card that has one. Runs `provision-needed.sh`, and starts `kalinka-provision.service` without waiting if setup is needed. With `Type=exec`, its wait for a route holds up no target. |
| `kalinka-provision.service` | static | `Wants=` and `After=bluetooth.service`; runs `python3 -m kalinka_provision` with `PYTHONPATH=/usr/lib/kalinka-image` |
| `bluetooth.service.d/kalinka.conf` | drop-in | `StopWhenUnneeded=yes`: `bluetoothd` runs exactly as long as something wants it. That is the daemon, or `bluetooth.target` for someone who enabled Bluetooth for their own devices. |

`provision-needed.sh` decides, in this order:

1. Switched off (§5.7): no.
2. A default route exists: no.
3. An Ethernet interface has carrier: wait up to 20 s for a default route, and if one comes, no.
4. A Wi-Fi network is configured: wait up to 30 s for a default route, and if one comes, no. A card set up with the wrong password therefore still ends in setup.
5. No Bluetooth adapter: log one line, and no. This covers a PC without one and a Compute Module without wireless.
6. Otherwise, yes.

On a box that has a network, the only cost is that shell check, and `bluetoothd` never starts. Neither unit has a `Before=` on `kalinka.service` or the renderer, so neither delays them.

Once running, the daemon advertises and exits on the first of these:

- the box gets a default route by itself (a cable plugged in, a router that came back after a power cut) while no phone is connected and no join has been started. The route a join brings does not count, so that exit cannot pre-empt the reconnect below;
- 15 minutes pass without a command, so neither an empty room nor a device that connects and then idles holds the box open;
- the phone that joined the box has read the `connected` status, or 120 s have passed since the join.

It keeps advertising until the phone has read `connected`. A phone whose link dropped during the join (the Pi's Wi-Fi and Bluetooth share one chip) can then reconnect and read the result. On the way out it withdraws the advertisement and removes the phone's pairing. `bluetoothd`, no longer wanted, stops too. A box that is on its network runs no Bluetooth process.

A box that later loses its network, because the router was replaced or the password changed, finds no route at its next boot and offers setup again. Moving house needs no card and no login.

### 5.4 The GATT contract

One primary service, `7c8e0001-1b2f-4e6a-9d3c-4b616c696e6b` (the last six bytes spell `Kalink`). The advertising data carries the flags and this 128-bit UUID, 21 of its 31 bytes. The local name, `Kalinka-` followed by four hex digits from a hash of `/etc/machine-id`, goes in the scan response, which Android's active scan reads. The app filters its scan by the service UUID. The maker of a ready-made box can print the name on its label after the box's test boot.

| Characteristic | UUID | Properties | Value |
|---|---|---|---|
| status | `7c8e0002-…` | read, notify | The box's state (below) |
| networks | `7c8e0003-…` | read | One page of the last scan |
| command | `7c8e0004-…` | encrypt-write | One command |

The characteristics' UUIDs end like the service's.

Every value is one UTF-8 JSON object of at most 512 bytes, the longest value ATT allows. Unknown fields are ignored. `protocol` is a major version. The app refuses to continue with a box whose major version it does not know, and the box only adds fields within a major version.

```json
{"protocol": 1, "name": "Kalinka-3F2A", "model": "Raspberry Pi Zero 2 W Rev 1.0",
 "state": "connected", "reason": null, "ssid": "Studio",
 "ipv4": "192.168.1.50", "port": 8000,
 "server_id": "0b0c5a3e-8c1b-4a51-9a0d-6c9b1c7f1e2a"}
```

`state` is one of `idle`, `scanning`, `joining`, `connected` and `failed`. `reason` is set only with `failed`, to `wrong_password`, `not_found`, `no_address`, `timeout` or `error`. `ssid`, `ipv4` and `port` are set with `connected`. `port` comes from the server's configuration, and is 8000 unless someone changed it. `server_id` is `null` until the server has written it at its first start. `model` is `/proc/device-tree/model`, or the DMI product name on a PC.

```json
{"total": 23, "offset": 0,
 "networks": [{"ssid": "Studio", "rssi": -48, "secure": true},
              {"ssid": "Office", "rssi": -71, "secure": true, "enterprise": true},
              {"ssid_hex": "e9636f6c65", "rssi": -80, "secure": false}]}
```

Networks are sorted by signal, strongest first, with one entry per SSID. A page holds as many as fit in 512 bytes. An SSID that is not valid UTF-8 is sent as `ssid_hex`, and `join` accepts either form.

```json
{"op": "scan"}
{"op": "page", "offset": 8}
{"op": "join", "ssid": "Studio", "psk": "…", "country": "GB", "hidden": false}
```

`scan` refreshes the list, with `scanning` then `idle` reported through status. `page` moves the networks window. `join` starts a join. A command that is malformed, too long or unknown is answered with status `failed` and reason `error`. A command sent while a join is still running is refused with BlueZ's `org.bluez.Error.InProgress`, and the status stays as it was.

The app asks for an MTU of 517 before its first write. Windows lets the app request no MTU, and a link may settle lower, so a command can arrive as a long write. BlueZ joins the pieces of a long write before it calls `WriteValue`, so the daemon receives the whole command at `offset` 0, and it answers a write at any other offset with `failed`/`error`. The 512-byte cap applies to the whole command. A value longer than one packet is read in pieces too, as `ReadValue` calls with rising offsets. The daemon answers those from the value it returned at offset 0, so a status that changes mid-read never arrives torn. A notification is cut at the link's MTU, so the app treats every notification as a cue to read `status`, which always returns the whole value.

The first device to write a command holds the box until it disconnects. Commands from any other device get `InProgress`.

### 5.5 Pairing, and what it protects

`command` is marked `encrypt-write`, so the phone has to pair before its first command. The daemon's agent is `NoInputNoOutput`, which makes that pairing Just Works: Android shows its own "Pair with Kalinka-3F2A?" prompt. The first command is `scan`, sent as soon as the phone connects, so the prompt appears before anyone has typed a password.

LE Secure Connections is used wherever it is available. Every supported Pi from the 3B+ and the Zero 2 W up has a Bluetooth 4.2 or newer radio. Linux's SMP does the key agreement in software, so the original 3B's 4.1 controller may manage it too. Issue 1 records the pairing method `btmon` reports on each board. Where only legacy pairing is available, someone sniffing the air during those seconds could recover the link key. We accept that rather than add cryptography packages to both the box and the app (§8). Just Works also offers no protection against an active attacker in range, which §2.5 accepts.

The passphrase is written once and can never be read back: no characteristic returns it, and the daemon never logs it. `wifi.sh` passes it on standard input. On disk it ends up only where NetworkManager or `wpa_supplicant` keep it, in root-only files.

### 5.6 Image build changes

- **Both bases:** install `bluez` and `python3-dbus-fast`, disable `bluetooth.service`, enable `kalinka-provision-check.service`, and write `KALINKA_WIFI_BACKEND`. Neither package has any Recommends in trixie, so `EXCLUDED_PACKAGES` stays green, and the existing check proves it on every build.
- **DietPi base:** install `pi-bluetooth` and Raspberry Pi's `bluez-firmware`, plus `iw` and `wpasupplicant` if DietPi's image lacks them. Switch Bluetooth and the Wi-Fi modules on with `dietpi-set_hardware` in the chroot, if that works there. Otherwise the build edits `config.txt` and `modprobe.d` itself, the way `keep_logs_on_disk` edits `.installed`. The build must also stop DietPi's first boot from switching the Wi-Fi modules back off on a card that asks for no Wi-Fi (§10). `verify_dietpi_setup` checks the result: no `disable-bt` overlay, no module blacklist, and the units enabled and disabled as intended.
- With Bluetooth on, the Pi's PL011 UART belongs to the radio. DAC HATs use I²S and I²C, not the UART, so audio is unaffected. Someone who needs the PL011 on GPIO 14 and 15 sets `dtoverlay=disable-bt` and loses only Bluetooth setup.

### 5.7 Switching it off

Anything that uses the radio can be switched off. On a Pi, `KALINKA_BLE_SETUP=0` in `dietpi.txt` does it; DietPi ignores keys it does not know, and `provision-needed.sh` reads `/boot/dietpi.txt` the way `soundcard.sh` does. On the PC image, `BLE_SETUP=0` in `kalinka-firstboot.conf` makes `firstboot.sh` write `KALINKA_BLE_SETUP=0` to `/etc/default/kalinka-image`, which `provision-needed.sh` reads, because the conf file does not survive being read. Disabling the unit instead would not cancel the start job already queued for that boot. The check runs after `kalinka-firstboot.service` (§5.3), so the switch holds from the boot that reads it. From a login, `systemctl disable kalinka-provision-check.service` does it on either image.

## 6. Path B: the app

### 6.1 Structure

| Piece | Where | Job |
|---|---|---|
| `ProvisioningTransport` | `lib/providers/provisioning_transport.dart` | Interface: support, adapter state, permission, scan, connect (a link that reads status and networks, sends commands and reports notifications) |
| BLE transport | `provisioning_transport_io.dart` | `universal_ble` behind the interface |
| Web stub | `provisioning_transport_stub.dart` | Reports "unsupported"; chosen with `if (dart.library.js_interop)`, as `discovery_provider.dart` chooses its notifier |
| `box_protocol.dart` | `lib/providers/` | The §5.4 codec in plain Dart |
| `provisioningProvider` | `lib/providers/provisioning_provider.dart` | The flow's state machine. It gets its transport from a provider, so tests inject a fake. |
| `connectToServer` | `lib/providers/server_connect.dart` | Moved out of `DiscoveryScreen._connectToServer`; used by the server list, manual entry and the hand-off |

The provider's phases are `unsupported`, `needsPermission`, `bluetoothOff`, `scanning`, `found`, `connecting`, `pairing`, `incompatible`, `listingNetworks`, `choosingNetwork`, `enteringPassword`, `joining`, `joined`, `reaching`, `failed(reason)` and `done`. It gives up on a scan after 20 s with nothing found, and on a join after 60 s without a verdict from the box.

Android needs `BLUETOOTH_SCAN` (with `usesPermissionFlags="neverForLocation"`) and `BLUETOOTH_CONNECT`. Android 11 and older also need `BLUETOOTH`, `BLUETOOTH_ADMIN` and `ACCESS_FINE_LOCATION`, all with `maxSdkVersion="30"`, and location services switched on before a BLE scan returns anything. The app asks for these when the user opens the flow, never at launch. On Linux and Windows the entry appears only when an adapter is present. It never appears on the web.

### 6.2 Screens

The flow lives inside the wizard's step 0. `DiscoveryScreen` gains an `onSetUpNewBox` callback. `OnboardingScreen` then shows the flow in place of discovery, inside the same 240 ms `AnimatedSwitcher`. System back walks back through the flow, and from its first screen it returns to discovery.

The five screens use `OnboardingStepScaffold` with one addition: an optional `eyebrow`. It replaces the "STEP n OF m" line and the stepper, while the progress bar follows the flow. Three pieces are pulled out of existing screens so both places draw the same thing:

- `ScanningArt`, from `DiscoveryScreen._buildScanningArt`, with the icon as a parameter;
- `SignalBars`, from `_buildSignalBars`;
- `StepTimeline`, from `RestartOverlay._buildSteps`, with the steps passed in as data.

`kalinkaFieldDecoration` gains an optional `suffixIcon`, for the password field's show and hide toggle.

The palette rule holds on every screen: at most one berry fill at rest, the footer's primary button. Rows are neutral. A selected row takes the discovery list's accent edge, which is a mark, not a fill. Haptics follow the restart overlay's: a light impact for each finished step, `successCrescendo` when the box has joined.

**The entry card**, on the discovery step. In the empty state it sits under **Scan again**. In the found state it sits under the server list, because a new box is not in that list.

```
╭──────────────────────────────────────────────────╮
│ [ᛒ]  Setting up a new Kalinka box?             › │
│      Put it on your Wi-Fi from here.             │
╰──────────────────────────────────────────────────╯
```

It is drawn like the server list's card: `surfaceRaised`, `borderDefault`, radius 14. A 38 px `surfaceOverlay` tile holds `Icons.bluetooth_rounded`, the label is in `trayRowLabel`, the line under it in `trayRowSublabel`, and the chevron is in `textMuted`.

**1. Find your box**

```
WI-FI SETUP
━━━━━━━━━━━──────────────────────────────────────────
Find your box                                (dialogTitle)
Boxes that are switched on and not yet on a network
show up here. Keep this phone within a few metres.

                    ((( ᛒ )))                (ScanningArt)
            Looking for boxes nearby…

NEARBY                          (OnboardingSectionLabel)
╭ SettingsCard ────────────────────────────────────╮
▌[▣] Kalinka-3F2A                           ▂▄▆█ │
│    Not on a network yet                          │
├──────────────────────────────────────────────────┤
│ [▣] Kalinka-91C0                           ▂▄▁▁ │
╰──────────────────────────────────────────────────╯
[ Back ]  [              Continue              ]
```

The rows follow the discovery list's pattern: an icon tile, `trayRowLabel` and `trayRowSublabel`, `SignalBars` computed from RSSI, and the accent edge on the selected row. The scan keeps running while the list is shown. Other states of this screen:

- **Permission needed**: `WarningNote` (warning), "Kalinka needs permission to look for nearby devices. It looks only while this screen is open.", with a neutral compact **Allow** button.
- **Bluetooth off**: "Bluetooth is off on this phone.", with **Turn on** where Android offers the system dialog.
- **Nothing found after 20 s**: an `OnboardingNote` for each thing to check.
  - "Is it switched on? A new box looks for a phone for 15 minutes after it starts. Switch it off and on again for another 15."
  - "Is a network cable plugged in? Then it's already on your network. Go back and it will be in the list."
  - "Older Kalinka images can't do this. Use the settings on the card instead."

  **Scan again** is neutral here, so that the disabled Continue stays the screen's only berry.

**2. Connecting**, with the eyebrow `WI-FI SETUP · KALINKA-3F2A`.

```
Connecting to Kalinka-3F2A
Your phone may ask to pair with it. Allow it: pairing
keeps your Wi-Fi password private on the way.

  ● Connecting               Over Bluetooth  (StepTimeline)
  │
  ◉ Pairing                  Allow it when your phone asks
  │
  ○ Listening for networks   Kalinka-3F2A scans the air
```

The screen moves on by itself once the networks arrive. Continue is shown disabled until then.

- **Pairing refused**: "Pairing didn't go through. Kalinka-3F2A takes a password only over a paired link.", with **Try again**.
- **Unknown protocol major**: `WarningNote` (warning), "Kalinka-3F2A needs a newer version of this app.", with **Get the update**, which opens the releases page through `url_launcher`.
- **Link lost**: "Lost touch with Kalinka-3F2A. Move closer and try again."

**3. Choose your Wi-Fi**

```
Choose your Wi-Fi
Kalinka-3F2A can hear these networks. Pick the one
this phone is on.

NETWORKS
╭ SettingsCard ────────────────────────────────────╮
│ ◉  Studio                    Secured     🔒 ▂▄▆█ │
├──────────────────────────────────────────────────┤
│ ○  Office          Needs a sign-in this setup … │  (dimmed)
├──────────────────────────────────────────────────┤
│ ○  Café downstairs           Open           ▂▁▁▁ │
├──────────────────────────────────────────────────┤
│ ○  Other network…                                │
╰──────────────────────────────────────────────────╯
                                      [ Rescan ]
[ Back ]  [              Continue              ]
```

The rows use the wizard's `RadioMark`, built like `step_output.dart`'s output rows: the whole row lights under the pointer, and a row that cannot be chosen sits at 0.45 opacity. Enterprise networks show at that opacity. When there are more networks than fit, the last row is **Show more** and sends `page`. When `model` says Zero 2 W or Pi 3, an `OnboardingNote` adds: "A Pi Zero 2 W or Pi 3 hears 2.4 GHz networks only. If yours is missing, your router may offer a 2.4 GHz network under another name."

**4. Password**

```
Password for Studio
It goes to Kalinka-3F2A over the paired link, and
Kalinka never shows it again.

╭──────────────────────────────────────────────╮
│ Wi-Fi password                          👁   │  (kalinkaFieldDecoration)
╰──────────────────────────────────────────────╯
Country    United Kingdom (GB)          Change
[ Back ]  [            Join Studio            ]
```

For **Other network…** a *Network name* field comes first, and the network is sent as hidden. For an open network the password field is not shown, and the subtitle reads "Studio is open: no password needed." The password must be 8 to 63 printable ASCII characters, WPA's rule for a passphrase. A 64-hex-digit key is refused, because the box passes the passphrase through (§5.1) and a raw key cannot join a WPA3-SAE network. The field's error line says so in `statusOffline`. The country defaults to the phone's region. **Change** opens a `KalinkaBottomSheet` listing the countries.

**5. Joining**

```
Joining Studio
This takes up to a minute.

                      ╭────╮
                      │ ⟳  │                (the restart overlay's tile)
                      ╰────╯
  ● Password sent        Over the paired link
  │
  ◉ Joining Studio       Checking the password
  │
  ○ Getting an address   From your router
  │
  ○ Reaching Kalinka     It starts once it's online
```

The 60 px tile is the restart overlay's, and it spins while the box works. When the box reports `connected`, the tile crossfades to the check, as the restart overlay's done state does. The title becomes "Kalinka-3F2A is on Studio", and after 1.2 s the hand-off (§6.4) moves the wizard on to **Music sources**. On a failure, the failing step's dot turns `statusOffline` and a `WarningNote` (error) gives the reason (§6.3). The footer then offers **Choose another network** (neutral) and **Try again** (accent).

### 6.3 Failure copy

| Cause | What the screen says | Try again goes to |
|---|---|---|
| `wrong_password` | Studio didn't accept that password. Passwords are case-sensitive: check it and try again. | Password, with the typed text kept |
| `not_found` | Kalinka-3F2A can't hear Studio any more. Move it closer to your router, or choose another network. | Networks, rescanned |
| `no_address` | Kalinka-3F2A joined Studio, but your router gave it no address. Restart the router, then try again. | Joining |
| `timeout` | Studio didn't answer in time. Try again, and if it keeps happening, move the box closer to the router. | Joining |
| `error` | Something went wrong on Kalinka-3F2A. Try again, or use a network cable. | Joining |
| No verdict in 60 s | Kalinka-3F2A stopped answering. It may have joined anyway, so Kalinka is looking for it on Studio. | 30 s of mDNS for its `server_id`, then Joining |
| Joined, but the phone can't reach it | Kalinka-3F2A is on Studio at 192.168.1.50, but this phone can't reach it. Is this phone on Studio too? Guest networks keep devices apart. | Reaching again, or **Enter address manually** |

### 6.4 Hand-off to the wizard

With `connected`, the provider calls `connectToServer` with the box's name, `ipv4` and `port`, held in memory as the wizard holds discovery's choice (`persistConnection: false`). The server may still be starting, so the call retries its health check for up to 90 seconds. Meanwhile the timeline stays on **Reaching Kalinka**. If the direct address fails, the provider tries an mDNS record with the same `server_id`. On success it calls the wizard's existing `_onConnected`, which loads the config and moves to step 2, **Music sources**. Back from there goes to discovery, where the box is now listed like any other server. After success the app removes its side of the pairing where the platform allows, so the next setup of this box, or of a re-imaged one, starts clean.

The same card works from the server-switching overlay. There the hand-off connects and closes the overlay, and the existing check of the server's `oobe_complete` flag opens the wizard at **Music sources**.

## 7. Path C: the hotspot, and why not

On a box with no network, a hotspot would raise an access point named something like `Kalinka-Setup`, hand out addresses and answer every DNS query with its own address. The phone's captive-portal check would then open a page where the user picks a network and types the password. Moode and Volumio work this way. For Kalinka it costs:

- **Packages the build refuses.** `hostapd` and `dnsmasq-base`: `dnsmasq-base` is on the `EXCLUDED_PACKAGES` list today, and NetworkManager's own hotspot mode on the PC image needs it too. Add `nftables` to send port 80 to wherever the page is served.
- **A privileged helper anyway.** The page's backend has to write network configuration, which the server must not be able to do (§2.4). The hotspot therefore needs the same root-side program as path B, plus a web endpoint in front of it.
- **A second UI.** The browser player is a Flutter web bundle of several megabytes, served by a server that may still be starting. Phones show captive pages in a stripped-down browser window that closes when the phone leaves the network. A hand-written HTML page would have to carry Kalinka's look separately and be kept in step with the app.
- **A join that drops the phone.** The Pi's `brcmfmac` cannot scan reliably while it runs the access point, so the list goes stale. Joining the chosen network means taking the access point down, which drops the phone in the middle of the conversation. A wrong password is reported only after the box has raised its access point again and the user has found and rejoined it. That is the loop the Moode and Volumio forums are full of. Android also offers to leave a network "with no internet" in favour of mobile data, and then the page does not load at all.
- **PC radios.** Some PC Wi-Fi cards refuse to act as an access point. Intel's, the commonest, will not start one on 5 GHz channels.

Its one advantage is that no app is needed. That buys less than it seems: a Kalinka user without the app uses the browser player, and the browser player cannot reach a box that is not yet on the network. The iPhone gap (§2.6) is real, and the right way to close it is an iOS build of the app, not a second setup UI.

## 8. Other alternatives rejected

- **The GATT server inside the Kalinka server.** The server runs as `kalusr` under `NoNewPrivileges`, with plugins in its process, and must not write network configuration (§2.4).
- **`python3-dbus` with `python3-gi`.** This pair is smaller on disk: about 2.4 MB (`python3-dbus`, `python3-gi`, `gir1.2-glib-2.0`, `gir1.2-girepository-2.0`, `libgirepository-1.0-1`), against 4.3 MB for `python3-dbus-fast` and the Sphinx JavaScript it drags in. But it runs a GLib main loop, with the D-Bus service written against it, where `dbus-fast` fits one asyncio loop that also runs the timers and the `wifi.sh` subprocesses. `python3-dbus-fast` is in trixie main, and is what Debian's own `python3-bleak` depends on. The pair stays the fallback if `dbus-fast` gives trouble on the box.
- **A separate venv with `dbus-fast` from PyPI.** That would mean a network dependency at build time and a second Python tree to maintain, for about 600 lines of code.
- **Scripting `bluetoothctl`'s GATT menu.** It needs no Python package, but it answers reads and writes through interactive prompts. That is no base for a protocol that carries a passphrase.
- **Key agreement in the application (X25519 and an AEAD) instead of BLE link encryption.** It would add `python3-cryptography` to the box and a cryptography package to the app, only to protect legacy pairing where it remains (§5.5).
- **Sending a pre-hashed key from the app.** It would shut out WPA3-SAE (§5.1). DietPi's handling of a hex key is also the uncertain part (§10). The encrypted link already protects the passphrase in transit.
- **Only one Imager front-end.** Imager's older output runs through `firstrun.sh` in `kernel-command-line.target`, where no unit of ours can step in. Providing the two programs the script looks for costs little and covers that case (§4.2).
- **Moving the Pi images to `kalinka-firstboot.conf`.** One file for both images would be tidier, but it means a larger rework of DietPi's first boot for no first-run gain. It remains a possible later direction.
- **Switching setup off from the app or the server's settings.** The daemon is outside the server's process and privileges. It stops by itself, and §5.7 covers turning it off for good.

## 9. Compatibility

- The REST/WebSocket API, the renderer protocol and the plugin SDK do not change, and `REST_API_VERSION` stays where it is. The BLE contract is new and additive, with its own `protocol` major version.
- **An old app with a new image:** the box advertises for up to 15 minutes at each boot until it has a network, and nothing else changes.
- **A new app with an old image:** the card appears, the scan finds nothing, and the empty state points at the card files.
- **Imager:** old images keep ignoring its customisation. New Pi images honour both `custom.toml` and the `firstrun.sh` hooks. The card files (`dietpi.txt`, `dietpi-wifi.txt`, `kalinka-firstboot.conf`) stay the documented fallback, unchanged.
- **Protocol majors:** a box with an unknown major is listed, and the Connecting screen says the app needs updating (§6.2). A box ignores fields it does not know and answers unknown commands with `failed`/`error`.

## 10. Open questions for the spike

Issue 1 answers these on a Pi Zero 2 W, a 3B, a 5 and the PC image, before the design is accepted.

1. What the current Imager writes for a custom image (`custom.toml`, `firstrun.sh` and `cmdline.txt`, or something else), and what today's image does with it. In particular, does the first boot stop in `kernel-command-line.target`?
2. DietPi's defaults: are Bluetooth and the Wi-Fi modules off? What are the exact `dietpi-set_hardware` calls, and do they run in the build chroot? Does DietPi's first boot switch the Wi-Fi modules back off when `AUTO_SETUP_NET_WIFI_ENABLED=0`, and if so, does setting it to 1 with an empty network database make the first boot wait?
3. `dietpi-wifidb`: does it accept a 64-hex key? Can the network be written where DietPi keeps it on the root filesystem instead of the FAT partition? Do hidden networks need `scan_ssid`?
4. `pi-bluetooth` and Raspberry Pi's `bluez-firmware` on DietPi Trixie: where they come from and their arm64 sizes. Do `/usr/lib/raspberrypi-sys-mods` and `/usr/lib/userconf-pi` stay unowned on DietPi?
5. BlueZ: does the name reach the phone in the scan response? Does `encrypt-write` trigger Android's pairing prompt? Which pairing method does each board use, according to `btmon`? Does the BLE link survive the Wi-Fi join on the Zero 2 W's shared chip? Does a long write from a Windows desktop reach the daemon as one `WriteValue` call at offset 0?
6. Measurements on the Zero 2 W, replacing the estimates of §3: resident memory of `bluetoothd` and the daemon, the `systemd-analyze critical-chain kalinka.service` change on a box with a network and one without, and the time from power to the box being visible on the phone.
7. `universal_ble` on Android, Linux and Windows: scanning with a service filter, requesting an MTU, pairing and unpairing, and its licence against the app's. The fallback is `flutter_blue_plus` with Android alone, which is the acceptance platform anyway.

## 11. Acceptance tests

**The design:** the maintainer's review of this document, and issue 1's check that BLE advertising works from a cold boot with no keyboard, and that a write from a phone arrives. Both come before anything else is built.

**Path A:**

- A card written by the current Imager boots an `rpi234` onto the configured Wi-Fi, with the host name and the login applied and `custom.toml` gone.
- An Imager that writes `firstrun.sh` gives the same result after its one reboot.
- After first boot no passphrase is left on the FAT partition, and under `/etc` it is only in root-only files.
- `tests/test_imager_custom.sh` covers a full file, Wi-Fi only, user only, a hex key and a plaintext one, a hidden network, CRLF and a BOM, an SSID and a passphrase containing `'`, a malformed file left in place, and the shred.
- `test_firstboot.sh` still passes with `create_account` moved, and `make image-test` is green.

**Path B, box:**

- Recorder tests of `wifi.sh` on both backends, including the passphrase never reaching `argv`. On `nm` they also cover the keyfile for an open, a WPA2 and a WPA3-only network, a hidden one, an SSID with a `;` or a non-UTF-8 byte, and a passphrase with a `\` or a leading space.
- `unittest` tests of the codec (the 512-byte cap, unknown fields, unknown ops, `ssid_hex`) and of the state machine (join while joining, the one-phone lock, the three exits, a notification sent for every state change).
- On real Pis:
  - the box is visible to the phone within 45 s of power on a Zero 2 W;
  - a correct password reaches wizard step 2 within 90 s;
  - a wrong password is reported within 45 s, and a retry then works;
  - a hidden SSID joins;
  - a box with a cable never advertises;
  - `kalinka-provision` and `bluetoothd` are gone within three minutes of joining;
  - a box whose router was switched off at boot stops advertising once the router is back;
  - the journal holds no passphrase;
  - `KALINKA_BLE_SETUP=0` keeps the radio silent.
- On the PC image, a USB adapter provisions it, and without one the unit exits 0 with one log line.

**Path B, app:**

- Provider tests against a fake transport for every phase and failure reason, the 20 s scan and 60 s join timeouts, the mDNS fallback by `server_id`, and an unknown protocol major.
- Widget tests for the card; for Find your box scanning, found, empty, permission and Bluetooth off; for Connecting's pairing-refused and incompatible states; for the network list with enterprise and paged entries; for the password field's validation; and for Joining's success and each failure's copy.
- Manual runs on Android 12 or newer, on Android 10 for the location path, and on a Linux desktop.

**Compatibility:** an old app against a new image and a new app against an old image behave as §9 says.

## 12. Plan

Each issue is about a week's work. The Imager change (issue 2) ships independently of the rest.

| # | Repository | Issue | Needs |
|---|---|---|---|
| 1 | KalinkaPlayer | Spike: pin DietPi, Imager and BlueZ facts on real boards; replace §3's estimates with measurements | — |
| 2 | KalinkaPlayer | Honour Raspberry Pi Imager's customisation on the Pi images (§4) | 1 |
| 3 | KalinkaPlayer | One Wi-Fi backend for both images: `wifi.sh` with `nm` and `dietpi` (§5.1) | 1 |
| 4 | KalinkaPlayer | The provisioning daemon, its unit and the image build (§5.2–§5.7) | 3 |
| 5 | KalinkaAI | Provisioning transport and provider (§6.1) | 1 |
| 6 | KalinkaAI | Wi-Fi setup screens and the hand-off to the wizard (§6.2–§6.4) | 4, 5 |

Relevant existing code: [firstboot.sh](../packages/kalinka-image/overlays/debootstrap/usr/lib/kalinka-image/firstboot.sh), [base-dietpi.sh](../packages/kalinka-image/lib/base-dietpi.sh), [dietpi-conf.sh](../packages/kalinka-image/overlays/dietpi/usr/lib/kalinka-image/dietpi-conf.sh), [soundcard.sh](../packages/kalinka-image/overlays/dietpi/usr/lib/kalinka-image/soundcard.sh), [kalinka.sh](../packages/kalinka-image/lib/kalinka.sh) (the refused packages), [the image README](../packages/kalinka-image/README.md), [service_discovery.py](../packages/kalinka-server/src/kalinka_server/service_discovery.py), [server_identity.py](../packages/kalinka-server/src/kalinka_server/server_identity.py), [kalinka.service](../packages/kalinka-server/scripts/kalinka.service), and in the app [onboarding_screen.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/screens/onboarding_screen.dart), [discovery_screen.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/widgets/discovery_screen.dart), [onboarding_step_scaffold.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/widgets/onboarding/onboarding_step_scaffold.dart), [restart_overlay.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/widgets/restart_overlay.dart) and [PALETTE.md](https://github.com/Kalinka-Player/KalinkaAI/blob/main/PALETTE.md).

Platform references: [BlueZ GATT API](https://github.com/bluez/bluez/blob/master/doc/org.bluez.GattCharacteristic.rst), [BlueZ LE advertising API](https://github.com/bluez/bluez/blob/master/doc/org.bluez.LEAdvertisement.rst), [dbus-fast](https://github.com/Bluetooth-Devices/dbus-fast), [universal_ble](https://pub.dev/packages/universal_ble), [Android Bluetooth permissions](https://developer.android.com/develop/connectivity/bluetooth/bt-permissions), [Raspberry Pi Imager](https://github.com/raspberrypi/rpi-imager) and [DietPi](https://github.com/MichaIng/DietPi).
