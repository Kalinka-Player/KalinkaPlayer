# Box setup over BLE, first iteration

Implemented scope, updated 2026-10-06. This supersedes the provisioning parts of
[the earlier design](first-run-wifi-design.md). Bluetooth playback, monitoring,
diagnostics and recovery actions are future work.

## Flow

The image runs `kalinka-supervisor.service` independently of the player. After
30 seconds without a usable IPv4 address on a physical LAN interface that
carries the IPv4 default route, it advertises `Kalinka-XXXX`, where XXXX is the
Bluetooth adapter address suffix. An interface without that route, such as a
cable straight to an amplifier, does not keep setup closed when Wi-Fi breaks.
When no physical interface carries the default route (a LAN with no gateway,
a VPN or a separate routing table holding it), the Wi-Fi interface counts
instead, so a wired box on a LAN with no gateway advertises while a Wi-Fi one
does not.
On a box which has previously connected, the grace period is five minutes.
An internet outage does not trigger setup. A returning Ethernet or Wi-Fi
connection closes setup automatically when no join is in progress.

In the installed app, **Set up a box** is available alongside normal
network discovery, including when other players are visible. The wizard has
two steps: select a nearby box and press **Connect**, then choose its Wi-Fi
network. Accept the OS pairing prompt if shown. Selecting a network opens a
password form with the SSID displayed read-only; **Change** returns to the
list. **Enter custom SSID** is always available below the network list and
opens manual name/password entry. The footer keeps **Back** and the main
action above the keyboard. Back returns from password entry to the network
list, then to box discovery, then to the player list. Returning to box discovery
disconnects BLE and discards pending scan results. During a join, Back asks a
box that supports network changes to stop the attempt and reopen the network list.
The country defaults from the phone locale and can be edited under
**Advanced**; rescan after changing it. The list shows signal strength, groups duplicate
access points by SSID/security, and marks unsupported security types. It
shows up to the 30 strongest networks. Manual network-name entry remains
available for hidden networks, scan failures and older boxes without scanning.
The first version accepts UTF-8 SSIDs (1–32 bytes, without control characters)
and WPA2-Personal passphrases (8–63 printable ASCII characters). WPA2/WPA3
transition networks work through WPA2; open, WPA3-only and enterprise networks
are not supported. SSID/password whitespace is significant and is preserved.
Hidden SSIDs are supported by enabling directed scans on the box.

The app checks the encrypted BLE status before sending credentials, recovering
connections that dropped during password entry. Status and network-list reads
allow up to two fresh BLE reconnections before reporting failure; network-list
recovery resumes the same scan. If the box is already joining or joined, the
app resumes that result without submitting credentials again. A lost write
acknowledgement also triggers a status check, never an automatic credential
resend when delivery is uncertain. The box tries the network,
reports failure for a retry or returns its IPv4 address and port. The app waits
for the Core's HTTP API, checks its identity when available, and resumes the
existing connection/onboarding flow. It also repeats normal mDNS discovery,
matching `server_id`; the Core already notices new network interfaces. Apple
builds use native Bonjour browsing with Local Network permission.
Wi-Fi is one step of box setup: the normal connection path reads
`base_config.server.oobe_complete` and runs the existing first-run wizard when
the box reports it is new. Previously configured boxes go to the player.

The box reports progress over BLE: preparing, authenticating, getting an IP
address, saving, connected. After joining, the app waits for Kalinka to respond.
Each active step has a spinner. **Retry** appears only after a reported failure,
a connection error, or a timeout. The failed step shows a red cross and its
error message; all spinners stop. Completed steps retain their green ticks.
Retry checks the player again after a handoff failure, resends the entered
credentials after a confirmed Wi-Fi failure, or reconnects to check the box
when its result is unknown. **Back** returns to network selection; there is
no separate change-network link below the buttons.

Going back during a join cancels the attempt and waits for rollback
before reopening the picker. After a successful join, the saved connection
is kept while the user chooses a replacement; setup stays open for another
three minutes even though the box is online. This action requires BLE setup
to still be available. It does not send the previous password again; the app
clears that field when the picker reopens.

