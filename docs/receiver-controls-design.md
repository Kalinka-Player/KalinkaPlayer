# Receiver connections and authentication

Implemented design for Core API 0.24 / SDK 3.24 and the companion app.
This replaces the earlier large receiver panel design. PCM delivery, renderer
selection and playback ownership remain as described in
[the media design](bluetooth-audio-sink-design.md).

## Navigation and presentation

Use the existing plugin receiver inventory. Show **installed, enabled** receivers
only, independently of whether a public plugin catalog or browsing catalog is
available. Disabled plugins remain manageable in Configuration. Do not fabricate
entries for protocols that are not installed.

On mobile, **Connect** belongs in the existing music-source strip. It opens a
compact bottom sheet listing receivers. On wide layouts, individual receivers
appear below **Connect** in the existing sidebar and open anchored popovers.
There is no dedicated Bluetooth header button or large Home connection row.
Keep existing theme tokens, typography, playback controls and unrelated layout.
The receiver panel does not contain another output selector.

Show the effective advertised name, instructions and actual session state.
Bluetooth shows **Bluetooth · Pair**, followed immediately by the actual paired
devices. Each row shows Paired, Connected or Connected · Playing, with
**Disconnect** for an audio session and **Forget** for its saved pairing. There
is no second device-management page, expansion or audio-permission switch.
Closing any presentation surface sends no receiver command.

## Plugin boundary

`ReceiverDescriptor` identifies the protocol and supported commands. An optional
`ReceiverControl` supplies snapshots and executes bounded commands. Core owns
routing and event delivery; plugins own protocol state, subprocesses, Bluetooth
trust and authorization. Existing input and playback interfaces do not change.

The additive fields and commands are documented in
[receiver-control-api.md](receiver-control-api.md). The app renders only actions
advertised by the plugin. There is no asynchronous-operation registry, generic
plugin lifecycle coordinator, or shared pairing implementation across protocols.

The existing optional pairing-request contract remains available to controllers
that need explicit approval. The Bluetooth controller described here does not
publish requests: opening its bounded pairing window is the user's consent.
`pairing_mode: "just_works"` makes this policy explicit in its snapshot.

## Bluetooth pairing

**Pair** opens a backend-owned 120-second window. Reopening an existing window
returns it without extending its deadline. The backend uses a monotonic clock,
the BlueZ discoverable timeout and its own cleanup timer. Clients display
remaining seconds using elapsed time, rather than comparing wall clocks.
Window expiry closes admission; it does not disconnect established allowed
audio. Closing a panel does not close the window. **Stop pairing** names the
current window, so an old client cannot close a replacement window.

The plugin takes the existing shared adapter lease before changing adapter-wide
properties or registering the default agent. It registers **NoInputNoOutput**,
using Bluetooth's Just Works association instead of numeric comparison. The
phone may still show its standard pairing confirmation. Kalinka does not ask
for a second confirmation or show a code. This deliberately trades comparison
code verification for a short, explicitly opened admission window; proximity
and timing do not establish the sender's identity. The first successful new
pairing closes the window. No permanent auto-accept mode is introduced.

Only calls from the actual BlueZ bus owner are accepted:

| BlueZ callback | Response |
|---|---|
| `RequestAuthorization(device)` | Accept one candidate while the window is open; record provisional audio admission and prior trust. |
| `AuthorizeService` | Accept only supported audio service UUIDs during the window. |
| `RequestConfirmation`, PIN/passkey input or display | Reject unsupported association methods. Never fabricate or automatically confirm a numeric comparison code. |
| `Cancel` / `Release` | Cancel unfinished work and clean up provisional access; Release also ends the window. |

A fresh candidate becomes a saved audio device when BlueZ reports `Paired=True`
after its authorization callback completes. No song, PCM frames or renderer is
needed. Pairing and audio connection remain distinct states: a completed bond
is shown as Paired until its actual A2DP session connects. Failed/expired
candidates lose provisional access and have their previous trust restored.
Saved audio devices reconnect without another window or permission step.

Provisioning retains its existing GATT implementation and shared adapter lease.
A bond created only for provisioning is not silently granted audio access.
Opening Pair and making a fresh A2DP connection inside that window admits an
already-bonded setup phone, even if BlueZ bypasses its agent callback; the first
valid A2DP PCM endpoint commits that grant. Existing ungranted transports are
not adopted just because the window opens. Unauthorized transports are
rejected through BlueZ rather than playing into silence. Internal grant records
remain useful for this separation, but are not a second user-facing setting.

