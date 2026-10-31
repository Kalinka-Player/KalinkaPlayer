# External output — design

> Proposed 2026-10-18, for review. Nothing here is built yet. The server change lands as one pull request in this repository and the app change as one in the app repository, each with the tests listed under [Tests](#tests). Issues: #254, #259, KalinkaAI#123.

## Problem

Roon Bridge plays through its own engine: it opens the DAC itself, and no Kalinka renderer carries the audio. The server only describes that playback, through an `ExternalPlayback` hold (`ExternalPlaybackService.acquire` in [external_playback.py](../packages/kalinka-server/src/kalinka_server/external_playback.py)). Everything that answers for outputs still answers as if a renderer were playing:

- The hold records `registry.active_id()` at acquire. `RendererRegistry.active_id()` resolves to the selected or first renderer while no session is held, so `GET /renderer/active`, `/renderer/list` and `current_renderer_changed` keep naming that renderer.
- `OutputDeviceRouter.current()` falls back to the built-in renderer volume device when the hold passed no `volume_control`, and the Roon plugin passes none. `/device/ws` and `/device/*` therefore offer a working slider for a renderer that plays nothing.

The app shows a cast chip naming the idle renderer and a slider whose moves become that renderer's volume for its next session. Android's media routing keeps a route for it and, while Kalinka is in the foreground, sends the phone's volume keys there.

## Decision

`playback_control` says whether the playback goes through a Kalinka renderer (`output: "renderer"`, the default) or through the holder's own engine (`output: "external"`). While an external hold is on, the registry reports no active renderer but keeps the saved selection and discovery, and the device bus reports no volume control unless the hold brought its own. Clients hide the renderer chip and the slider from those two facts alone.

The renderer protocol, the plugin SDK's calls, the Android native code and the Roon plugin stay as they are.

## API: REST/WebSocket 0.18

### `playback_control.output`

```json
{"mode": "exclusive", "plugin_id": "roon", "title": "Roon", "seekable": true, "output": "external"}
{"mode": "queue", "plugin_id": null, "title": null, "seekable": true, "output": "renderer"}
```

`output` is `"renderer"` for the queue and for a plugin that plays on a renderer through direct playback (Qobuz Connect, AirPlay). It is `"external"` only under `"exclusive"`, for a plugin whose engine owns the audio. The server sets it from the kind of hold; a plugin does not choose it. It travels wherever `playback_control` does: `playback_control_changed`, the `/queue/ws` and `/queue/events` replay, and `GET /queue/control`.

### While `output` is `"external"`

| Answer | During the hold |
| --- | --- |
| `current_renderer_changed.renderer_id`, the replay's `current_renderer_id`, `GET /renderer/active`'s `renderer_id` | null |
| `selected_renderer_id`, each `/renderer/list` row's `selected` | the saved pin, unchanged |
| each `/renderer/list` row's `active` | false |
| `renderers_changed` | keeps firing as renderers come, go and reconnect |
| `PUT /renderer/active` | ends the hold and pins the renderer, as SDK 3.7's "selection changes revoke external playback" says (`ExternalSession.move_to` releases with OUTPUT_LOST); any listed renderer, even an offline one, resolves, so only `renderer_id: null` with no playable renderer leaves the hold on and just returns to automatic, as today |
| `/device/ws` state, `GET /device/get_volume` | without `volume_control`: `{"power_on": false, "volume": {"supported": false}}`, and `supported: false`; with it, the engine's volume as SDK 3.11 specifies |
| `GET /device/list`, `PUT /device/set_volume`, the device socket's commands | without `volume_control`: the existing no-device answers ("No device configured"; the socket ignores the command), and nothing is sent anywhere |

None of these is a new shape: they are the answers the server already gives when no device module is in charge. A downstream amplifier delegated to the renderer playback would go to keeps precedence in both cases, as it does today.

A client that connects during a hold learns all of it from the replays: the queue socket's carries the external `playback_control` and a null `current_renderer_id`, the device socket's the unsupported volume.

### Event order on the queue socket

Acquire: the queue's own `state_changed` (STOPPED) if it was playing, `playback_control_changed` (external), `state_changed` (the plugin's), `current_renderer_changed` (renderer_id null); then the device bus re-authors its state.