Network scanning happens on the box, so it does not require phone Wi-Fi scan
permissions and only lists networks visible to the box's radio. Phone SSID
prefill is not implemented. [Android Wi-Fi APIs](https://developer.android.com/reference/android/net/wifi/WifiConfiguration#preSharedKey)
do not expose saved passwords. [Apple's Wi-Fi Infrastructure sharing](https://developer.apple.com/documentation/wifiinfrastructure)
is a separate accessory integration with EU-only production availability; it
is not part of this flow.

The Core identity may not exist before its first startup. BLE exposes the
existing identity file when it appears, rather than creating a competing ID.
Setup remains available for three minutes after joining, including after a
BLE disconnect. A successful app handoff sends `complete` to close it sooner.
The app's join wait is 100 seconds; its subsequent handoff wait is 150 seconds.

The daemon remains idle while online and resumes advertising after a later
network loss. It unregisters only its own GATT application, advertisement and
pairing agent. It leaves BlueZ, the adapter and existing bonds in place, so
future Bluetooth playback can share the stack. It does not monitor Core;
restarting Core on request belongs to the supervisor's
[control API](supervisor-control.md).

## Try it on a Linux laptop

The test runs the actual BlueZ peripheral and encrypted GATT service, with a
fake Wi-Fi backend. It advertises even when the laptop is already online and
prints received SSID/country, with the password redacted. It never configures
the laptop's network or writes DietPi configuration.
The picker shows four explicitly labelled sample networks, including disabled
open/WPA3-only choices. Choose **Test network** and enter any valid dummy
password (for example `test12345`) to exercise selection and BLE transfer.
Test joining displays simulated stages for about three seconds. With no Core
running, the player check times out after 150 seconds, showing a red cross and
**Retry**. **Back** remains available during the wait to return to network selection.

From the KalinkaPlayer checkout:

```sh
make supervisor-build
packages/kalinka-supervisor/run-test.sh
```

BlueZ must be running and the adapter must support both GATT serving and LE
advertising. The current Fedora laptop successfully registered the service
and advertisement without sudo. On systems whose BlueZ policy requires root,
run the already-built launcher with `sudo packages/kalinka-supervisor/run-test.sh`.
The test temporarily uses the adapter's alias and default pairing agent;
exiting restores the alias and unregisters its own objects. Use a phone or a
second Bluetooth adapter as the client; the same adapter cannot test an
over-the-air connection to itself. Ctrl-C stops the receiver. The launcher
also serves the control API on port 8001, with simulated actions.

Useful options, passed after the script name:

```sh
--test-result wrong_password
--test-result no_address
--test-result timeout
--adapter hci1
--test-address 192.168.1.50 --port 8000
--server-id-file /path/to/server_id
--server-config /path/to/kalinka_conf.cfg
```

For the complete handoff, run a normal Core on the laptop, allow incoming HTTP
and mDNS on the LAN, and use its identity file and port. Otherwise receiving
the credentials is enough to verify BLE; the app will report that the player
cannot yet be reached. Test mode never creates a fake mDNS player.

Before the updated app is available, a GATT client can read encrypted status
to pair, then write the framed commands described below. In the app checkout,
`flutter run` includes the new flow on supported native platforms. The web
build keeps normal manual connection and does not expose nearby setup.

## Real Wi-Fi changes on a NetworkManager laptop

Use `packages/kalinka-supervisor/run-live.sh` for an end-to-end test that actually changes the
laptop's network. This is a separate backend for NetworkManager (for example,
Fedora); DietPi images keep their ifupdown/supplicant backend. Do not use
`--test` for this test. The live receiver advertises even while already online,
and the app shows real networks with no simulated-network badge. It turns the
control API off: run as root, that would reboot the laptop for real.

Start a Core in a dedicated local prefix, then point BLE at its identity and
configuration so the app can verify the network handoff:

```sh
KALINKA_PREFIX=/tmp/kalinka-live-e2e make dev-run
# In a second terminal; stop an existing simulated receiver first.
packages/kalinka-supervisor/run-live.sh \
  --interface wlp0s20f3 \
  --server-id-file /tmp/kalinka-live-e2e/var/lib/kalinka/server_id \
  --server-config /tmp/kalinka-live-e2e/etc/kalinka/kalinka_conf.cfg
```

Use your laptop's Wi-Fi interface name from `nmcli device status`. The logged-in
user needs NetworkManager permissions to scan, activate connections, and save
profiles (`nmcli general permissions`). The phone must be on the same LAN and
the laptop firewall must allow the Core's TCP port and mDNS. Enter credentials
in the phone's **SET UP A BOX** flow. The network switch can briefly interrupt
other laptop connections.

Credentials go directly to NetworkManager over system D-Bus, never shell
arguments or logs. The backend creates a separate in-memory profile, waits for
its activation and IPv4 address, then saves that profile. Existing saved
profiles are not modified or deleted. Failures and cancellations delete only
the new candidate and reactivate the previous profile. An interrupted attempt
is recovered on the receiver's next start using a credential-free rollback
record in its private persistent state directory. A successful join stays active after
the receiver exits. The laptop driver retains control of its regulatory domain;
the app's country field is not applied by this backend.

For manual restoration, use `nmcli connection up uuid <previous-connection-uuid>`.
Do not pass Wi-Fi passwords to shell commands. Profile staging/persistence use
NetworkManager's [AddConnection2 and Update2 flags](https://www.networkmanager.dev/docs/api/latest/nm-dbus-types.html).

## DietPi integration

The `rpi234` and `rpi5` image builds install the static Go supervisor, BlueZ, Pi
Bluetooth firmware/UART support, and the Wi-Fi tools. The build removes
DietPi's Bluetooth/Wi-Fi disable overlays and blacklists, enables the UART
initialisation and provisioning units, and checks for the noninteractive
`dietpi-network apply --no-restart` API. An older cached base lacking that API
fails the build and must be refreshed. The x86-64 PC/VM image now also uses
DietPi and the same native networking backend. NetworkManager remains supported
for installations on other distributions. All images install a separately
versioned `kalinka-supervisor` Debian package.

UART serial-console defaults are disabled so a login/kernel console cannot
claim the Bluetooth UART on the first boot. The local HDMI console remains
available. A custom GPIO/UART configuration may require disabling Bluetooth.

The provisioning unit runs after DietPi's initial configuration, without
waiting on `network-online.target`. Its static Go binary needs neither Python
nor the Core/plugin environment. Root permissions are confined to this service.
See [Supervisor architecture](supervisor.md) for build, isolation and future
recovery boundaries.

The backend configures the wireless interface for DHCP using DietPi's tool,
starts ifupdown's supplicant/DHCP path in the separate `kalinka-wifi@` unit
(so a supervisor restart preserves networking), and stages the candidate using
`wpa_supplicant`'s local control socket. It waits for the selected network to
complete authentication and acquire an IPv4 address. A failed attempt restores
the previous files and restarts the interface on its saved configuration.
Successful joins save the supplicant configuration, update the matching or
first empty DietPi database slot, and persist the country. Existing other
networks retain their enabled state. A full five-slot database reports a
storage error rather than silently deleting a network.

A durable rollback record under `/var/lib/kalinka-supervisor/provision-rollback`
allows the service to restore configuration after interruption. Credentials
never go into subprocess arguments or log output. The WPA2 key is derived
locally and saved in root-only files. Database entries are escaped as shell
literals without executing the database as shell code. The daemon saves the
supplicant's own encoding, including quoted/non-ASCII SSIDs; later manual
regeneration with DietPi's Wi-Fi UI is still subject to that tool's SSID
escaping limitations.

The advertised port comes from `base_config.server.port` in
`/etc/kalinka/kalinka_conf.cfg` (default 8000), or `--port`. Identity defaults to
`/var/lib/kalinka/server_id`. Use a systemd `ExecStart` override for a different
interface, adapter or server configuration. A physical Wi-Fi adapter is
required; boards without built-in radios need suitable USB adapters.

Set `KALINKA_BLE_SETUP=0` in the card's `dietpi.txt` before first boot, or stop
and disable `kalinka-supervisor.service`, to turn setup off. The switch controls
provisioning, not the Bluetooth hardware. Pairing uses encrypted Just Works:
proximity authorises setup, without authenticated proof of ownership. Pi
images allow Just Works re-pairing; after reflashing a box the phone may still
need to forget its stale bond in Bluetooth settings.

Before a join, scanning sets the requested regulatory country, unblocks Wi-Fi,
brings the wireless link up and uses `iw dev <interface> scan`. It does not
change saved profiles or start DHCP. Scans are bounded to 20 seconds and
serialized with joining; scan failure leaves manual entry available.

## Protocol v1

Service UUID: `7c8e0001-1b2f-4e6a-9d3c-4b616c696e6b`. Characteristics share its
suffix:

| UUID prefix | Name | Operations |
|---|---|---|
| `7c8e0002` | Status | Encrypted read; notify |
| `7c8e0003` | Identity | Encrypted read |
| `7c8e0004` | Command | Encrypted write with response |
| `7c8e0005` | Networks | Encrypted read, paginated scan results |
| `7c8e0006` | Progress | Encrypted read, join stage |

Every command is UTF-8 JSON, maximum 512 bytes:

```json
{"v":1,"op":"join","ssid":"Home","password":"dummy password","country":"GB"}
{"v":1,"op":"complete"}
{"v":1,"op":"scan","country":"GB"}
{"v":1,"op":"networks","page":1}
{"v":1,"op":"change_network"}
```

Split JSON into chunks of at most 19 bytes. Prepend one flags byte to each:
START=1, END=2; a single-chunk message uses 3, middle chunks use 0. Await each
write response. The receiver decodes only the complete message, allowing a
UTF-8 character to span chunks. START replaces that client's partial message;
incomplete messages expire after ten seconds. Disconnect also clears partial
messages. Another client receives `InProgress` while a message or join is held.
Join/result ownership survives a transient BLE disconnect. A write response
acknowledges reception, not a successful Wi-Fi join.

Status is ten bytes, all multibyte values in network byte order:

| Offset | Size | Meaning |
|---|---|---|
| 0 | 1 | Version, 1 |
| 1 | 1 | Capabilities: bit 0 WPA2 provisioning, bit 1 dry-run, bit 2 Wi-Fi scanning, bit 3 join progress, bit 4 change network |
| 2 | 1 | State: idle=0, joining=1, joined=2, failed=3, online=4 |
| 3 | 1 | Reason: none=0, invalid_request=1, wrong_password=2, no_address=3, timeout=4, unavailable=5, storage_error=6, busy=7 |
| 4 | 4 | IPv4 address, all zeros if unavailable |
| 8 | 2 | HTTP port |

Notifications contain only `01` (status changed); read status for the result.
Identity is the server UUID's 16 raw bytes, or empty while the Core has not
created it. Neither value needs a long read. The app currently polls status;
other clients can subscribe to notifications. No characteristic exposes the
password. Unsupported major versions are refused; unknown JSON fields are
ignored and unknown commands rejected. Future diagnostics/recovery can add
capabilities and commands without coupling this daemon to playback.

Send `scan` only when the scan capability is present, then poll Networks until
its `state` is `ready` or `failed`. A scan resets the selected page to zero.
The encrypted Networks characteristic returns UTF-8 JSON:

```json
{"v":1,"id":1,"state":"ready","reason":null,"page":0,"pages":2,"networks":[{"ssid":"Home","signal":-42,"security":"wpa2"}]}
```

States are `idle`, `scanning`, `ready`, `failed`; a failed scan reports reason
`unavailable`. Security is `wpa2` (including mixed WPA2/WPA3), `open`, or
`unsupported`. Each page contains at most three entries and fits the 512-byte
GATT attribute limit. Unlike Status/Identity, Networks uses standard GATT long
reads; the daemon freezes each reader's page across read offsets. Send
`networks` to select each subsequent page, then read Networks. Check the scan
`id`, requested `page` and `pages` throughout to reject a concurrent refresh.
There are at most ten pages. Scan/page commands never carry credentials.

When the progress capability is present, read Progress while Status is
`joining`. Its two bytes are version `1` and stage: preparing=0,
authenticating=1, getting_address=2, saving=3, connected=4. Status remains the
authoritative join outcome. Older boxes use the generic connecting message.

With the change-network capability, the current join owner may send
`change_network` during joining or before acknowledging a successful handoff.
Status stays `joining` while the backend cancels/restores, then becomes `idle`
when a new scan/join is safe. Further writes are rejected while restoration is
running. No saved network is removed by opening the picker.

Both backends allow up to 55 seconds for rollback. Clients wait at least 75
seconds after submitting `change_network` before reporting a reset timeout,
leaving 20 seconds for cancellation cleanup and BLE status polling. A client
must wait for the server's idle/failed status rather than treating elapsed
time as permission to send another join. Startup crash recovery has its own
deadline and completes before setup is advertised.

## Verification

`make supervisor-test` runs Go protocol conformance, lifecycle, persistence,
backend recorder, secret-redaction and D-Bus boundary tests with the race detector,
then `go vet`. `make image-test` checks image integration, including radio
configuration and service ordering. Captured Python packets in
`packages/kalinka-supervisor/testdata/python-v1.json` keep the app contract fixed.
The optional read-only host check is:

```sh
cd packages/kalinka-supervisor
KALINKA_TEST_HOST_NM=1 go test ./internal/wifi -run TestNMHostReadOnly -v
```

These tests exercise DietPi configuration and supplicant operations using
injected OS boundaries; they do not establish that a Pi's actual radio and
DHCP stack work. App tests cover retry, reconnect, cancellation and handoff.

Physical acceptance still requires a phone and a flashed Pi: encrypted
pairing/write; cold boot without networking; correct/wrong credentials; DHCP
failure; reconnect during joining; reboot after success; moving to an absent
network; and restoring saved networking after an interrupted attempt. Linux
tests cannot establish Pi firmware behaviour or validate iOS permission UI.
