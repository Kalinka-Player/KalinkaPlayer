# Receiver controls and device management

Status: proposed, revised 2026-11-02 after review. The interfaces below are not implemented yet. This extends the [Bluetooth and AirPlay media design](bluetooth-audio-sink-design.md) without changing PCM transport, renderer selection or playback ownership.

## Decision and v1 scope

Add an optional **receiver-control capability** to input plugins. Core routes a small set of typed commands and publishes snapshots; the app adds a shared **Connect a device** panel. Plugins retain protocol behavior and permission enforcement.

Persistent settings are **Enabled**, a receiver-name override and optional supported codec preferences. Pairing, disconnecting audio and granting/revoking device access are immediate commands, outside staged settings. Enable/disable is a live write to the existing saved `enabled` field, including when invoked from the panel. There is no second, temporary availability switch.

Bluetooth v1 implements pairing windows, saved-device audio permissions and profile disconnect. AirPlay contributes identity, readiness and current-session information; enabling/disabling its plugin controls availability persistently. AirPlay has no pairing or per-device permission controls.

Keep `LiveAudio`, `DirectPlayback`, `ExternalPlayback` and the existing input interface unchanged. Receiver control is optional and independent of media delivery. Defer access requests, independent advertisement control and full bond removal until a backend implements them; do not specify their commands now.

| Approach | Tradeoff |
|---|---|
| Keep actions in settings | Smallest change, but transient operations remain mixed with saved values, staged edits and restart behavior. |
| Protocol-specific Core routes and app screens | Simple initially, but every receiver adds protocol logic throughout Core and the app. |
| Optional typed controller with synchronous commands | Recommended. Shared presentation and routing, with protocol policy inside plugins and no operation-tracking framework. |

## Code baseline and required corrections

Reviewed Core/SDK/Bluetooth/supervisor at `c1b918313f66e80c8e310ec9319fe61c2d82ff82`, AirPlay at `ad98729593e02377d9708a99a8fee60f593fbc81`, and the mobile worktree at `175b1b8`.

| Current code | Required change |
|---|---|
| Both Bluetooth grant paths set `Trusted=True`; revocation deletes an `AudioAccess` entry and filters its PCM. | Save prior trust, restore it on revocation, and disconnect unauthorized A2DP profiles. Silently dropping PCM is insufficient. |
| The supervisor changes the adapter alias and pairing agent, but does not set device `Trusted`. | Protect the bond and restore actual prior trust; do not assume provisioning owns a trust value created by audio. |
| The pairing agent saves a grant on its authorization callback. | Treat the callback as provisional admission; commit on the concrete A2DP success condition below. |
| `PreparedPlugin.setup()` skips disabled plugins. Bluetooth's `config_changed()` otherwise handles enabled changes. | Opt into setup while disabled; retain the controller while its audio runtime is stopped. |
| AirPlay starts its receiver during setup and has no `config_changed()`. | Separate controller initialization from receiver start/stop; implement live enabled/name changes. |
| Home sources depend on `catalogs` and catalog loading. | Add an independent entry point driven by installed receiver descriptors. |
| Bluetooth exists on the feature branch, with no containing release tag. Test images are already installed. | Ship the controller in its first release; migrate test-image data without a public one-shot API compatibility period. |

References: [SDK](../packages/kalinka-plugin-sdk/src/kalinka_plugin_sdk/plugin.py), [setup/health](../packages/kalinka-server/src/kalinka_server/player_setup.py), [config notifications](../packages/kalinka-server/src/kalinka_server/config_route.py), [Bluetooth plugin](../packages/kalinka-plugin-bluetooth/src/kalinka_plugin_bluetooth/__init__.py), [pairing/access](../packages/kalinka-plugin-bluetooth/src/kalinka_plugin_bluetooth/pairing.py), [PCM endpoint discovery](../packages/kalinka-plugin-bluetooth/src/kalinka_plugin_bluetooth/backend.py), [supervisor BlueZ ownership](../packages/kalinka-supervisor/internal/gatt/bluez.go). AirPlay references are the inspected sibling checkout's `__init__.py`, `receiver.py` and `shairport.py`.

## User experience

