# First-run Wi-Fi — design

A Kalinka image gets onto Wi-Fi only if someone edits a file on its card before the first boot. That works for a person who flashes their own card and knows where to look. It does not work for a box sold ready-made, which comes with no card the buyer is meant to edit, no keyboard and no screen. It also fails on the PC image, whose settings partition is an EFI partition that desktops hide. The app cannot help either: its setup wizard starts by finding the server on the network, which is the very thing that is missing.

This document compares three ways out. Bluetooth LE provisioning from the app is built for boxes nobody flashes, on the Pi images first (§5, §6). Raspberry Pi Imager's own customisation is honoured on the Pi images as a separate convenience for people who flash their own card, and nothing else waits for it (§4). The hotspot with a captive page is not built (§7). Nothing here touches the audio path, the REST/WebSocket API, the renderer protocol or the plugin SDK.

Status: proposed, 2026-09-28, and revised the same day after the maintainer's review. The decision stands on facts that were read from the code and from Debian 13's package indexes. Facts about DietPi, about Imager's output and about the radios could not be checked without the hardware. §10 lists them. A check of its own settles the Imager facts, ahead of the plan, because they concern today's image. The first issue of the plan (§12) settles the rest before anything is built. Every memory and boot-time figure below is an estimate until that issue replaces it with a measurement.

## 1. Where things stand

### 1.1 How a box gets online today

