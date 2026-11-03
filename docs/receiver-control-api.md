# Receiver control API v1

Implemented with Core REST/WS API **0.22** and plugin SDK **3.21**. This optional
control interface is independent of browsing, direct playback, PCM delivery and
renderer selection. Plugins without it retain their existing behavior.

## Inventory and commands

| Request | Result |
|---|---|
| `GET /receivers` | `{"version":1,"receivers":[ReceiverSnapshot, ...]}` |
| `GET /receivers/{receiver_id}` | One `ReceiverSnapshot` |
| `POST /receivers/{receiver_id}/commands` | `200 ReceiverSnapshot` after the bounded transition |

Inventory includes disabled receivers and receivers whose backend failed.
A successful empty list means no receiver is installed; a failed request does
not. Identity comes from the prepared plugin, never the public plugin catalog.

Example Bluetooth snapshot (opaque handles shortened for readability):

```json
{
  "version": 1,
  "receiver_id": "bluetooth",
  "title": "Bluetooth",
  "kind": "bluetooth",
  "instructions": "Select this name in your device's Bluetooth settings.",
  "enabled": true,
  "state": "ready",
  "message": null,
  "effective_name": "Kitchen Kalinka",
  "name_inherited": true,
  "visibility": "advertising",
  "pairing_mode": "just_works",
  "pairing": {"window_id": "window-handle", "remaining_seconds": 114.3},
  "recent_peer_id": null,
  "peers": [],
  "sessions": [],
  "actions": [{"command": "open_pairing", "available": true, "reason": null}]
}
```

`state` is `disabled`, `starting`, `ready`, `stopping` or `error`. `visibility`
is `hidden`, `advertising` or `unknown`; a running child process alone does not
confirm network advertisement. AirPlay currently reports unknown visibility
while enabled and its configured receiver name. mDNS collision-renaming is not
independently observed by this implementation.

Peer rows contain `peer_id`, `name`, `paired`, `audio_allowed`, `connected`.
Bluetooth lists actual paired BlueZ devices. `audio_allowed` remains available
for compatibility and provisioning isolation; the app does not expose it as
a separate permission control.
Session rows contain `session_id`, `name`, nullable `peer_id`, and `playing`.
AirPlay offers session information without pretending sender names are stable
device identities or exposing a device access list.

Supported command bodies:

```json
{"command":"open_pairing"}
{"command":"close_pairing","window_id":"window-handle"}
{"command":"forget_device","peer_id":"peer-handle"}
{"command":"disconnect_audio","session_id":"session-handle"}
```

Opening an existing window does not extend it. The window lasts at most 120
seconds. With `pairing_mode: "just_works"`, the plugin uses NoInputNoOutput and
accepts one candidate during that explicit window; it publishes no approval
request. A successful new BlueZ bond commits audio access and ends the window.
An already-bonded provisioning phone must establish a fresh audio connection
inside the window; its valid A2DP PCM endpoint then commits access. No song or
renderer is needed. Pending candidates are not saved grants.

Forget removes the actual BlueZ bond and associated grant, including any bond
shared with provisioning. It uses the adapter lease and rejects stale peer
handles if the bond has changed. Confirmation belongs in the client. Removal
failure leaves audio denied, but the destructive call is never automatically
replayed; refresh and explicitly retry if necessary. Disconnect preserves the
bond. Internal/legacy `grant_audio` and `revoke_audio` commands remain accepted
but are no longer advertised as Bluetooth actions. Revocation alone restores
known pre-grant trust and disconnects A2DP without removing the bond.

Close/disconnect target an opaque runtime handle and cannot affect a newer
window/session. A gone target is a no-op unless it conflicts with a replacement.
Commands reject extra fields. There is no operation polling endpoint, request-ID
cache or automatic replay of a timed-out POST. Refresh state and let the user
choose another action after an uncertain result.

Errors have `{"detail":{"code":"...","message":"..."}}`:

| HTTP | Codes |
|---|---|
| 400 | `unsupported_operation` |
| 404 | `receiver_not_found` |
| 409 | `plugin_disabled`, `adapter_busy`, `stale_target` |
| 422 | Invalid command body |
| 503 | `backend_unavailable` |
| 504 | `backend_timeout` |