End of the hold, by release or preemption: the engine stops (on preemption; on release the plugin has already stopped it); `current_renderer_changed` (renderer back); the device bus is re-authored with the renderer's volume; `playback_control_changed` (queue); `state_changed` (the queue's STOPPED, followed by its playback when the queue is what took over). The renderer comes back only once the engine is silent, so no client routes volume keys or a slider to it while Roon is still audible.

One external hold taking over from another: `current_renderer_changed` (renderer back) and the device bus re-authored with the renderer's volume as the old hold ends, since its teardown cannot tell an external successor from the queue or Qobuz Connect, which re-author nothing themselves; `playback_control_changed` (the new holder, still external) unless its control equals the old one's (the arbiter sends none for an equal control, as for a second Roon hold), `current_renderer_changed` (null), then the bus re-authored for the new hold. `output` stays external throughout.

A client keys the renderer chip on `playback_control.output`, never on `renderer_id` alone. Then none of these gaps shows a wrong chip. The slider follows the bus alone, so between two holds it can show for the moment the renderer's volume is on the bus; that gap is accepted rather than giving teardown knowledge of its successor.

### Version note

For [version.py](../packages/kalinka-server/src/kalinka_server/version.py):

> 0.18: `playback_control` carries `output`: "renderer" while the queue or a plugin plays on a Kalinka renderer, "external" while the plugin holding the output plays through its own engine, as Roon Bridge does, and no renderer carries the audio. Then `current_renderer_changed`, the replay's `current_renderer_id` and GET /renderer/active report `renderer_id` null, no /renderer/list row is `active`, `selected_renderer_id` keeps the pin, and the device volume reports `supported` false unless the plugin offers volume, in which case the volume is the engine's. PUT /renderer/active still ends the hold. A client before 0.18 sees a null renderer: a crossed cast icon with no output name, and no slider.

kalinka-v5.11.0 shipped 0.17, so this is a bump. If another change takes 0.18 first and it is still unreleased, this note joins that paragraph instead.

### SDK 3.14

README line: `PlaybackControl.output` (3.14+): `PlaybackOutput.RENDERER` or `EXTERNAL`; the server sets EXTERNAL on the control of an `ExternalPlayback` hold, so clients know no Kalinka renderer carries the audio. Plugins need not change. The datamodel refuses EXTERNAL with mode QUEUE.

The server imports `PlaybackOutput`, so its SDK pin, `control.in` and both RELEASING.md quotes of the pin move to 3.14, as for any minor bump. No plugin's pin moves.

## Server

### Registry

[renderer_registry.py](../packages/kalinka-server/src/kalinka_server/renderer_registry.py) holds the external hold beside the queue's claim:

- `external_claimed(owner)` and `external_released(owner)` set and clear `_external_hold` by identity. A release from another owner is ignored, as `session_released` ignores one. Both call `_publish_current()`, so `current_renderer_changed` fires with `(None, selected)` and later `(renderer, selected)`, and observers are told.
- `serving_id()` is what clients are told plays: None while `_external_hold` is set, otherwise `active_id()`. Its docstring says it is the renderer serving the current playback, for reporting only, and that code deciding where playback goes wants `active_id()`. It has three callers: `_publish_current()`, `list()` and `_renderer_selection()` in [server.py](../packages/kalinka-server/src/kalinka_server/server.py), which answers both `GET` and `PUT /renderer/active`.
- `active_id()` keeps its meaning, the renderer playback runs on or would go to, and all of its other callers. Its docstring gains one line: an external hold does not change it, and reporting goes through `serving_id()`.