**Disconnect** disconnects only the A2DP profile and preserves the bond for
reconnection. **Forget** calls BlueZ `Adapter1.RemoveDevice`: it disconnects and
removes the actual Bluetooth bond, including any shared provisioning pairing.
Its confirmation explains this and that Wi-Fi configuration remains saved;
the phone may also need to forget its stale bond before pairing again. Forget
uses the shared adapter lease, refuses a device still being paired, and checks
the opaque peer handle against the current bond before removal. A timed-out
removal is never automatically replayed against a replacement bond. The plugin
keeps audio denied until the removal is observed or the user explicitly retries.
Neither disabling audio nor closing the pairing window powers off the adapter.

## AirPlay password

AirPlay has a persistent **AirPlay password** in its existing plugin settings.
An empty value means no password. The setting is declared with the existing
password widget, so normal configuration reads expose only whether it is set,
and credential logging protection applies. It is passed to Shairport Sync's
`general.password`, escaped as a libconfig string, in the existing private
0600 configuration file. It is not a temporary PIN or a Bluetooth approval.

Applying the setting restarts only the plugin's Shairport receiver and ends its
current AirPlay session. Its help text states this explicitly. The existing
`applies_live` marker means Core itself does not need to restart. Receiver
snapshots expose starting/error/ready state while the change is applied.

The connection panel shows the advertised name, same-network AirPlay
instructions and password-required status. **Show password** explicitly reads
the applied password through an optional `ReceiverPasswordControl`, using
`POST /receivers/{id}/password`. The response is `Cache-Control: no-store` and
never appears in receiver events or general configuration responses. **Hide
password**, closing the panel, losing availability or backgrounding the app
clears its displayed copy. Late responses after state/server changes are ignored.

This endpoint uses the existing management API access model, including demo-mode
write refusal. It is deliberately limited to a receiver's shareable connection
password; it cannot reveal arbitrary configuration credentials. Normal Kalinka
management clients are trusted clients on the streamer's network; this change
does not introduce a separate user/account permission system.

## Compatibility and verification

Ship SDK 3.24, Core API 0.24, Bluetooth and the companion app together for this
simplified Bluetooth flow. The existing AirPlay plugin continues to work.
`forget_device` and optional `pairing_mode` are additive. The old grant/revoke
commands remain accepted for compatible clients but are not advertised in
Bluetooth snapshots. The generic explicit-approval UI remains available for
older controllers and other plugins; it does not appear for Just Works pairing.
No installed, enabled receiver means no Connect entry.

Focused tests cover callback authenticity, window bounds, rejecting unsupported
association methods, candidate cancellation, success before PCM, saved-device
reconnection, provisional trust restoration, provisioning lease exclusion,
actual bond removal, replacement-bond races and failed-removal recovery.
Flutter checks cover mobile/wide placement, direct device rows, distinct
Disconnect/Forget actions, confirmation, countdowns, enabled filtering,
offline/reconnect cleanup, password visibility, large text and landscape.
Existing explicit-approval tests remain for compatible controllers.

The AirPlay companion also tests real classic RTSP password authentication in
the shipped ARM64 Shairport build: missing/wrong passwords fail, the configured
password succeeds, a new connection needs fresh authentication, and clearing the
password permits access. This required a narrow fix in the pinned Shairport AP2
build, whose initial AP2 requests otherwise pre-authorized later classic sessions.
The native CI check and patched corresponding source live in the AirPlay repo.

Hardware acceptance still needs real Android/iOS/desktop pairing callbacks,
BlueZ/provisioning coexistence, reconnecting saved devices, and both successful
and rejected password authentication from AirPlay senders. Unit tests cannot
establish sender interoperability or end-to-end audio behavior.

Primary protocol references: [BlueZ Agent API](https://bluez.readthedocs.io/en/latest/agent-api/),
[BlueZ Adapter API](https://bluez.readthedocs.io/en/latest/adapter-api/) and the [bundled Shairport Sync 5.5.2 configuration](https://github.com/mikebrady/shairport-sync/blob/5.5.2/scripts/shairport-sync.conf).