| Image | Wi-Fi comes from | Who can do it |
|---|---|---|
| `rpi234`, `rpi5` (DietPi) | `AUTO_SETUP_NET_WIFI_ENABLED=1` in `dietpi.txt` and the network in `dietpi-wifi.txt`, on the FAT partition, before the first boot. DietPi's first boot applies them and removes the files. | Someone who can mount the card and edit two files. |
| `amd64` (Debian) | `kalinka-firstboot.conf` on the FAT partition, applied by [firstboot.sh](../packages/kalinka-image/overlays/debootstrap/usr/lib/kalinka-image/firstboot.sh) through NetworkManager, then shredded. | Someone who can mount a hidden EFI partition. The [installation guide](installation.md#on-the-pc-image) needs a page of `diskpart` and `diskutil` for it. |

Raspberry Pi Imager offers to set Wi-Fi, a user and SSH, and our images ignore all of it. The guide tells people to skip that screen ([installation.md](installation.md#2-write-it-to-the-card), step 4), even though it is where anyone who has set up Raspberry Pi OS types their Wi-Fi.

A Pi Zero 2 W has no Ethernet port. For the smallest supported board, "use a cable" means a USB adapter.

In the app, the wizard ([onboarding_screen.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/screens/onboarding_screen.dart)) starts at step 0, the `DiscoveryScreen`, which finds servers by mDNS or takes an address by hand. A box that is not on the network never appears there. The app ships for Android, Windows, Linux and the browser. It is headed for the Play Store and the App Store, with an iOS build.

### 1.2 What there is to build on

- DietPi's first boot already turns `dietpi.txt` and `dietpi-wifi.txt` into a working Wi-Fi setup. Writing DietPi's own inputs reuses that path, and nothing has to replace it.
- [dietpi-conf.sh](../packages/kalinka-image/overlays/dietpi/usr/lib/kalinka-image/dietpi-conf.sh) reads and writes `dietpi.txt` the way DietPi does. [kalinka-soundcard.service](../packages/kalinka-image/overlays/dietpi/usr/lib/systemd/system/kalinka-soundcard.service) already drives a DietPi tool (`dietpi-set_hardware`) from a Kalinka unit.
- `firstboot.sh` has `configure_wifi` for NetworkManager and `create_account` for a sudo account with a hash or a key. [test_firstboot.sh](../packages/kalinka-image/tests/test_firstboot.sh) shows how to test such code with recorders in place of the real tools.
- The server listens on every address by default (`interface: all`). It also re-reads its interfaces every 10 seconds and announces a new address over mDNS ([service_discovery.py](../packages/kalinka-server/src/kalinka_server/service_discovery.py)). A box that joins Wi-Fi after the server started is therefore reachable and discoverable within seconds, with no restart. That holds for a server that is running. [kalinka.service](../packages/kalinka-server/scripts/kalinka.service) has `Wants=` and `After=network-online.target`, so on a first boot with no network the server may still be waiting when the join completes. The join is then what starts it, and the hand-off waits through its cold start (§6.4).
- The server's identity is `/var/lib/kalinka/server_id`, the same value its mDNS record carries as `server_id`. It exists only once the server has started for the first time.
- Both bases name every box `kalinka` (`AUTO_SETUP_NET_HOSTNAME` in [base-dietpi.sh](../packages/kalinka-image/lib/base-dietpi.sh), `/etc/hostname` in [base-debootstrap.sh](../packages/kalinka-image/lib/base-debootstrap.sh)), so a host name does not tell two new boxes apart.
- The app already picks a platform implementation with a conditional import: `discovery_provider.dart` chooses between `discovery_notifier_io.dart` and a web stub.

## 2. Decisions

### 2.1 Honour Raspberry Pi Imager on the Pi images, apart from the rest

People already type their Wi-Fi into Imager. Applying it costs no new package, since `python3` and its `tomllib` are already on the image. It also removes the one instruction in the guide that is there only because we fall short. It is a convenience for people who flash their own card, and it does nothing for a box the buyer never flashes, so nothing in path B waits for it. It ships as its own small change (issue 2) whenever it is ready. If Imager's customisation stops today's image at its first boot (§4.1), that is a defect in the image as it ships, and it is checked on its own, ahead of the plan.

### 2.2 Build Bluetooth LE provisioning for boxes nobody flashes

The box advertises over Bluetooth LE while it has no network. The app finds it, the phone pairs with it, and the box receives the Wi-Fi network and its password over the encrypted link. The box joins and reports its address, and the app carries on into the wizard. This is the only path that works for a ready-made box with no card to edit and no screen or keyboard to use. It asks the buyer only for the app, on Android or iPhone, and their Wi-Fi password.

### 2.3 Do not build the hotspot

A temporary access point with a captive page works from any phone, which is its one real advantage. It costs a privileged helper, a second web UI, packages the image keeps out today, and a join flow that drops the phone halfway through. §7 gives the full argument.

### 2.4 Provisioning belongs to the image, not the server

Writing network configuration is a root job. The server runs as `kalusr` with `NoNewPrivileges=yes`, alongside third-party plugins, and it must stay unable to do that job. Provisioning is an appliance concern, like growing the root filesystem or applying a sound card. It lives in `packages/kalinka-image` and ships only on the images. A one-command install on someone's own machine has a keyboard and a network already.

### 2.5 Proximity is the authorisation; the link is encrypted

Anyone within Bluetooth range of a box that is waiting for setup can put it on a network. Every commercial speaker makes the same trade. A box that has never had a network waits for setup until it gets one: nothing on it works without a network, and nothing on it needs protecting yet. A box that has had a network first gives its router five minutes to come back, and then offers setup for 15 minutes only (§5.3). Either can be switched off (§5.7). The password itself travels only over a link that the phone and the box have paired and encrypted (§5.5).

### 2.6 What this leaves out, plainly

- **Changing Wi-Fi later.** This design puts a box on its first network, and on a new one when its old one is gone. Moving a box from a cable to Wi-Fi, or to a new network while the old one still works, is not covered. A streamer's owner expects a Network page in the app's settings for that. It is separate work, filed on its own. It can keep §2.4's boundary with a root helper that accepts only fixed commands and reuses `wifi.sh` (§5.1).
- **Enterprise Wi-Fi** (802.1X, "sign in with a user name"). The network list shows such networks but they cannot be chosen, and they need a cable or a login. Home networks are WPA2/WPA3-Personal or open, and those are covered.
- **The same network.** The hand-off needs the phone on the network it just gave the box. Guest networks with client isolation already stop discovery today. The app says so when it happens, and on an iPhone it tells that apart from a declined Local Network permission (§6.3), rather than failing without a word.

## 3. The three paths compared

Disk sizes are Debian 13's `Installed-Size` for amd64. arm64 sizes differ slightly. Memory and boot times are estimates for a Pi Zero 2 W, the smallest board, and issue 1 measures them.

| | A. Imager customisation | B. Bluetooth LE from the app | C. Hotspot and captive page |
|---|---|---|---|
| Serves | People who flash their own Pi card with Imager | Anyone with the app on Android or iPhone, or a Linux or Windows desktop with Bluetooth, including buyers of a ready-made box | Any phone with a browser |
| New on the Pi images | Nothing | `bluez`, `python3-dbus-fast`, `pi-bluetooth` and Raspberry Pi's `bluez-firmware`; Bluetooth and the Wi-Fi modules switched on in DietPi | `hostapd`, `dnsmasq-base` (kept out of the image today), `nftables` for the port-80 redirect, a root helper and a page |
| New on the PC image | Not offered | Later (§12, issue 8): `bluez`, `python3-dbus-fast`. Intel and Realtek Bluetooth firmware is already in `firmware-iwlwifi` and `firmware-realtek`. | `hostapd`, `dnsmasq-base`, `nftables` |
| New in the app | Nothing | `universal_ble`; Android Bluetooth permissions; iOS Bluetooth and Local Network usage strings | Nothing |
| Disk | A few kilobytes of scripts | About 9 MB: `bluez` 4.8, `python3-dbus-fast` 3.2, and the Sphinx JavaScript it hard-depends on, 1.1 (`libjs-sphinxdoc`, `libjs-jquery`, `libjs-underscore`). Up to 2 MB more for `libdw1t64` and `libelf1t64` where missing. GLib, which BlueZ needs, is already there through ffmpeg's `librsvg2-2`. | About 5 MB: `hostapd` 2.3, `dnsmasq-base` 1.1, `libnftables1` 1.1 and small netfilter libraries |
| Memory while working (estimate) | One short Python process at first boot | 18–23 MB: `bluetoothd` about 3, the daemon 15–20. A box that has never had a network carries it until it is set up. | About 5 MB for `hostapd` and `dnsmasq`, plus whatever serves the page |
| Memory once on the network | None | None. Both processes exit; the kernel's Bluetooth modules stay loaded (under 1 MB). | None |
| Boot cost, box already on a network | None: the unit's condition is false | One shell check off the critical path; the Bluetooth firmware loads in parallel (1–2 s) | The same shell check |
| Boot cost, box with no network | 1–2 s before DietPi's first boot, first boot only | `bluetoothd` and the daemon take 2–3 s to start, off the critical path | The access point takes about 5 s to come up |
| When it goes wrong | The file stays on the card and the box boots without Wi-Fi. With path B on the image, the box then advertises for setup. | A wrong password is retried on the same screen, and a network the box already had stays as it was. A new box keeps waiting; one that had a network is switched off and on for another 15 minutes, or given a cable or the card files. | The phone leaves the hotspot mid-join, and a wrong password shows only once the user rejoins the hotspot |
| Platforms | Wherever Imager runs | Android and iPhone; Linux and Windows desktops with an adapter | Any |

None of the three costs anything once the box is on its network. What separates them is who each one reaches, and what goes wrong while it runs.

## 4. Path A: Imager customisation

### 4.1 What Imager leaves on the card

Imager has written customisation in two forms. One is `custom.toml` on the FAT partition, which Raspberry Pi OS reads at its first boot. The other is `firstrun.sh`, with `systemd.run=` and `systemd.unit=kernel-command-line.target` added to `cmdline.txt`. The script calls `/usr/lib/raspberrypi-sys-mods/imager_custom` and `/usr/lib/userconf-pi/userconf` when they exist, and otherwise writes Raspberry Pi OS's files itself. DietPi reads neither form. Imager's output has changed between releases, and newer releases may write cloud-init's `user-data` and `network-config` as well. So a check of its own, ahead of the plan (§12), records exactly what the current release writes for a **custom** image, cloud-init files included, and what today's image does with that output. If `systemd.run=` names a path that does not exist on DietPi, the first boot stops in `kernel-command-line.target`. The guide's "skip it" is then all that keeps users from a box that never comes up, and that is a defect in today's image whatever becomes of the rest of this design. If Imager writes cloud-init files for a custom image, issue 2 decides from what the check finds whether they need a front-end of their own.

### 4.2 One applier, two front-ends

`imager-custom.sh` in the DietPi overlay is the only code that applies anything. It writes only DietPi's own inputs: keys in the card's `dietpi.txt`, set with `dietpi_conf_set`, and the network in `dietpi-wifi.txt`. DietPi's first boot then applies them as if a person had edited the files. The applier also creates the login account with `create_account`, which moves from `firstboot.sh` into `overlays/common/usr/lib/kalinka-image/account.sh` so that both bases share one copy.

Two front-ends feed it, so it does not matter which form Imager wrote:

- **`custom.toml`**: `imager-toml.py` parses the file with the standard library's `tomllib` on the system interpreter and checks every value. It passes the values to the applier as NUL-separated pairs on a pipe, never on a command line, because any local user can read another process's arguments. `kalinka-imager-custom.service` runs it (§4.3).
- **`firstrun.sh`**: the two programs the script looks for, at the paths where it looks for them. Each one calls the applier. `userconf` creates the named account instead of renaming DietPi's `dietpi` user, which DietPi's own tools expect. Where `firstrun.sh` finds `imager_custom`, it writes no `authorized_keys` itself. It passes the keys as `imager_custom enable_ssh -k KEY…`, before `userconf` runs. `enable_ssh` therefore installs them for UID 1000, DietPi's `dietpi`, where the script would otherwise have written them. `userconf` then moves them over to the new account. The Imager check confirms the order against the current script. The build fails if any package owns either path (`dpkg -S`), so a later `raspberrypi-sys-mods` cannot overwrite them without anyone noticing.

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

`kalinka-imager-custom.service` is a oneshot with `ConditionPathExists=/boot/firmware/custom.toml`. It runs after `local-fs.target` and `systemd-remount-fs.service`, so that the boot partition is mounted and the root filesystem, `/home` included, is writable for `useradd`, and before `dietpi-preboot.service` and `dietpi-firstboot.service`, with `DefaultDependencies=no` so the ordering forms no cycle. Issue 2 confirms where it sits with `systemd-analyze verify`. It applies only while DietPi has never booted (`/boot/dietpi/.install_stage` is `-1`, which the build already checks), because the `AUTO_SETUP_*` keys mean nothing after that. A `custom.toml` found later is logged as not applied, and shredded. The `firstrun.sh` path runs earlier still, in its own boot that ends in a reboot. Either way, the applier finishes before DietPi's first boot reads its files.

### 4.4 Secrets

Once `custom.toml` has been applied it is shredded, as `kalinka-firstboot.conf` is, because FAT has no permissions to protect it with. A file that cannot be parsed is left in place. That matches the PC image, where a file still on the card after boot tells the user it was not read. With `password_encrypted`, Imager writes the Wi-Fi key as 64 hex digits, the key derived from the passphrase. Whether `dietpi-wifidb` accepts such a key is open (§10). If DietPi's first boot also takes the network from where DietPi keeps it on the root filesystem, the applier writes it there, and the key never touches the FAT. Otherwise it goes into `dietpi-wifi.txt`, which DietPi removes after the import.

### 4.5 Fallback and documentation

If nothing applies, the box boots without Wi-Fi, exactly as it does today. On an image that also carries path B, it then advertises for setup, so a failure in path A leads into path B. The card files stay documented and unchanged. The guide's Imager step changes from "skip it" to "set your Wi-Fi, host name and a user here", and the image README and the release-notes template change with it.

## 5. Path B: Bluetooth LE provisioning, the box

The Pi images come first. The PC image follows once the Pi path has shipped (§12, issue 8): x86 boxes nearly always have Ethernet, so Bluetooth setup adds little there. This section describes both, so that the second needs no new design.

### 5.1 The Wi-Fi backend

`overlays/common/usr/lib/kalinka-image/wifi.sh` does three things, the same on both images: `scan`, `join` and `status`. The base records which backend it uses as `KALINKA_WIFI_BACKEND` in `/etc/default/kalinka-image`. `firstboot.sh` already reads that file, and the DietPi base starts writing one too.

- `scan COUNTRY` sets the regulatory domain to `COUNTRY` first, for the running radio only, then prints one network per line, tab-separated: signal in dBm, security (`open`, `psk`, `sae` or `eap`), and the SSID as hex octets. Only a join that succeeds stores a country, so a scan, or a join that fails, leaves the box's stored country as it was, and with it the channels its stored network may be on. A network that offers WPA2 and WPA3 together (transition mode) is reported as `psk`, which every supported radio can join. `sae` is reported only for a WPA3-only network. SSIDs are 32 arbitrary bytes, and hex keeps tabs and newlines out of the parse. The radio scans under the domain it booted with, which leaves out channels 12 and 13 and some 5 GHz channels until the country allows them.
- `join SSID_HEX SECURITY COUNTRY HIDDEN` reads the passphrase from standard input, never from `argv`. `SECURITY` is what the daemon's last scan reported for that SSID. A hidden network appears in no scan, so for one the daemon passes `psk` when the phone sent a passphrase and `open` when it did not. On success it prints the IPv4 address and exits 0. Otherwise its exit status names the reason, 1 to 5 in this order: `wrong_password`, `not_found`, `no_address`, `timeout`, `error`. The whole join is bounded at 45 seconds.
- `status` prints the network and address the box is on, or nothing.

A join is staged apart from the networks the box already has. Only a join that succeeds replaces a stored network with the same SSID. One that fails drops what it staged and leaves the stored network as it was. Take a box set up for "Studio" whose setup opened after a power cut: a wrong password sent for "Studio" then cannot delete the working network, and the box goes back onto it when the router returns.

The **`nm`** backend, for the PC image, scans with `nmcli --terse device wifi list --rescan yes` and converts NetworkManager's 0–100 signal back to dBm on NetworkManager's own linear scale. It stages a join as a keyfile profile of its own with `autoconnect=false`, then runs `nmcli --wait 40 connection up` on it, which leaves five of the join's 45 seconds for staging the profile and cleaning up after it. NetworkManager's state reason gives the failure reason. On success it deletes the other profiles for that SSID and turns autoconnect on for the new one. On failure it deletes only the staged profile. `configure_wifi` moves here and `firstboot.sh` sources `wifi.sh`, but the function as it stands cannot serve a join from the phone, and it is generalised on the way:

- `key-mgmt` follows `SECURITY`: `wpa-psk` for `psk`, `sae` for `sae`, and no `[wifi-security]` section for `open`. Today it always writes `wpa-psk`, which fails on a WPA3-only network and on an open one.
- A hidden network gets `hidden=true`.
- The SSID is written as a keyfile byte list (`ssid=83;116;117;…;`), since it arrives as hex and may hold any byte.
- The passphrase is escaped for GKeyFile: `\` as `\\`, and a leading space as `\s`. Written raw, as today, a passphrase with a backslash or a leading space reaches NetworkManager changed and fails as a wrong password.

All but the hidden flag are defects in `firstboot.sh` as it ships, reachable from `kalinka-firstboot.conf` today: it also writes the SSID raw, so an SSID with a `\`, a leading space, or only digits and `;` reaches NetworkManager changed. They are fixed on their own, ahead of the plan (§12), and the backend builds on the fixed function.

The **`dietpi`** backend, for the Pi images, drives DietPi's tools the way `kalinka-soundcard.service` does. Before a scan and before a join it sets the running radio's domain with `iw reg set`. Only once a join has succeeded does it store the country with `dietpi-set_hardware wificountrycode`, which persists it. It stages a join in `wpa_supplicant`'s running configuration through `wpa_cli` (`add_network`, `select_network`) without saving it, and watches for the result of the four-way handshake, and `wlan0` for a DHCP lease. On success it adds the network to DietPi's Wi-Fi database with `dietpi-wifidb`, in place of an entry with the same SSID. On failure it removes the staged network and has `wpa_supplicant` reread its saved configuration. Issue 1 pins down the exact calls.

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

Everything except `gatt.py` is tested with the standard library's `unittest` under `make image-test`. That container has `python3` but neither `pytest` nor `dbus-fast`, which is why D-Bus stays at the edge. [image-pr.yml](../.github/workflows/image-pr.yml) runs `make image-test` on every pull request that touches `packages/kalinka-image`, so a pull request that breaks the codec or the state machine fails its own checks.

The daemon logs the phases it passes through and the SSIDs it joins. It never logs a command's payload.

### 5.3 When it runs

Both images ship the same units, the PC image from issue 8. `bluetooth.service` is disabled at build time, because `bluez`'s postinst enables it.

| Unit | Kind | Job |
|---|---|---|
| `kalinka-provision-check.service` | enabled, `Type=exec` | `After=dietpi-preboot.service dietpi-firstboot.service kalinka-firstboot.service`, as `kalinka-soundcard.service` waits for DietPi; the base's absent units are ignored. Only then has DietPi imported the card's `dietpi.txt` into `/boot/dietpi.txt` and applied `dietpi-wifi.txt`, or `firstboot.sh` applied `kalinka-firstboot.conf`. Earlier, the check would read the build's copy of `dietpi.txt` and see no Wi-Fi configured on a card that has one. Runs `provision-needed.sh`, and starts `kalinka-provision.service` without waiting if setup is needed. With `Type=exec`, its wait for a route holds up no target. |
| `kalinka-provision.service` | static | `Wants=` and `After=bluetooth.service`; runs `python3 -m kalinka_provision` with `PYTHONPATH=/usr/lib/kalinka-image` |
| `bluetooth.service.d/kalinka.conf` | drop-in | `StopWhenUnneeded=yes`: `bluetoothd` runs exactly as long as something wants it. That is the daemon, or `bluetooth.target` for someone who enabled Bluetooth for their own devices. |

`provision-needed.sh` decides, in this order:

1. Switched off (§5.7): no.
2. A default route exists: record that the box has had a network, and no.
3. The box has had a network before: wait up to five minutes for a default route, and if one comes, no. A router that lost power with the box takes minutes to hand out addresses again, and a shorter wait would open setup after nearly every power cut.
4. The box has never had a network: wait up to 20 s for a default route if an Ethernet interface has carrier, or 30 s if a Wi-Fi network is configured, and if one comes, no. A card set up with the wrong password therefore still ends in setup.
5. No Bluetooth adapter after waiting up to 15 s for one to appear: log one line, and no. On a Pi the radio sits on a UART and registers as `hci0` a few seconds into boot, and on a box with nothing configured no earlier step waits, so without this wait the check could find no adapter on exactly the box it exists for. `bluetooth.service` would be skipped too, by its own `ConditionPathIsDirectory=/sys/class/bluetooth`. This step covers a PC without an adapter and a Compute Module without wireless.
6. Otherwise, yes.

The record is `/var/lib/kalinka-image/networked`. The daemon writes it too, when a route arrives while it runs. Re-flashing the card clears it. A maker who test-boots a box re-flashes it before shipping anyway, since the server's state from the test would otherwise ship too, and the buyer's box starts as one that has never had a network.

On a box that has a network, the only cost is that shell check, and `bluetoothd` never starts. Neither unit has a `Before=` on `kalinka.service` or the renderer, so neither delays them. On a box that has never had a network, `bluetoothd` and the daemon stay until it is set up. Their memory is not missed there, because nothing else on the box works without a network.

Once running, the daemon advertises and exits on the first of these:

- the box gets a default route by itself (a cable plugged in, a router that came back after a power cut) while no phone is connected and no join has been started. The route a join brings does not count, so that exit cannot pre-empt the reconnect below;
- on a box that has had a network, 15 minutes have passed since it started and no join is running. Commands do not extend that window, so a device in range cannot hold setup open, or keep the box from its returning router, by writing now and then. A box that never had one does not stop for idleness. There, 15 minutes without a command only drop the device that holds the box (§5.4), so a device that connects and then idles cannot keep other phones out;
- the phone that joined the box has read the `connected` status, or 120 s have passed since the join.

It keeps advertising until the phone has read `connected`. A phone whose link dropped during the join (the Pi's Wi-Fi and Bluetooth share one chip) can then reconnect and read the result. On the way out it withdraws the advertisement. It keeps the pairing, so that the same phone can set the box up again later (§5.5). `bluetoothd`, no longer wanted, stops too. A box that is on its network runs no Bluetooth process.

A box that later loses its network, because the router was replaced or the password changed, finds no route at its next boot. After its five-minute wait it offers setup for 15 minutes. Moving house needs no card and no login.

### 5.4 The GATT contract

One primary service, `7c8e0001-1b2f-4e6a-9d3c-4b616c696e6b` (the last six bytes spell `Kalink`). The advertising data carries the flags and this 128-bit UUID, 21 of its 31 bytes. The local name, `Kalinka-` followed by the last four hex digits of the Bluetooth adapter's public address, goes in the scan response, which the phone's active scan reads. The app filters its scan by the service UUID. The address belongs to the radio, so the name is the same at every boot, on both images, and across a re-flash, and the maker of a ready-made box can print it on the label. `/etc/machine-id` would not do: `firstboot.sh` empties it at the PC image's first boot, so every PC box would hash the same empty file on that boot and take another name from the next.

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

`state` is one of `idle`, `scanning`, `joining`, `connected` and `failed`. `reason` is set only with `failed`, to `wrong_password`, `not_found`, `no_address`, `timeout` or `error`. `ssid`, `ipv4` and `port` are set with `connected`. An SSID that is not valid UTF-8 is sent as `ssid_hex` in place of `ssid`, as in the networks list. `port` comes from the server's configuration, and is 8000 unless someone changed it. `server_id` is `null` until the server has written it at its first start, which on a first boot may come after the join (§1.2), so the app never depends on it. `model` is `/proc/device-tree/model`, or the DMI product name on a PC.

```json
{"total": 23, "offset": 0,
 "networks": [{"ssid": "Studio", "rssi": -48, "secure": true},
              {"ssid": "Office", "rssi": -71, "secure": true, "enterprise": true},
              {"ssid_hex": "e9636f6c65", "rssi": -80, "secure": false}]}
```

Networks are sorted by signal, strongest first, with one entry per SSID. A page holds as many as fit in 512 bytes. An SSID that is not valid UTF-8 is sent as `ssid_hex`, and `join` accepts either form.

```json
{"op": "scan", "country": "GB"}
{"op": "page", "offset": 8}
{"op": "join", "ssid": "Studio", "psk": "…", "country": "GB", "hidden": false}
```

`scan` sets the country and refreshes the list, with `scanning` then `idle` reported through status. The phone knows its region from the start, so even the first scan hears every channel the country allows (§5.1). `page` moves the networks window. `join` starts a join, and carries the country too, as the one the box keeps. A command that is malformed, too long or unknown is answered with status `failed` and reason `error`. A command sent while a join is still running is refused with BlueZ's `org.bluez.Error.InProgress`, and the status stays as it was.

On Android the app asks for an MTU of 517 before its first write. iOS and Windows let the app request no MTU and pick one themselves, and any link may settle lower, so a command can arrive as a long write. BlueZ joins the pieces of a long write before it calls `WriteValue`, so the daemon receives the whole command at `offset` 0, and it answers a write at any other offset with `failed`/`error`. The 512-byte cap applies to the whole command. A value longer than one packet is read in pieces too, as `ReadValue` calls with rising offsets. The daemon answers those from the value it returned at offset 0, so a status that changes mid-read never arrives torn. A notification is cut at the link's MTU, so the app treats every notification as a cue to read `status`, which always returns the whole value.

The first device to write a command holds the box until it disconnects, or until 15 minutes pass without a command from it. Commands from any other device get `InProgress`.

### 5.5 Pairing, and what it protects

`command` is marked `encrypt-write`, so the phone has to pair before its first command. The daemon's agent is `NoInputNoOutput`, which makes that pairing Just Works: Android and iOS each show their own prompt to pair with Kalinka-3F2A. The first command is `scan`, sent as soon as the phone connects, so the prompt appears before anyone has typed a password.

LE Secure Connections is used wherever it is available. Every supported Pi from the 3B+ and the Zero 2 W up has a Bluetooth 4.2 or newer radio. Linux's SMP does the key agreement in software, so the original 3B's 4.1 controller may manage it too. Issue 1 records the pairing method `btmon` reports on each board. Where only legacy pairing is available, someone sniffing the air during those seconds could recover the link key. We accept that rather than add cryptography packages to both the box and the app (§8). Just Works also offers no protection against an active attacker in range, which §2.5 accepts.

The passphrase is written once and can never be read back: no characteristic returns it, and the daemon never logs it. `wifi.sh` passes it on standard input. On disk it ends up only where NetworkManager or `wpa_supplicant` keep it, in root-only files.

A pairing has two sides, and either can be lost alone. Re-flashing the card loses the box's side. The Bluetooth address survives, and with it the phone's side. Android has no public call to remove a pairing and iOS has none at all, so the app cannot clear the phone's side. The box therefore never removes its own side, and the same phone sets the box up again with the key both still hold. The reverse case needs a setting of its own. A phone that has lost its side, because the user forgot the box in its Bluetooth settings or the app sent them there by mistake, pairs afresh with a box that still holds a key for it. BlueZ's default, `JustWorksRepairing = never`, refuses that, and since the box keeps its key, the phone would stay shut out until a re-flash. The images therefore set `JustWorksRepairing = always` in `/etc/bluetooth/main.conf`. That lets no one in who could not already pair, because the box accepts pairing only while setup is open. A stale pairing is left only where the box cannot help it: after a re-flash, or a key lost when the daemon dies mid-pairing. The phone's encrypted write then fails until the user forgets the box in the phone's Bluetooth settings. The app recognises that failure and says so (§6.2), and issue 1 records how it shows on each platform (§10).

### 5.6 Image build changes

- **Pi images (issue 4):** install `bluez`, `python3-dbus-fast`, `pi-bluetooth` and Raspberry Pi's `bluez-firmware`, plus `iw` and `wpasupplicant` if DietPi's image lacks them. Disable `bluetooth.service`, set `JustWorksRepairing = always` in `/etc/bluetooth/main.conf` (§5.5), enable `kalinka-provision-check.service`, and write `KALINKA_WIFI_BACKEND`. The build takes no recommends, and `bluez` and `python3-dbus-fast` declare none in trixie, so each brings its dependencies and nothing more.
- **PC image (issue 8):** install `bluez` and `python3-dbus-fast`, with the same units and the `nm` backend.
- **The radios on the Pi:** switch Bluetooth and the Wi-Fi modules on with `dietpi-set_hardware` in the chroot, if that works there. Otherwise the build edits `config.txt` and `modprobe.d` itself, the way `keep_logs_on_disk` edits `.installed`. The build must also stop DietPi's first boot from switching the Wi-Fi modules back off on a card that asks for no Wi-Fi (§10). `verify_dietpi_setup` checks the result: no `disable-bt` overlay, no module blacklist, and the units enabled and disabled as intended.
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

The provider's phases are `unsupported`, `needsPermission`, `bluetoothOff`, `scanning`, `found`, `connecting`, `pairing`, `stalePairing`, `incompatible`, `listingNetworks`, `choosingNetwork`, `enteringPassword`, `joining`, `joined`, `reaching`, `failed(reason)` and `done`. It gives up on a scan after 20 s with nothing found, and on a join after 60 s without a verdict from the box.

Android needs `BLUETOOTH_SCAN` (with `usesPermissionFlags="neverForLocation"`) and `BLUETOOTH_CONNECT`. Android 11 and older also need `BLUETOOTH`, `BLUETOOTH_ADMIN` and `ACCESS_FINE_LOCATION`, all with `maxSdkVersion="30"`, and location services switched on before a BLE scan returns anything. The app asks for these when the user opens the flow, never at launch. On Linux and Windows the entry appears only when an adapter is present. It does not appear in the browser player (§8 weighs a Web Bluetooth page).

iOS needs more than a Bluetooth permission, because the hand-off and discovery both reach the box on the local network:

- `NSBluetoothAlwaysUsageDescription` in `ios/Runner/Info.plist`. iOS asks for Bluetooth the first time the flow scans.
- The Local Network permission, which iOS 14 and later ask for before the app's first connection to a local address: `NSLocalNetworkUsageDescription`, and `NSBonjourServices` listing `_kalinkaplayer._tcp`. The app's `Info.plist` has neither key today, nor any Bluetooth key.
- Discovery uses `multicast_dns`, which opens raw multicast sockets. iOS 14 and later allow those only with Apple's multicast entitlement. Apple grants it on request, and tells an app that only browses Bonjour services to use the Bonjour APIs instead. On iOS, discovery therefore browses through `NWBrowser`, behind a small method channel in the Runner, chosen in `discovery_provider.dart` the way the web stub is chosen today. That needs no entitlement and no new package. It concerns discovery in general, not only this flow, so it is its own issue (§12, issue 7), and the flow waits for it on iOS.
- `NWBrowser` reports a declined Local Network permission as a policy error. The app uses that to tell the permission apart from a network that keeps devices apart (§6.3).
- iOS picks the MTU itself, so the long write of §5.4 applies as it does on Windows.
- iOS offers no way to remove a pairing, and the flow is built for that (§5.5).

### 6.2 Screens

The flow lives inside the wizard's step 0. `DiscoveryScreen` gains an `onSetUpNewBox` callback. `OnboardingScreen` then shows the flow in place of discovery, inside the same 240 ms `AnimatedSwitcher`. System back walks back through the flow, and from its first screen it returns to discovery.

The five screens use `OnboardingStepScaffold` with one addition: an optional `eyebrow`. It replaces the "STEP n OF m" line and the stepper, while the progress bar follows the flow. Three pieces are pulled out of existing screens so both places draw the same thing:

- `ScanningArt`, from `DiscoveryScreen._buildScanningArt`, with the icon as a parameter;
- `SignalBars`, from `_buildSignalBars`;
- `StepTimeline`, from `RestartOverlay._buildSteps`, with the steps passed in as data. `UpgradeOverlay._buildSteps` is a copy of it and switches to `StepTimeline` in the same change.

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
  - "Is it switched on? A new box waits for a phone until it's set up. A box that has been on a network before waits for its router for five minutes, then for a phone for 15. Switch it off and on again for another try."
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
- **Pairing out of date**: `WarningNote` (warning), "This phone still holds an old pairing with Kalinka-3F2A. Open this phone's Bluetooth settings, forget Kalinka-3F2A, then come back and try again.", with **Try again**. It shows when the encrypted write fails the way issue 1 records for a stale key (§5.5). Where a platform cannot tell a stale key from a refused pairing, **Pairing refused** carries the same instructions.
- **Unknown protocol major**: `WarningNote` (warning), "Kalinka-3F2A needs a newer version of this app.", with **Get the update**. Through `url_launcher`, it opens the app's store listing on Android and iPhone, and the releases page on the desktop.
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
Country    United Kingdom (GB)          Change
                                      [ Rescan ]
[ Back ]  [              Continue              ]
```

The country sits on this screen because the list depends on it (§5.4). It defaults to the phone's region and goes with the first `scan`. **Change** opens a `KalinkaBottomSheet` listing the countries, and the box rescans under the one chosen. The rows use the wizard's `RadioMark`, built like `step_output.dart`'s output rows: the whole row lights under the pointer, and a row that cannot be chosen sits at 0.45 opacity. Enterprise networks show at that opacity. When there are more networks than fit, the last row is **Show more** and sends `page`. When `model` names a Zero 2 W or a Pi 3 Model B (not the 3B+ or 3A+, which are dual-band), an `OnboardingNote` adds: "A Pi Zero 2 W or Pi 3 Model B hears 2.4 GHz networks only. If yours is missing, your router may offer a 2.4 GHz network under another name."

**4. Password**

```
Password for Studio
It goes to Kalinka-3F2A over the paired link, and
Kalinka never shows it again.

╭──────────────────────────────────────────────╮
│ Wi-Fi password                          👁   │  (kalinkaFieldDecoration)
╰──────────────────────────────────────────────╯
[ Back ]  [            Join Studio            ]
```

For **Other network…** a *Network name* field comes first, and the network is sent as hidden. For an open network the password field is not shown, and the subtitle reads "Studio is open: no password needed." On a `psk` network, and on a hidden one, the password must be 8 to 63 bytes, WPA's rule for a passphrase. A WPA3-only (`sae`) network takes a password of any non-empty length, since SAE has no such limit. Characters outside ASCII are allowed: routers accept them and the box passes the bytes through. A 64-hex-digit key is refused, because the box passes the passphrase through (§5.1) and a raw key cannot join a WPA3-SAE network. The field's error line says so in `statusOffline`.

**5. Joining**

```
Joining Studio
The first time, this can take a few minutes.

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
| No verdict in 60 s | Kalinka-3F2A stopped answering. It may have joined anyway, so Kalinka is checking with it. | Reconnects over Bluetooth for up to 30 s and reads the verdict, which the box holds for it (§5.3). With `connected` it carries on to the hand-off. Without a link, discovery, where the box shows up once its server has started |
| Joined, but this iPhone may not use the local network | Kalinka-3F2A is on Studio, but Kalinka isn't allowed to reach devices on your network. Turn on Local Network for Kalinka in Settings, then try again. | The app's page in Settings, then Reaching again. Shown when iOS reports the permission declined (§6.1) |
| Joined, but the phone can't reach it | Kalinka-3F2A is on Studio at 192.168.1.50, but this phone can't reach it. Is this phone on Studio too? Guest networks keep devices apart. | Reaching again, or **Enter address manually** |

### 6.4 Hand-off to the wizard

With `connected`, the provider calls `connectToServer` with the box's name, `ipv4` and `port`, held in memory as the wizard holds discovery's choice (`persistConnection: false`). The address is the key the hand-off uses. The daemon always knows it, while `server_id` may not exist yet (§5.4), and the host name is `kalinka` on every box (§1.2). On a first boot with no network, the join may be what starts the server (§1.2), so the wait includes a Zero 2 W's cold start. The call therefore retries its health check, `GET /server/modules`, at that address until the server answers, for as long as issue 1 measures from a join to that answer on a Zero 2 W's first boot, plus a margin. Until that measurement exists, the budget is three minutes. Meanwhile the timeline stays on **Reaching Kalinka**. On success it calls the wizard's existing `_onConnected`, which loads the config and moves to step 1, **Music sources**. Back from there goes to discovery, where the box is now listed like any other server.

The same card works from the server-switching overlay. There the hand-off connects and closes the overlay, and the existing check of the server's `oobe_complete` flag opens the wizard at **Music sources**.

## 7. Path C: the hotspot, and why not

On a box with no network, a hotspot would raise an access point named something like `Kalinka-Setup`, hand out addresses and answer every DNS query with its own address. The phone's captive-portal check would then open a page where the user picks a network and types the password. Moode and Volumio work this way. For Kalinka it costs:

- **Packages the image keeps out.** `hostapd` and `dnsmasq-base`. `dnsmasq-base` reached the PC image only as a recommends of NetworkManager, which the build no longer takes, and NetworkManager's own hotspot mode needs it too. Add `nftables` to send port 80 to wherever the page is served.
- **A privileged helper anyway.** The page's backend has to write network configuration, which the server must not be able to do (§2.4). The hotspot therefore needs the same root-side program as path B, plus a web endpoint in front of it.
- **A second UI.** The browser player is a Flutter web bundle of several megabytes, served by a server that may still be starting. Phones show captive pages in a stripped-down browser window that closes when the phone leaves the network. A hand-written HTML page would have to carry Kalinka's look separately and be kept in step with the app.
- **A join that drops the phone.** The Pi's `brcmfmac` cannot scan reliably while it runs the access point, so the list goes stale. Joining the chosen network means taking the access point down, which drops the phone in the middle of the conversation. A wrong password is reported only after the box has raised its access point again and the user has found and rejoined it. That is the loop the Moode and Volumio forums are full of. Android also offers to leave a network "with no internet" in favour of mobile data, and then the page does not load at all.
- **PC radios.** Some PC Wi-Fi cards refuse to act as an access point. Intel's, the commonest, will not start one on 5 GHz channels.

Its one advantage is that no app is needed. That buys less than it seems: a Kalinka user without the app uses the browser player, and the browser player cannot reach a box that is not yet on the network. With the app on the App Store, iPhones take path B like Android phones, so the hotspot no longer covers a phone that path B misses. The costs above stand on their own.

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
- **A Web Bluetooth page, for now.** `universal_ble` also runs on the web, so the app's web build, served from kalinkaplayer.com, could set up a box before anything is installed. The browser player a box serves cannot, since it comes from the box. This is deferred, not rejected. Web Bluetooth exists only in Chromium browsers, so not in Firefox and not on an iPhone. Whether Chrome raises the system's pairing prompt for an `encrypt-write` characteristic on each platform is unknown. And the page would make the project's site part of setup, although nothing would pass through it. The transport seam (§6.1) takes a Web Bluetooth implementation in place of the web stub and the box needs no change, so the page can follow once the app's flow has shipped.

## 9. Compatibility

- The REST/WebSocket API, the renderer protocol and the plugin SDK do not change, and `REST_API_VERSION` stays where it is. The BLE contract is new and additive, with its own `protocol` major version.
- **An old app with a new image:** a box that has never had a network advertises until it gets one. A box that has had one advertises for up to 15 minutes at a boot where its network does not come back. Nothing else changes.
- **A new app with an old image:** the card appears, the scan finds nothing, and the empty state points at the card files.
- **Imager:** old images keep ignoring its customisation. New Pi images honour both `custom.toml` and the `firstrun.sh` hooks. The card files (`dietpi.txt`, `dietpi-wifi.txt`, `kalinka-firstboot.conf`) stay the documented fallback, unchanged.
- **Protocol majors:** a box with an unknown major is listed, and the Connecting screen says the app needs updating (§6.2). A box ignores fields it does not know and answers unknown commands with `failed`/`error`.

## 10. Open questions for the spike

The Imager check answers question 1 on its own, ahead of the plan, because it concerns today's image. Issue 1 answers the rest on a Pi Zero 2 W, a 3B and a 5, with an Android phone and an iPhone, before the design is accepted. The PC image's side waits for issue 8.

1. What the current Imager writes for a custom image (`custom.toml`, `firstrun.sh` and `cmdline.txt`, cloud-init's `user-data` and `network-config`, or something else), and what today's image does with it. In particular, does the first boot stop in `kernel-command-line.target`? Do `/usr/lib/raspberrypi-sys-mods` and `/usr/lib/userconf-pi` stay unowned on DietPi?
2. DietPi's defaults: are Bluetooth and the Wi-Fi modules off? What are the exact `dietpi-set_hardware` calls, and do they run in the build chroot? Does DietPi's first boot switch the Wi-Fi modules back off when `AUTO_SETUP_NET_WIFI_ENABLED=0`, and if so, does setting it to 1 with an empty network database make the first boot wait?
3. `dietpi-wifidb`: does it accept a 64-hex key? Can the network be written where DietPi keeps it on the root filesystem instead of the FAT partition? Do hidden networks need `scan_ssid`? Can a join be staged in `wpa_supplicant`'s running configuration through `wpa_cli` without DietPi rewriting it underneath, and does rereading the saved configuration bring back the stored network after a failed one? Does `brcmfmac` honour a domain set with `iw reg set` for a scan, without `dietpi-set_hardware wificountrycode` storing it?
4. `pi-bluetooth` and Raspberry Pi's `bluez-firmware` on DietPi Trixie: where they come from and their arm64 sizes.
5. BlueZ, with an Android phone and an iPhone: does the name reach the phone in the scan response? Does `encrypt-write` trigger the pairing prompt on both? Which pairing method does each board use, according to `btmon`? Does the BLE link survive the Wi-Fi join on the Zero 2 W's shared chip? Does a long write from an iPhone or a Windows desktop reach the daemon as one `WriteValue` call at offset 0? After a re-flash, and after the daemon is killed mid-pairing, what does a write from a phone that kept its old key fail with, on Android and on iOS? Can the app tell that from a refused pairing, and does either system pair afresh by itself?
6. Measurements on the Zero 2 W, replacing the estimates of §3: resident memory of `bluetoothd` and the daemon, the `systemd-analyze critical-chain kalinka.service` change on a box with a network and one without, and the time from power to the box being visible on the phone. Also, on a first boot with no network: how long `network-online.target` holds `kalinka.service` back, and the time from a successful join to the server answering `GET /server/modules`, which sets the hand-off's wait (§6.4).
7. `universal_ble` on Android, iOS, Linux and Windows: scanning with a service filter, requesting an MTU where the platform allows it, pairing, the errors a stale pairing gives, and its licence against the app's. The fallback is `flutter_blue_plus` on Android and iOS, the two store platforms, which are the acceptance platforms anyway.
8. iOS: does a declined Local Network permission reach `NWBrowser` as a policy error on every iOS version the app supports, and does a connection to the box's address then fail in a way the app can tell apart from an unreachable host?

## 11. Acceptance tests

**The design:** the maintainer's review of this document, and issue 1's check that BLE advertising works from a cold boot with no keyboard, and that writes from an Android phone and an iPhone arrive. Both come before anything else is built.

**Path A:**

- A card written by the current Imager boots an `rpi234` onto the configured Wi-Fi, with the host name and the login applied and `custom.toml` gone.
- An Imager that writes `firstrun.sh` gives the same result after its one reboot.
- After first boot no passphrase is left on the FAT partition, and under `/etc` it is only in root-only files.
- `tests/test_imager_custom.sh` covers a full file, Wi-Fi only, user only, a hex key and a plaintext one, a hidden network, CRLF and a BOM, an SSID and a passphrase containing `'`, a malformed file left in place, and the shred.
- `test_firstboot.sh` still passes with `create_account` moved, and `make image-test` is green.

**Path B, box:**

- Recorder tests of `wifi.sh` on the `dietpi` backend, including the passphrase never reaching `argv`, the country set before a scan, and a failed join leaving a stored network with the same SSID as it was while a successful one replaces it. Issue 8 adds the same for `nm`, with the keyfile for an open, a WPA2 and a WPA3-only network, a hidden one, an SSID with a `;` or a non-UTF-8 byte, and a passphrase with a `\` or a leading space.
- `unittest` tests of the codec (the 512-byte cap, unknown fields, unknown ops, `ssid_hex`, `country` on `scan`) and of the state machine (join while joining, the one-phone lock and its idle release, every exit for a box that has had a network and one that never had one, the `networked` record, a notification sent for every state change).
- On real Pis:
  - a Zero 2 W that has never had a network is visible to the phone within 45 s of power, and still is after 30 minutes;
  - a box that has had a network advertises for 15 minutes at most;
  - a box whose router comes back within five minutes of power never advertises, and one whose router takes longer stops advertising once it is back;
  - a correct password reaches the wizard's **Music sources** within issue 1's measured join-to-`/server/modules` time plus 15 s;
  - a wrong password is reported within 45 s, and a retry then works;
  - a wrong password sent for a network the box already has leaves that network working, and the box rejoins it when the router is back;
  - a hidden SSID joins, and a network on channel 12 or 13 is listed when the phone's country allows it;
  - the name stays the same across reboots and a re-flash;
  - the same phone sets up the same box a second time without forgetting anything, pairs with it again after forgetting it in the phone's settings, and after a re-flash the app shows **Pairing out of date**;
  - a box with a cable never advertises;
  - `kalinka-provision` and `bluetoothd` are gone within three minutes of joining;
  - the journal holds no passphrase;
  - `KALINKA_BLE_SETUP=0` keeps the radio silent.
- In issue 8, on the PC image, a USB adapter provisions it, and without one the unit exits 0 with one log line.

**Path B, app:**

- Provider tests against a fake transport for every phase and failure reason, the 20 s scan and 60 s join timeouts, the Bluetooth reconnect after a lost verdict, the hand-off's wait for a server still starting, a stale pairing, a declined Local Network permission, and an unknown protocol major.
- Widget tests for the card; for Find your box scanning, found, empty, permission and Bluetooth off; for Connecting's pairing-refused, pairing-out-of-date and incompatible states; for the network list with enterprise and paged entries and the country's rescan; for the password field's validation; and for Joining's success and each failure's copy, the Local Network one included.
- Manual runs on Android 12 or newer, on Android 10 for the location path, on an iPhone with Local Network allowed and declined, and on a Linux desktop.

**Compatibility:** an old app against a new image and a new app against an old image behave as §9 says.

## 12. Plan

Two pieces of work come first and stand on their own, because they concern the images as they ship today:

- **The Imager check** (KalinkaPlayer, a few days): flash an `rpi234` and an `rpi5` card with the current Imager, customisation set, and answer §10's first question. If the first boot hangs, that is fixed in today's image straight away. What Imager writes, cloud-init files included, feeds issue 2.
- **The `configure_wifi` fixes** (KalinkaPlayer, a few days): `key-mgmt` from the network's security, the SSID written as a byte list, and the passphrase escaped for GKeyFile, in `firstboot.sh` as it stands, with cases in `test_firstboot.sh` (§5.1).

Each issue of the plan is about a week's work. Path B builds on the Pi images first. The Imager change (issue 2) waits only for the Imager check, and nothing waits for it.

| # | Repository | Issue | Needs |
|---|---|---|---|
| 1 | KalinkaPlayer | Spike: pin DietPi and BlueZ facts on real boards, with an Android phone and an iPhone; replace §3's estimates with measurements, the join-to-`/server/modules` time included | — |
| 2 | KalinkaPlayer | Honour Raspberry Pi Imager's customisation on the Pi images (§4) | The Imager check |
| 3 | KalinkaPlayer | `wifi.sh` with the `dietpi` backend: country on scan, staged joins (§5.1) | 1 |
| 4 | KalinkaPlayer | The provisioning daemon, its units and the Pi image build (§5.2–§5.7) | 3 |
| 5 | KalinkaAI | Provisioning transport and provider, on Android and iOS (§6.1) | 1 |
| 6 | KalinkaAI | Wi-Fi setup screens and the hand-off to the wizard (§6.2–§6.4) | 4, 5; 7 on iOS |
| 7 | KalinkaAI | iOS: discovery through `NWBrowser`, and the Bluetooth and Local Network usage strings (§6.1) | — |
| 8 | KalinkaPlayer | Bluetooth setup on the PC image: the `nm` backend and the units on the Debian base (§5.1, §5.6) | 4, the `configure_wifi` fixes |

The Network page for changing Wi-Fi later (§2.6) is filed apart from this plan.

Relevant existing code: [firstboot.sh](../packages/kalinka-image/overlays/debootstrap/usr/lib/kalinka-image/firstboot.sh), [base-dietpi.sh](../packages/kalinka-image/lib/base-dietpi.sh), [base-debootstrap.sh](../packages/kalinka-image/lib/base-debootstrap.sh), [dietpi-conf.sh](../packages/kalinka-image/overlays/dietpi/usr/lib/kalinka-image/dietpi-conf.sh), [soundcard.sh](../packages/kalinka-image/overlays/dietpi/usr/lib/kalinka-image/soundcard.sh), [the image README](../packages/kalinka-image/README.md), [image-pr.yml](../.github/workflows/image-pr.yml), [service_discovery.py](../packages/kalinka-server/src/kalinka_server/service_discovery.py), [server_identity.py](../packages/kalinka-server/src/kalinka_server/server_identity.py), [kalinka.service](../packages/kalinka-server/scripts/kalinka.service), and in the app [onboarding_screen.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/screens/onboarding_screen.dart), [discovery_screen.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/widgets/discovery_screen.dart), [discovery_notifier_io.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/providers/discovery_notifier_io.dart), [Info.plist](https://github.com/Kalinka-Player/KalinkaAI/blob/main/ios/Runner/Info.plist), [onboarding_step_scaffold.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/widgets/onboarding/onboarding_step_scaffold.dart), [restart_overlay.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/widgets/restart_overlay.dart), [upgrade_overlay.dart](https://github.com/Kalinka-Player/KalinkaAI/blob/main/lib/widgets/upgrade_overlay.dart) and [PALETTE.md](https://github.com/Kalinka-Player/KalinkaAI/blob/main/PALETTE.md).

Platform references: [BlueZ GATT API](https://github.com/bluez/bluez/blob/master/doc/org.bluez.GattCharacteristic.rst), [BlueZ LE advertising API](https://github.com/bluez/bluez/blob/master/doc/org.bluez.LEAdvertisement.rst), [dbus-fast](https://github.com/Bluetooth-Devices/dbus-fast), [universal_ble](https://pub.dev/packages/universal_ble), [Android Bluetooth permissions](https://developer.android.com/develop/connectivity/bluetooth/bt-permissions), [Apple's TN3179, local network privacy](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy), [Raspberry Pi Imager](https://github.com/raspberrypi/rpi-imager) and [DietPi](https://github.com/MichaIng/DietPi).