Reporting is the exception, so it takes the new name. Three call sites change, and an internal caller added later that reaches for `active_id()` keeps working through a hold instead of silently getting None.

### Callers that keep `active_id()`

Several read the renderer before the arbiter has ended the hold, or need the renderer behind it. None during a hold would break them:

| Caller | What None would break |
| --- | --- |
| `RendererPlayer._ensure_session` ([renderer_player.py](../packages/kalinka-server/src/kalinka_server/renderer_player.py)) | It reads the renderer and only then claims it, and the claim's `before_claim` hook is what calls `arbiter.acquire` (`PlayQueueImpl._new_player`). Pressing play could not take the output back from Roon: it would fail with "no renderer is connected". |
| `DirectPlaybackService.acquire`, both reads ([direct_playback.py](../packages/kalinka-server/src/kalinka_server/direct_playback.py)) | The pre-check runs before the arbiter hands over, so Qobuz Connect would be refused while Roon holds the output instead of taking over from it. |
| The renderer an `ExternalSession` records at acquire (see [Session](#session)) | It is read before `arbiter.acquire`. A hold acquired while another still holds would record None, and `vacate` (the speaker test, a renderer upgrade) could no longer find it. |
| `OutputDeviceRouter.current_name` ([output_device_router.py](../packages/kalinka-server/src/kalinka_server/output_device_router.py)) | A downstream amplifier delegated to that renderer would lose its precedence during the hold. |
| `OutputCapabilityTracker._current` ([output_capabilities.py](../packages/kalinka-server/src/kalinka_server/output_capabilities.py)) | AirPlay would stop advertising what the renderer can play while Roon holds the DAC. |
| The speaker test's fallback (`server_test_tone` in [server.py](../packages/kalinka-server/src/kalinka_server/server.py)) | The setup wizard would no longer find the renderer, and `vacate` would no longer end the hold first. |

`RendererVolumeDevice` keeps `active_id()` as well. During a hold it is not the device in charge, and the router drops its events. `PlayQueueImpl.switch_renderer` resolves through `resolve_active()` and is unaffected.

### Router

[output_device_router.py](../packages/kalinka-server/src/kalinka_server/output_device_router.py):

- `set_external(owner, device)` takes an optional device and is called for every external hold.
- `current_name()` is unchanged. `active_id()` still names the renderer whose delegated amplifier keeps precedence during a hold, and without one the external plugin's id comes next, as today.
- `_external_name()`: the external owner's plugin id from `set_external` until `clear_external`, whether or not the hold is still `active`, so the external owner stays in charge through its teardown (see [Session](#session)).
- `current()`: an external owner in charge gives its device, or None when it brought none. `resync()` already maps None to the unsupported state, and every `/device/*` route and the device socket already treat None as no control.

Device automation already does nothing without a device that can power on. A hold without volume therefore leaves power alone, just as the renderer device's no-op power does today, and a delegated amplifier is powered as before.

### Session

[external_playback.py](../packages/kalinka-server/src/kalinka_server/external_playback.py):

- `ExternalPlaybackService(plugin_id, arbiter, registry=None, router=None)`: the registry replaces the `renderer_id` callable. The service hands it to each `ExternalSession`, which records `registry.active_id()` as its `renderer_id` at acquire and releases its claim through it. Without a registry the session records None, as the default callable does today. `player_setup.py` passes the registry in place of `renderer_registry.active_id`, and the router as before. With one source, a caller cannot pass the registry and forget the renderer, which would silently leave `vacate` unable to find the hold. Tests that passed `registry.active_id` pass the registry instead. Those that passed `lambda: "local"` pass one built by `_registry_with("local")` from `test_renderer_volume_control.py`, as `test_external_volume.py` already does.
- `ExternalSession.control = PlaybackControl.exclusive(plugin_id, title, output=PlaybackOutput.EXTERNAL)`.
- `acquire`: after `arbiter.acquire`, under the existing guard (`session.active and arbiter.owns(session)`) and with no await in between, calls `router.set_external(session, session.volume_device)` when there is a router and `registry.external_claimed(session)` when there is a registry, and only then `await router.resync()`. A preemption landing during that await runs `_stop` after the claim, so it is released; claiming after the await would set it on a hold that has already ended, hiding the renderer until restart. Both stay optional, as the tests that build the service with neither rely on.
- `_stop`: `router.clear_external(self)` and the bounded `resync()` run for every hold with a router, not only one with a volume device. `router.clear_external(self)` and `registry.external_released(self)` both run in a `finally` right after the `on_revoked` wait, before the resync, so the renderer and its controls reappear only once the engine is silent. For that, the router's `_external_name()` keys on the owner it was given, not on `owner.active`: `_begin_end` clears `active` before `on_revoked` is even called, which today hands `/device/set_volume`, the device socket's commands and the renderer's own volume events back to the renderer while Roon is still audible. `ExternalVolumeDevice` already refuses requests once its session is inactive. `_stop` is the teardown every exit starts in `_begin_end`, so "a finished teardown means no claim" holds for release, preemption, a failed acquire and cancellation alike.

#254 also names plugin disable and disconnection. A plugin's settings apply on restart, and shutdown revokes the hold through `arbiter.shutdown()`. A plugin that loses its Roon Core releases. Both paths end in `_stop`. The registry does not tell a local renderer from a remote one. With no renderer connected, the claim changes nothing a client sees: `serving_id()` was None before and stays None, and no event fires.

## App

### Model and providers

- `data_model.dart`: `enum PlaybackOutput { renderer, external }`. `PlaybackControl.output` is read leniently, so an absent or unknown value reads as renderer, and it is part of `==` and `hashCode`. `bool get isExternal => isExclusive && output == PlaybackOutput.external`. `PlaybackControl` is hand-written, so nothing is regenerated.
- `app_state_provider.dart`: `externalOutputProvider = playbackControlProvider.select((c) => c.isExternal)`.

### Widgets

- `RendererSwitcherButton` renders nothing while external.
- `RendererSwitcherDropdown` shows, in its own place, a line that cannot be tapped: the speaker icon and the attribution copy. The copy comes from the one function the note under the metadata (`_ExclusivePlaybackNote` in `now_playing_content.dart`) uses, so the two cannot drift.
- The kiosk output panel (`kiosk_output_panel.dart`) shows the same line in place of the output's name, and no "OTHER OUTPUTS" rows. `kiosk_screen.dart`'s `outputs` is false while external, so the output pill opens the panel only when the hold brings volume. The pill already names the server followed by "· via Roon", since a null output falls back to the server's name.
- The coach-mark tour (`music_player_screen.dart`) skips its output stop while external, as it does while the switcher is hidden.
- The slider follows the device bus alone (`volumeAvailableProvider`), with no gate on `isExternal`, so a hold that brings volume keeps its slider.

### Behaviour

| Element | Queue on a renderer | Plugin on a renderer (Qobuz Connect, AirPlay) | External hold, no volume (Roon today) | External hold with `volume_control` | Just after the hold ends |
|---|---|---|---|---|---|
| Cast button (phone Now Playing header, mini player) | shown | shown | hidden | hidden | shown |
| Output dropdown (tablet and embedded headers) | output name and chevron | same | speaker icon and "Playing via Roon", not tappable | same | output name back |
| Volume slider (Now Playing, kiosk bar and panel) | per device bus | per device bus | hidden: the bus says unsupported | shown, drives the engine | hidden while the queue sits stopped, back once Kalinka plays (existing rule) |
| Artwork, title, artist, format line, progress | the queue's | the plugin's | Roon's, as reported; position control per `seekable` | same | the queue's STOPPED snapshot |
| Transport | the queue | the plugin | the plugin; play/pause disabled unless Roon reports playing or paused (existing) | same | the queue |
| Note under the metadata | source line only | source line and "Playing via Qobuz Connect" | source line and "Playing via Roon" | same | source line only |
| Bit-perfect badge | per output and volume | per output and volume | hidden: an unsupported volume has backend unknown | per the output and volume the plugin reports | per output and volume |
| Stream info dialog (#259) | the queue's stream | the plugin's | Roon's track and what it reports | same | the queue's |
| Kiosk output pill | output name | output name · via Qobuz Connect | server name · via Roon, opens nothing | server name · via Roon, opens the panel | output name |
| Kiosk output panel | name, slider, other outputs | same | not reachable | "Playing via Roon" and the slider, no rows | back |
| Android media notification and session | shown | hidden (existing: exclusive) | hidden (existing) | hidden | back when the queue's state arrives |
| Android volume keys, Kalinka in the foreground | the renderer, via `PUT /device/set_volume` | the renderer | Android's own: no current route | Android's own; the in-app slider works | the renderer once the queue plays |
| Android foreground volume overlay | on key presses | on key presses | never: the keys are Android's | never | once the queue plays |
| Android output switcher chip | names the renderer | names it | absent: no current route | absent | back |

The phone header's cast button is icon-only, so it is simply absent, and the note under the metadata names Roon. The tablet and embedded headers name the output, so the dropdown carries the line in its place. Whichever wording review settles on applies to both.

The stream info dialog already lists a plugin's track under exclusive control (`streamInfoFields(exclusive:)`); this design leaves it as it is.

### KalinkaAI#123

The issue sets two rules. The media notification shows only while the mini player shows and no plugin plays in direct mode. The hardware volume keys and volume control work only while a renderer is connected, playback is playing, buffering or paused, and Roon does not hold the output. The table keeps both:

- The first is existing behaviour: `KalinkaMediaService` stands down under exclusive control.
- The second follows from the server change alone. Android's `KalinkaRouteState.current` is null once `current_renderer_changed` says null, so `KalinkaVolumeKeys.dispatch` returns false and the keys stay Android's.

Erratic behaviour outside a hold stays with #123's own fix.

## Compatibility

- **Old app, new server.** `output` is ignored. A null `renderer_id` makes the chip read "Choose output" with the crossed cast glyph, the bus hides the slider, and the existing exclusive note still names Roon. Picking a renderer in its sheet ends the hold, as `PUT /renderer/active` always has. Blunt but honest.
- **Old Android native code.** `current_renderer_changed` null clears the current route, so the keys fall through and the chip is absent. The notification is already hidden under exclusive control.
- **New app, old server.** An absent `output` reads as renderer, so the app behaves as today.
- **Plugins.** No SDK call changes, and the Roon plugin is unchanged. `PlaybackControl` gains an optional field with a default.

## What this does not do, and its cost

During a hold that brings `volume_control`, the hardware keys stay with Android: native volume is tied to a media route, and no renderer route exists during the hold. The in-app slider still drives the engine. A route-less native volume path is a separate change, and the Roon plugin offers no volume today.

Until it is updated, an old app shows a crossed cast icon rather than a note during Roon. That is less wrong than today's live slider for an idle renderer, but it reads as "no renderer" rather than "playing elsewhere", for the very user Roon support is meant to win. The app pull request should ship close behind the server's.

## Tests

### Server

- `packages/kalinka-plugin-sdk/tests/test_playback_control.py`: `output` defaults to renderer for the queue and for `exclusive()`. `exclusive(..., output=EXTERNAL)` serialises `"output": "external"`. `PlaybackControl(mode=QUEUE, output=EXTERNAL)` is refused. JSON without `output` parses as renderer.
- `tests/test_renderer_registry.py`:
  - `external_claimed` hides the serving renderer (`serving_id()` None, no row active) while `selected_id` and the rows' `selected` flags stay and `active_id()` still names the renderer; `external_released` restores it.
  - A release from another owner is ignored, and the current callback receives `(None, selected)` then `(renderer, selected)`.
  - A renderer registering, reconnecting or leaving during the claim fires `renderers_changed` and leaves current at `(None, selected)`.
  - Observers see `active_id()` unchanged.
  - With nothing connected, a claim and its release fire no current event.
  - A local and a remote renderer behave alike.
- `tests/test_external_playback.py`:
  - The hold's control has output EXTERNAL.
  - With a registry, `serving_id()` is None while held and the renderer again after each way a hold ends: release, preemption by the queue, by another holder, `take_back`, shutdown and `vacate`, a failing `on_revoked`, a cancelled acquire, and each cancelled handover. `active_id()` names the renderer throughout.
  - Event order on the emitter and the registry callbacks matches [Event order](#event-order-on-the-queue-socket), and by the time the queue's control is published the renderer is serving again. On preemption, `serving_id()` stays None until a slow `on_revoked` returns.
  - A preemption that lands while `acquire` awaits its resync leaves no claim: `serving_id()` is the renderer once it has run.
  - A service built without a router or registry acquires and releases as today, and its holds record no renderer.
  - A hold acquired over another records the shared renderer, and `vacate` of that renderer ends it.
  - The tests that passed `lambda: "local"` pass a registry naming `local` and still vacate the hold.
- `tests/test_external_volume.py`:
  - A hold without `volume_control` leaves the bus volume unsupported and `router.current()` None, and a renderer `VolumeChangedEvent` is dropped; release restores the renderer's 42.
  - A delegated amp keeps precedence without `volume_control`.
  - While a slow `on_revoked` runs, with and without `volume_control`, `router.current()` is not the renderer device and a renderer `VolumeChangedEvent` is dropped.
  - The `volume_control` cases are unchanged.
  - `test_old_plugin_without_volume_keeps_existing_routing` now expects no control: the one expectation this design flips on purpose, and the pull request says so. The rig passes the registry in place of `registry.active_id`.
- `tests/test_direct_playback.py`:
  - Qobuz Connect acquires over a Roon hold without being refused. Roon is revoked with OTHER_HOLDER, the new control's output is renderer, and `serving_id()` is the renderer.
  - `test_external_engine_releases_the_renderer_and_waits_before_queue_resumes` passes the registry, so the queue playing over a Roon hold pins `_ensure_session` on `active_id()`.
  - An output capabilities watcher sees no change across a Roon hold.
- An app-level test over `server.create_app`, with `tests/app_harness.py`'s `isolate_app` and `running` as `test_demo_mode_app.py` uses them, a renderer registered, and a hold taken through `app.state.player_context.external_playback_for`:
  - During the hold: `GET /renderer/active` returns `renderer_id` null with the pin, `/renderer/list` has no active row, `GET /device/get_volume` returns `supported: false`, `PUT /device/set_volume` sends nothing, a fresh `/queue/ws` replay carries the external control and a null `current_renderer_id`, and a fresh `/device/ws` replay the unsupported volume.
  - After release, all of these are back.
- `tests/test_plugin_inventory.py`: the pinned API version moves to 0.18.

### App

- `test/playqueue_events_test.dart`: `PlaybackControl.fromJson` reads `output` and treats an absent or unknown one as renderer. `isExternal` is true only for exclusive and external, and `==` tells the outputs apart.
- `test/renderer_switcher_test.dart`:
  - While external, the button renders nothing and the dropdown shows the attribution line, which opens no picker.
  - Both come back with queue control, and stay under a plugin playing on a renderer.
  - The crossed glyph for an empty renderer list is unchanged.
- `test/now_playing_external_test.dart`, new, modelled on `now_playing_exclusive_test.dart`:
  - With a Roon external control, a renderer in the list and an unsupported volume, Now Playing shows Roon's title, artist, artwork and progress, the header line and the note, and no `Slider` or cast control. Transport reaches the plugin.
  - With a supported volume, the slider shows.
  - Switching the control to the queue brings the dropdown back.
- `test/mini_player_test.dart`: no cast button while external.
- `test/kiosk_test.dart`:
  - While external, the output panel shows the line and no switch rows.
  - The pill opens nothing without volume and opens the panel with it.
- `test/volume_control_slider_test.dart`: the slider is hidden while the bus says unsupported during playback, and shown again when a supported volume arrives while playing.
- `test/coach_marks_overlay_test.dart`: the output stop is left out while external.

### Android

`KalinkaRoutingTest` and `KalinkaVolumeKeysTest` (Robolectric):

- `playback_control_changed` exclusive followed by `current_renderer_changed` null leaves `state.current` null and `volumeControllable` false. Dispatch returns false for the volume keys, `outputChanged(false)` is reported, and no `PUT /device/set_volume` is sent.
- `current_renderer_changed` with the renderer restores it with exactly one `GET /device/get_volume`, and no stale controller id survives the gap.

### On a device

Steps 1, 5 and 6 of the app's `docs/android-output-switcher.md` checklist, with a Roon hold:

- No Kalinka notification, no Kalinka chip in the output switcher, and the volume keys adjust the phone.
- After Roon releases and the queue plays, the chip, the notification and the keys return.

## Files

Server: `kalinka_plugin_sdk/datamodel.py` and `__init__.py`, the SDK README and version; `renderer_registry.py`, `output_device_router.py`, `external_playback.py`, `server.py` (`_renderer_selection`), `player_setup.py` (wiring), `version.py`; the server's SDK pin, `control.in` and RELEASING.md.

App: `data_model.dart`, `app_state_provider.dart`, `renderer_switcher.dart`, `now_playing_content.dart` (the shared copy), `kiosk/kiosk_output_panel.dart`, `kiosk_screen.dart`, `music_player_screen.dart` (the coach-mark stop), and `CHANGELOG.md` with the server version the hidden chip needs.

## Rejected

- **An `output` object on `GET /renderer/active` and the renderer events** (`kind` renderer, external or none, with `plugin_id` and `title`). It repeats what `playback_control` already carries, adds a third representation to the replay state and to the Android parser, and puts plugin facts on a renderer resource. Its "none" is already a null renderer under queue control.
- **A flag alone, keeping `renderer_id` set to the shared-DAC renderer.** Old clients would keep naming a renderer and offering a slider for audio it is not playing, `/renderer/list` would still mark it active, and Android would keep a live volume provider for an idle renderer. #254 asks for the registry to report no renderer.
- **Deriving "external" from exclusive control plus a null `renderer_id`.** It is implicit, depends on two events arriving together, and cannot tell an external engine from a renderer holder whose renderer vanished mid-hold.
- **Making `active_id()` report None during a hold.** On its own, it would refuse the queue and Qobuz Connect taking over from Roon, drop amp delegation during a hold, and stop output capability advertising, all pinned by existing tests. With a new `target_id()` for internal callers, six would have to move: queue play, Qobuz Connect's takeover, the hold's own renderer, amp delegation, capability advertising and the speaker test. A caller added later that reached for `active_id()` would silently bring those bugs back. Hence `serving_id()` for the three that report.
- **Keeping the `renderer_id` callable beside the registry.** Both would come from the same registry, and a caller passing only the registry would get the default `lambda: None`: holds that `vacate` can no longer find.
- **A stub volume device that answers "unsupported".** `resync()` already maps None to that state and every `/device/*` route already treats None as no control, so a stub would be a second way to say the same thing.
- **Gating the slider on `isExternal` in the client.** It would hide the slider for a hold that brings `volume_control`. The device bus is the one source of truth for volume.
- **Native Android changes so the hardware keys drive an external hold's volume.** Native volume is tied to a media route, and no renderer route exists during a hold. The Roon plugin offers no volume today, so this is a named follow-up rather than part of this design.