### Add entry points to existing app surfaces

Add a labelled **Connect a device** action beside Home's music sources. Introduce a receiver entry widget in `SearchSessionView._buildZeroState` / `SearchZeroState`, alongside `HomeSourceList` (`lib/widgets/search/search_session_view.dart`, `lib/widgets/search/search_zero_state.dart`, `lib/widgets/home/home_source_list.dart`). Keep it outside the catalog-loading and `showSources` guards so it remains accessible when the navigation rail replaces the source list.

Add a **Connect a device** row to both `ServerSheetContent` and `_TabletServerSheetContent` in `lib/widgets/server_sheet.dart`, using a new `ServerSheetAction` handled by `MusicPlayerScreen`. This is a new row in the existing server sheet. Reuse `showKalinkaBottomSheet` on phones and `SlideInPanel` on larger screens. Both entries open the same panel for the connected Core.

No new active-input badge is required for v1. A later shortcut would be an explicit addition to `MiniPlayer` / `NowPlayingContent`; the existing `SourceBadge` and stream-info tap target are not receiver-management controls.

Show the entry for any installed receiver, including disabled or unhealthy plugins. Hide it when a successful inventory fetch finds none; installation remains accessible through existing Configuration/plugin catalog navigation. If the last known inventory contained receivers but the connection fails, retain the entry with unavailable/retry state. A failed fetch is not an empty inventory. Visibility is independent of the public catalog being enabled or reachable.

```text
Connect a device
Receiving on: Kitchen Kalinka
Play through: Living-room renderer                 Change

Bluetooth
Visible as “Kitchen Kalinka” when pairing is open
[Pair a device]                          [Manage devices]

AirPlay
Available on this network as “Kitchen Kalinka”
[How to connect]                         [Disable AirPlay]
```

The names match because both receivers inherit the streamer name. Show their actual effective names if overrides or backend limitations make them differ. A disabled row offers **Enable Bluetooth/AirPlay**; an unhealthy row explains the failure.

Opening the panel or pairing window does not claim playback, acquire an output or interrupt the queue. Show output availability separately and reuse the existing renderer picker. Changing output does not change which host receives Bluetooth or advertises AirPlay.

### Bluetooth pairing and device management

1. **Pair a device** obtains the shared adapter lease, registers the agent and opens a two-minute window. Show the effective name, sender instructions, countdown and **Stop pairing**. Closing the panel leaves the bounded window running.
2. Agent acceptance records a provisional candidate, not a saved grant. Opening the window consents to admit a nearby audio device; a familiar display name is not proof of its identity.
3. Success is the first eligible A2DP PCM endpoint from a candidate admitted in this still-open window, as defined below. Persist the grant, close admission, finish authorization replies and unregister the agent/restore adapter properties. Show **“Pixel 9 Pro can now play audio”** with **“Not this device? Remove”**, which immediately revokes audio access. Removal remains available in Manage devices after the notice disappears.
4. On timeout/cancel, discard provisional candidates and restore temporary trust changes. A bond alone never creates a grant. An already bonded phone can subsequently receive **Allow audio** in Manage devices. Remembered, allowed phones reconnect without reopening pairing.

The first qualifying candidate completes the window; serialize that decision so two phones cannot both win. Clean up other provisional candidates. A remembered phone reconnecting does not complete another phone's pairing flow. Pairing success alone does not take output; actual audio follows existing arbitration.

Manage devices shows connected/playing/saved state and **Paired; audio not allowed**. Use **Paired for setup** only when provisioning supplies that provenance. **Allow audio** handles an existing bond without a grant; it is not a second mandatory step after ordinary audio pairing.

| Device action | Meaning |
|---|---|
| Disconnect audio | Disconnect this A2DP session and release its playback; retain its grant and bond. A later reconnect is allowed. |
| Remove audio access | Persist denial, restore pre-grant trust when known, disconnect current A2DP and reject subsequent unauthorized audio connections. Retain the bond. |

V1 offers no full unpairing and never labels audio-only revocation **Unpair** or **Forget device**. Do not use whole-device disconnect or `RemoveDevice` as a substitute for audio-profile disconnect.

