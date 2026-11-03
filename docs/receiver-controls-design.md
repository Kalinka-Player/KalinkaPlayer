# Receiver connections and authentication

Implemented design for Core API 0.23 / SDK 3.23 and the companion app.
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
Bluetooth's primary actions are Pair a device, Stop pairing and Disconnect.
Saved devices are secondary, with paired status and audio permission shown
separately. Closing any presentation surface sends no receiver command.

## Plugin boundary

`ReceiverDescriptor` identifies the protocol and supported commands. An optional
`ReceiverControl` supplies snapshots and executes bounded commands. Core owns
routing and event delivery; plugins own protocol state, subprocesses, Bluetooth
trust and authorization. Existing input and playback interfaces do not change.

The additive fields and commands are documented in
[receiver-control-api.md](receiver-control-api.md). The app renders only actions
advertised by the plugin. There is no asynchronous-operation registry, generic
plugin lifecycle coordinator, or shared pairing implementation across protocols.

A pending agent callback is input awaiting a decision, not a long-running HTTP
request. Its snapshot has an opaque request ID, requesting device name, actual
callback method, supplied code where applicable, and remaining lifetime.
A synchronous `answer_pairing` command consumes that request ID once. Other
clients, late responses and replayed responses cannot approve a replacement.

## Bluetooth pairing

**Pair a device** explicitly opens a backend-owned 120-second window. Reopening
an existing window returns it without extending its deadline. The backend uses
a monotonic clock, the BlueZ discoverable timeout and its own cleanup timer.
Clients display the remaining seconds using elapsed time, rather than comparing
wall clocks. Window expiry closes admission; it does not disconnect established
allowed audio. Closing a panel does not close the window.

The plugin takes the existing shared adapter lease before changing adapter-wide
properties or registering the default agent. It registers **DisplayYesNo** to
support numeric comparison and restores the adapter state and lease when done.
Provisioning retains its existing GATT implementation and lease coordination.
Audio access does not remove provisioning bonds or power off the adapter.

Only calls from the actual BlueZ bus owner are accepted. The callback determines
what the user sees:

| BlueZ callback | Presentation and response |
|---|---|
| `RequestConfirmation(device, passkey)` | The requesting device and supplied six-digit, zero-padded code. **Check this code matches your device**; **Reject / Codes match**. The D-Bus reply waits for the explicit decision. |
| `RequestAuthorization(device)` | Explicit approval without a code. Never present a fabricated comparison code. |
| `RequestPinCode` / `RequestPasskey` | Ask for the device's actual PIN/code and return the user's validated entry. Never prefill or generate it. |
| `DisplayPinCode` / `DisplayPasskey` | Show the supplied code for entry on the other device, including entered-digit progress. This is not numeric comparison and has no Codes match action. |
| `AuthorizeService` | Only audio service UUIDs. Existing audio grants or an explicitly accepted candidate can proceed; otherwise request audio authorization. |
| `Cancel` / `Release` | Remove the request, reject unfinished work and clean up provisional access. Release also ends the agent's window. |

The app mounts an approval host outside the connection panel. A real pending
request opens a focused prompt even when the panel is closed. Backend
cancellation, expiry, disconnect or another client's response removes the prompt.
Disconnected snapshots cannot enable approval. Returning to the app refreshes
inventory. A request is bounded to 60 seconds or the window's remaining time,
whichever is shorter. Simultaneous requests do not replace an unanswered one.

Explicit acceptance allows the Bluetooth handshake and records provisional
audio admission. A successful paired A2DP PCM endpoint commits the durable audio
grant, closes the window and exposes the actual connected session. No audio
frames or renderer are required for that success signal. A bond alone is not an
audio grant. Existing granted phones reconnect without a new pairing window.

Before Kalinka sets `Trusted`, it records prior trust. Removing audio access
restores that baseline and disconnects the A2DP profile. Unauthorized transports
are rejected through BlueZ rather than playing into silence. Failed or expired
provisional grants are cleaned up, including after process restart. Already
allowed connections survive an unrelated pairing timeout.

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

Ship the SDK, Core, Bluetooth, AirPlay and app updates together for the full flow.
Older plugins omit the optional authentication fields. Older clients cannot
answer new Bluetooth requests; the backend times out instead of automatically
approving them. The new app refuses to start pairing against an older Bluetooth
controller without explicit approval support. No installed receiver means no
Connect entry.

Focused tests cover callback authenticity, leading zeroes, explicit decisions,
request IDs, concurrent responses, rejection, cancellation, expiry, retry,
window cleanup without audio disconnection, prior-trust restoration, password
configuration, private files, explicit reveal, demo refusal, and live restart.
Flutter checks cover mobile/wide placement, enabled filtering, compact panels,
approval outside the panel, offline/reconnect cleanup, password visibility,
large text and landscape layouts.

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

Primary protocol references: [BlueZ Agent API](https://bluez.readthedocs.io/en/latest/agent-api/)
and the [bundled Shairport Sync 5.5.2 configuration](https://github.com/mikebrady/shairport-sync/blob/5.5.2/scripts/shairport-sync.conf).