The Core command budget is 15 seconds; snapshots have a two-second budget.
Settings callbacks retain their existing three-second budget. AirPlay owns its
configuration task so callback cancellation cannot interrupt child cleanup;
Bluetooth's admission watcher also reconciles a cancelled disable callback.
Protocol calls have their own bounds. Cancellation does not undo a durable
revocation: failed cleanup remains denied and is retried by the plugin.

## Live settings and events

Enable/disable and receiver-name overrides use the existing `PUT /server/config`
with its `schema_version` and flat `changes` object. Enable/disable remains in
Configuration; the connection panel only lists enabled receivers. Every plugin
field applies as it is saved: the plugin takes the change in through
`config_changed()`, returning `True`, or Core sets it up again on the new
settings. A client restarts only for a field whose schema entry says
`apply: restart_required`, which no receiver field does. `apply_immediately`
is separate: it still describes one-shot actions consumed on save.

Blank receiver names inherit `base_config.server.service_name`; explicit names
remain overrides. AirPlay name changes restart only its owned receiver and end
its current session. Bluetooth names reconcile under the provisioning adapter
lease. Disabling persists over Core restart; it is not a temporary mute.

The existing queue event connection adds:

```json
{"event_type":"receivers_changed","seq":42,"receivers":[]}
```

Replay state contains `receivers: {"version":1,"receivers":[...]}`. Clients
also GET on initial connection, reconnect, app resume and after commands. Use
response generations to reject older HTTP responses after a newer snapshot.
Count down from `remaining_seconds` using monotonic elapsed time, never the
Pi's wall clock. Offline state must not claim pairing remains open.

## Plugin integration

An input plugin declares a `ReceiverDescriptor` in `RECEIVER`, opts into
`SETUP_WHEN_DISABLED = True`, and returns its `ReceiverControl` from
`get_receiver_control()`. `snapshot()` reads cached state; `execute(command)`
serializes protocol changes and returns a snapshot or raises `ReceiverError`.

`InputPluginContext.receiver_changed()` requests a coalesced snapshot event.
`context.server_name()` reads the current streamer name; Core calls
`server_name_changed()` after a saved rename. The plugin handles live
`config_changed()` transitions and final `shutdown()` cleanup. Controller and
input registration remain stable while enabled changes. Setup success and
current module health remain separate.

Bluetooth keeps admission supervision while disabled if BlueALSA still exposes
A2DP, but opens no PCM stream and acquires no renderer. AirPlay starts no child
while disabled. The shared media path remains uncompressed PCM with bounded
live buffers; this API introduces no transcoding or media endpoints.


## Pairing requests and receiver passwords (API 0.22 / SDK 3.21)

Controllers that require explicit approval may include `pairing_request`: an opaque `request_id`, `device_name`,
`method`, optional string `code`, optional `entered` count, and monotonic
`remaining_seconds`. Methods are `confirmation`, `authorization`, `pin`,
`passkey`, `display_pin` and `display_passkey`, corresponding to actual BlueZ
callbacks. Numeric codes retain all six digits including leading zeroes.
The current Bluetooth controller uses `pairing_mode: "just_works"` and does not
implement `answer_pairing`. A controller using numeric comparison may declare
`pairing_mode: "confirm"`; absent mode leaves older controllers compatible.

`{"command":"answer_pairing","request_id":"opaque-id","accept":true}`
answers a comparison or authorization request. `accept` must be a JSON boolean.
A `pin` or `passkey` request additionally takes the user's `code` string;
comparison/authorization requests refuse a supplied code. Display-only requests
cannot be accepted: enter the shown code on the other device. All methods can
be rejected with `accept:false`. The request ID is consumed before yielding;
stale, expired or already-answered IDs return 409. No response is auto-replayed.
The HTTP command completes without waiting for the remote pairing handshake.

`password_required` and `password_reveal_available` are optional booleans.
Receivers implementing the optional `ReceiverPasswordControl` may opt into
`POST /receivers/{receiver_id}/password`, returning `{"password":"..."}` with
`Cache-Control: no-store`. This is an explicit reveal of an applied, shareable
receiver password, subject to the same management gate as configuration writes;
demo servers refuse it. It never reveals arbitrary configuration secrets and
never puts a password into inventory or WebSocket events. Disabled/unready
receivers refuse reveal. No temporary AirPlay PIN or pairing command exists.