### AirPlay and persistent enable/disable

An enabled AirPlay receiver is available by default. Show its effective advertised name and sender instructions. Report actual readiness; spawning a child alone is not proof of advertisement, so use unknown visibility until confirmed. **Disable AirPlay** writes `enabled=false`, stops its owned receiver and releases its input without restarting Core. The choice survives a restart or power cycle. **Enable AirPlay** saves `enabled=true` and starts it again.

Always explain that disabling ends any AirPlay playback; confirm this consequence before saving. There is no **Stop receiving / Make available** pair or temporary availability state. Add genuine session disconnect only after verifying support in the bundled shairport-sync build; do not assume an upstream D-Bus feature is present.

AirPlay has no saved-device list in v1. Current sender names/addresses are informational, not authenticated identities or permission keys. Bluetooth's **Stop pairing** instead closes new-device admission while remembered devices can still reconnect.

Panel enable/disable writes only that plugin's `enabled` field through the existing config API. Reconcile it with the settings UI while preserving unrelated staged edits. Opening a panel never silently enables a receiver or installs packages.

## One streamer name, optional overrides

Use Core's `server.service_name` as the default for both receivers. Add an optional Bluetooth `device_name`; replace AirPlay's `Kalinka (<hostname>)` default with an unset override. Show **Use streamer name** by default, with a custom-name field when overridden. Clearing the override restores inheritance; never copy the current server name into a saved override.

Core supplies an optional read-only `context.server_name()` accessor and calls an optional `server_name_changed()` plugin hook, defaulting to a no-op, after a saved rename. Plugins read the current name and reconcile their own receiver; they do not inspect Core internals. Applying `base_config.server.service_name` updates Core's own discovery identity and both inheriting receivers. Explicit overrides remain unchanged. Implement discovery updates and notifications, not just an app-label change.

Name changes apply without a Core restart. AirPlay may restart its own receiver and interrupt its input; explain this when applying a name change, including an inherited rename. A disabled receiver stores the desired name without starting. Validate protocol name limits and report the effective name after any normalization or collision handling.

Bluetooth's alias is adapter-wide. Serialize writes with the existing adapter lease. While provisioning owns it, retain the desired name, show the current effective name/pending change, and reconcile after lease release. The supervisor already saves/restores its temporary alias; do not race that restoration or reapply a stale name. Reconcile the latest desired name before audio discovery. Provisioning's temporary setup identity stays distinct from the audio name.

Preserve explicitly saved AirPlay names as overrides during migration. Unset/default-derived names follow the server. Existing Bluetooth has no override to migrate.

## Minimal optional SDK contract

Illustrative additions, with defaults that leave existing plugins unchanged:

```python
class InputModulePlugin(...):
    SETUP_WHEN_DISABLED: ClassVar[bool] = False
    RECEIVER: ClassVar[ReceiverDescriptor | None] = None

    def get_receiver_control(self) -> ReceiverControl | None:
        return None

class ReceiverControl(Protocol):
    async def snapshot(self) -> ReceiverSnapshot: ...
    async def execute(self, command: ReceiverCommand) -> ReceiverSnapshot: ...
```

The static descriptor contains contract version, presentation identity and supported v1 actions. Core derives `receiver_control` from it, independently of plugin IDs/the public catalog. An initialized controller remains accessible while disabled; commands requiring reception return a readable refusal. Failed/incompatible plugins retain an inventory row when their descriptor is available, without a callable controller.

Use typed snapshots and a discriminated command union:

| Snapshot state | Meaning |
|---|---|
| Identity | Core/plugin-scoped receiver ID, title and contract version. One receiver per plugin initially. |
| Enabled and status | Saved enabled value; actual `disabled`, `starting`, `ready`, `stopping` or `error`, with a readable reason. |
| Name and visibility | Effective name, inherited/custom indication, and `hidden`, `advertising` or `unknown`. |
| Pairing, when supported | Current opaque window ID and remaining seconds, or closed; recent successful peer for feedback. |
| Sessions | Senders and connection/playback state with opaque session IDs, independent of selected output. |
| Peers, when supported | Opaque peer IDs, names, bond state and audio permission. |
| Actions | Supported v1 commands and current availability/reason. |

Commands are `open_pairing`, `close_pairing(window_id)`, `grant_audio(peer_id)`, `revoke_audio(peer_id)` and `disconnect_audio(session_id)`. Opening uses the bounded 120-second default. Bluetooth implements these; AirPlay initially implements none and participates through its snapshot plus ordinary live config fields. Older clients ignore unknown optional capabilities; Core rejects unsupported commands.

Do not define access-request models, advertisement commands, bond-removal commands, effect-acknowledgement tokens or runtime-generation fields for v1. Add capabilities with a working backend and concrete client need.

## Core setup and live configuration

For an opted-in receiver, `PreparedPlugin.setup()` calls `setup(context)` even when disabled. Setup initializes context, controller and saved policy, but does not start audio reception or pairing while disabled. Create its input interface/controller once for the Core lifetime. The plugin starts/stops its audio runtime in `config_changed({"enabled"})`; disabling is not plugin unload.

Successful setup leaves internal `health_state=READY`; `get_state()` reports `DISABLED` until enabled. Refresh that report before publishing inventory or admitting browse/playback calls. Reuse `refresh_health()` and `_update_enabled_input_modules()` on changes. Preserve successful setup versus current serving state without mutating setup health, rebuilding interfaces or re-registering plugins. Controller lookup must not use the enabled-input filter. The plugin still refuses playback while disabled, including calls through `set_up_input_module()`.

Bluetooth can reuse its enabled-change logic after separating runtime stop from final cleanup. AirPlay needs `config_changed()` for enabled/name changes and idempotent start/stop. Plugins serialize commands, name/enabled changes and backend events with their own lock; final shutdown cancels tasks/timers and releases owned resources. A missing backend must not prevent lightweight controller setup while disabled. Runtime failures appear in snapshots, never as implicit Core restarts.

Add one per-field **`applies_live`** marker, default false, alongside **`apply_immediately`**. It means saving the field needs no Core restart, not that a staged widget saves itself or that audio cannot be interrupted. Existing `apply_immediately` behavior stays for other plugins. New clients skip restart only when every changed field applies live. Mixed writes retain existing restart behavior for other fields; receiver commands never enter such a batch.

Mark enabled/name fields live once handlers exist. Optional codec preferences remain absent unless the backend can support and apply them; prefer Automatic. Config already persists before notifying owners: failed startup leaves desired enabled=true and actual error, not a false ready state. Refresh receiver/module state after notification.

There is no new lifecycle coordinator, per-plugin reload API, `restart_plugin` apply tier or operation registry. Existing plugins keep their setup/restart defaults.

## Synchronous API and state updates

```text
GET  /receivers
GET  /receivers/{receiver_id}
POST /receivers/{receiver_id}/commands  -> 200 ReceiverSnapshot
```

POST awaits the bounded backend transition and returns the snapshot. **Open pairing** completes when the window is open, not when a phone pairs or the timer expires. No `202`, `/operations`, request-ID cache or operation events. Enabled/name changes use the config API; `starting`/`stopping` describes background receiver startup/shutdown.

Commands are idempotent at their target:

- Opening an already-open window returns it without extending it.
- Close names a window ID and never closes a newer window; disconnect names a session ID and never disconnects a replacement session. An already-ended target is a no-op; a conflicting/stale target is refused.
- Granting existing access or revoking absent access is a no-op, except that pending trust-restoration/profile-disconnect cleanup still needs reconciliation.

Use unpredictable window/session IDs and controller-runtime-scoped peer handles; invalidate them on runtime replacement. No boot epoch/generation is required on every command. Do not automatically resend POSTs after timeout, reconnect or restart: GET current state first, then let the user act. An old close/disconnect cannot affect a replacement; an untargeted new **open** is a new intent, not a replay guarantee across restarts.

Bound backend calls and reconcile cancellation. A partial revocation stays denied and reports failed cleanup; never report success while the phone remains connected for audio. Use structured errors such as `plugin_disabled`, `unsupported_operation`, `adapter_busy`, `stale_target`, `backend_unavailable` and `backend_timeout`, with appropriate 4xx/503/504 responses. Unsupported capability is not HTTP 500. Client confirmation explains the fixed action meaning; the backend revalidates targets/state and refuses an unsafe race without an effects-token exchange.

Add only `receivers_changed` to the existing event connection, with a snapshot on replay. An optional context notifier invalidates cached state; coalesce notifications. The app refreshes after writes/events/reconnect and serializes its refreshes so an older HTTP response cannot overwrite a newer one. Snapshots never wait for pairing and contain no PCM blocks or playback-progress ticks.

Send pairing **remaining seconds**, sampled from the backend's monotonic deadline. The app counts down using monotonic elapsed time and refreshes on resume/reconnect. It is approximate; only the backend decides expiry. An incorrect Pi wall clock before NTP, or a clock adjustment, cannot extend the window. Offline UI must not claim discovery is still active.

Use the existing streamer-management authorization boundary. An audio grant does not grant app/admin access; a familiar peer name is not identity proof. No separate permission-management framework is introduced.

## Bluetooth admission and revocation

### Concrete pairing success

Before changing trust, record the candidate's actual BlueZ object path, resolved identity/address, pre-grant `Trusted` and owning window. Never derive its path from the address; privacy addresses may differ. Agent callbacks admit a candidate provisionally for audio-related authorization during that window, without adding a saved grant.

Success is the first newly observed eligible **BlueALSA PCM source endpoint**, not an agent callback or generic `Connected=True`. Require `Transport="A2DP-sink"`, `Mode="source"`, matching `Device` on the selected adapter, valid PCM format, and BlueZ `Paired=True` / `Connected=True`, as in `BlueAlsaBackend.endpoints()`. It must belong to a candidate admitted in this same still-open window. Correlate updates whether pairing state or PCM interface notification arrives first.

Here “first A2DP PCM” means endpoint availability; it does not require non-zero samples or starting a song. A paused phone can finish pairing. Renderer readiness/output ownership is irrelevant. This candidate is the only provisional exception to the no-grant disconnect rule, bounded by the window. On success, durably commit its grant/trust baseline before admitting PCM to playback, then close the window after outstanding agent replies. Failure/cancel/expiry rolls back provisional trust and disconnects ungranted A2DP.

Already bonded **Allow audio** uses explicit user consent instead of pairing success. Record prior trust before changing it and saving the grant. Repeated grants retain the original baseline, never replacing it with Kalinka's newly written `True`.

### Trust restoration and effective removal

Replace the address-to-alias file with versioned records containing identity, alias, grant state and **trust before the audio grant** (`true`, `false`, or `unknown` for legacy test data). Persist restoration information before changing trust; retain cleanup records until restoration succeeds. Roll back pending grants after a crash, never promote them silently on boot.

On **Remove audio access**:

1. Durably revoke the grant and provisional admission. Stop its PCM forwarding and release only its playback hold.
2. Restore pre-grant trust: false if audio introduced it, true if it already existed. The inspected supervisor never sets `Trusted`, so preserving audio-created trust as supposed setup state is incorrect. Keep the bond and provisioning credentials.
3. Call `org.bluez.Device1.DisconnectProfile` for the peer's registered A2DP audio profile using the actual device path. Observe transport/PCM removal; report failure if teardown exceeds its bounded budget. Method success alone is not proof of removal. The [BlueZ Device API](https://bluez.readthedocs.io/en/latest/device-api/) distinguishes this from whole-device `Disconnect`, which terminates all profiles/the ACL connection.
4. On every new/reappearing A2DP transport or PCM endpoint, disconnect it unless the peer has a grant or valid provisional admission. Reject unauthorized audio-service requests when the agent is consulted. Enforce denial regardless of `Trusted`; previously trusted and legacy phones must not stay selected for audio that is merely discarded.

Implement the disconnect helper and D-Bus permission before exposing revoke/disconnect. Coalesce transport notifications and retry teardown with a bound/backoff; report failures rather than spinning. Inspect startup inventory and subsequent events so reconnect/restart cannot bypass policy. Scope this to Kalinka's configured BlueALSA receiver, not other Bluetooth profiles or another audio manager. Verify profile teardown on the packaged Pi build, including simultaneous provisioning use.

Disabling Bluetooth closes admission and disconnects audio sessions. If its configured BlueALSA service keeps exposing A2DP, retain a lightweight admission watcher in the initialized controller to disconnect incoming audio while disabled. No discovery, capture or playback runs in that state. Stopping the PCM reader alone is insufficient; do not stop shared BlueZ/provisioning services.

Existing test-image grants have no recoverable trust baseline. Migrate them with `trust_before_grant=unknown`, preserve current trust on revoke, and enforce denial through the same profile disconnect path. Do not invent a false baseline. After such a grant is removed and cleanup completed, a later explicit grant snapshots then-current trust normally.

## Delivery and compatibility

1. Add SDK defaults for the descriptor/controller, `SETUP_WHEN_DISABLED`, live-field metadata, state notification and small server-name context hook. Leave browsing/media interfaces unchanged.
2. Add Core GET/POST routing, snapshot events, opt-in setup while disabled, name propagation and live config presentation. Reuse prepared plugins/health refresh, without tracked operations or reload machinery.
3. Ship Bluetooth's first release with the controller and corrected admission/trust/profile-disconnect behavior. Remove `pair_phone`, `authorize_device` and `forget_device` from its config model now. Discard stored test-image overrides before generic one-shot consumption; never execute a staged discovery action during migration. Preserve bonds and migrate grants as above. Upgrade the test app alongside Core/plugin.
4. Add AirPlay's initialized controller, inherited name and live enabled/name handlers. Preserve explicit saved names and enabled values. Disabled stays disabled across reboot; enabled starts its receiver.
5. Add app entry points/panel, feature-detect the API and honor `applies_live`. Older servers retain their settings path with an update explanation; do not fake commands with restart. Older clients can use ordinary settings but do not acquire new Bluetooth device management automatically.

No released Bluetooth one-shot API needs an adapter. If that API ships first, explicitly revise the plan: retain forwarding fields marked **superseded**, let new clients filter them, and keep them visible to old clients. Core cannot hide fields selectively without client capability negotiation. This is a contingency, not the selected v1 plan.

Plugins with no descriptor/setup opt-in are unchanged. Missing system packages use existing explicit software management. Installed inventory drives receiver visibility even for unpublished plugins.

## Acceptance criteria

- Home and both server-sheet layouts expose the entry with catalogs off, disabled/error receivers and navigation rails. No installed receiver hides it; a failed fetch is not an empty inventory.
- Pairing opens in two taps without a Core restart or output takeover. Its monotonic countdown survives wrong Pi time, NTP changes and app backgrounding.
- Agent callbacks alone save no grant. Eligible A2DP plus a paired candidate completes even when paused; test event ordering, two candidates, expiry and crash recovery. Success offers **Not this device? Remove**.
- Revocation removes the phone's A2DP route and blocks reconnects, including originally trusted and legacy-unknown phones. Originally untrusted phones return to `Trusted=False`; previously trusted ones keep trust. The bond/BLE setup remain usable.
- Disconnect retains the grant. Stale session/window commands cannot affect replacements. Ungranted/disabled audio connections are disconnected, not discarded. Backend failure yields a finite error and pending cleanup.
- First enable after booting disabled works live. Controller/input registration remain stable across toggles, with correct health/serving state. Disabled setup starts no audio reception.
- AirPlay disable persists over reboot. Enable/name changes need no Core restart; receiver-only interruption is explained. Unsupported disconnect/permission controls are absent.
- Streamer rename updates Core and both inherited receiver names; overrides survive. Provisioning alias restoration/deferred rename cannot undo the desired audio name.
- Synchronous commands return snapshots or structured errors. Lost replies cause refresh, not replay; repeated current-target commands are safe. No operation service or effects tokens are needed.
- Test-image migration preserves grants/bonds, marks unknown trust honestly and never executes saved one-shots. Other plugins retain their behavior.
- Use existing palette/components, 48dp targets, stable busy/error layout and focus. Check narrow/landscape/tablet layouts, large text and screen readers without announcing every countdown tick.

Latency reporting and lip-sync remain media work. This interface does not own timing, compression or audio buffers.
