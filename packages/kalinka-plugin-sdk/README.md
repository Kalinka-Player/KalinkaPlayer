# Kalinka Plugin SDK

A Software Development Kit for developing input modules and device plugins for the Kalinka Player.

## Overview

The Kalinka Plugin SDK provides the necessary interfaces and base classes for creating:
- Input modules for different music streaming services
- External device plugins for audio output control

## Installation

```bash
pip install kalinka-plugin-sdk
```

## Usage

### Creating an Input Module

```python
from kalinka_plugin_sdk import InputModule, TrackInfo, SearchType

class MyInputModule(InputModule):
    @property
    def module_name(self) -> str:
        return "my_music_service"
    
    def search(self, query: str, search_type: SearchType, offset: int = 0, limit: int = 25):
        # Implement search functionality
        pass
    
    def browse(self, entity_id=None, offset: int = 0, limit: int = 25):
        # Implement browse functionality
        pass
    
    def get_track_info(self, track_ids):
        # Implement track info retrieval
        pass
```

### Creating an External Device Plugin

```python
from kalinka_plugin_sdk import ExternalOutputDevice, DeviceVolume

class MyDevice(ExternalOutputDevice):
    def get_volume(self) -> DeviceVolume:
        # Implement volume getting
        pass
    
    def set_volume(self, volume: int) -> None:
        # Implement volume setting
        pass
    
    def power_on(self) -> None:
        # Implement power on
        pass
    
    def is_power_on(self) -> bool:
        # Implement power status check
        pass
    
    def power_off(self) -> None:
        # Implement power off
        pass
```

## API Reference

### Core APIs
- `Track.image` (3.20+): the cover to show for one track, where it can differ from its album's — a compilation whose tracks embed their singles' sleeves, or a disc with its own cover. Fill it with the track's own cover when it has one and the album's otherwise; leave `album.image` the album's canonical cover. Clients show `track.image` in the queue, Now Playing and track lists, falling back to `album.image` when it is absent, and keep album grids and headers on `album.image`.
- `TrackSource.audio_codec` and `PlaybackState.audio_codec` (3.19+): optional encoded audio codec for display, such as `flac` or `aac`. The server carries it through queue and direct playback. Keep `TrackSource.format` as the renderer's source format (`application/dash+xml` for DASH, `audio/mp4` for MP4); it still selects the decoder and is reported as `mime_type`. Clients prefer `audio_codec` for their format label and fall back to `mime_type` when it is absent. Leave it unset when the codec is unknown.
- `ExternalPlayback` (3.7+): an input plugin whose engine owns audio output (for example Roon Bridge) acquires `InputPluginContext.external_playback`, then reports `PlaybackState` metadata and progress without opening a renderer. Its async `on_revoked` callback must stop external audio within three seconds, including an engine-termination fallback when network control fails. The server waits for that callback before handing playback to the next source. Do not reenter acquisition/release from that callback. Renderer selection changes revoke external playback rather than migrating it. `release()` is called after the plugin has stopped its own engine. Position is in milliseconds; report timestamps are assigned by the server. Older servers expose `None`.
- `InputModule.get_track_source(track_id)` (3.8+): where one of the module's tracks plays from, asked each time the track is about to play rather than when it is queued, so the queue keeps a track while its module is starting, offline or switched off. Raise `SourceUnavailableError` for a track that exists but cannot be served now, `LookupError` for one the module does not have. It has 8 seconds where other calls have 3. The default resolves through `get_track_info` and the track's `source_retriever`; override it to answer from the id alone.
- `PlayQueueAPI`: Interface for playqueue operations. Since 3.8 `add` takes `Track`s, the queue asking each track's module for its source at play time; a `TrackInfo` passed to it keeps only its metadata.
- `PlayQueueController.replace(tracks)` (3.10+): clears the queue and adds `tracks` in its place as one command, so no other caller finds the queue empty in between and the playback state goes straight to the new first track. A queue from before 3.10 falls back to a clear and then an add.
- `ExternalPlayback.supports_volume` (3.11+): when true, pass `volume_control` to `acquire()` and publish confirmed `DeviceVolume` values with `session.report_volume()`. The controller's async `set_volume()` receives slider requests while the hold owns volume, including when paused. A configured downstream amplifier retains precedence. Ending the hold restores renderer controls and cancels pending volume requests. Report `supported=False` for fixed or unavailable volume; older servers lack the capability property, so check it with `getattr(..., "supports_volume", False)`.
- `InputModule.capabilities()` and `ModuleCapability` (3.13+): the optional calls a module answers as it runs now, which the server lists by value in the module's `capabilities`. The default reports `AI_SEARCH` (`"ai_search"`) when the module overrides `ai_search`; a module that implements it behind a setting overrides `capabilities()` and leaves `AI_SEARCH` out while the setting is off.
- `ModuleCapability.CATALOGS` (3.14+, `"catalogs"`): the module's browse root leads to catalogs a client may offer as a home-screen source and pin with filters. Nothing infers it, since a receiver implements `browse` with no catalog behind it: a module that has catalogs reports it from `capabilities()`, and a receiver-only module leaves it out. Servers before 3.14 list no such value.
- `ModuleCapability.RANKED_SEARCH` (3.22+, `"ranked_search"`): `search()` returns each kind's hits best first, by the source's own relevance, and the server keeps that order instead of ranking hits by how their names answer the query. A streaming service's search reports it; a source that lists hits alphabetically, as a library does, leaves it out so the server orders them by name. The server merges kinds and sources by reciprocal rank and sets `NameMatch.relevance` on every hit (3.22+), which a client merging several sources' hits sorts by. Servers before 3.22 list no such value and order every source's hits by name tier.
- `InputModule.root_preview()` (3.22+): how a client lays out the module's root, which the server sends as the `preview_config` of the module's entry in the browse root. Return an `IMAGE_TEXT` preview for a root a client opens as cards; the default, `None`, leaves the layout to the client, which shows rows. Servers before 3.22 never ask.
- `TextEmbedder.missing_runtime()` (3.12+): import names of the packages the shared text embedder runs on that this box cannot import, empty once it can run. The server no longer installs them, so a plugin that embeds offers each as an optional package and reports it in `missing_packages`. Older servers installed the runtime with themselves and lack the method, so ask with `getattr(context.embedder, "missing_runtime", None)`.
- `OptionalPackageSpec.architectures` (3.12+): the architectures an optional package has wheels for, empty for all. `has_wheels_for(wheel_architecture())` says whether an install can succeed on this box; where it cannot, a plugin leaves the package out of `missing_packages` and `required_packages()` and reports the feature unavailable on this device rather than one install away.
- `optional_packages_manifest(plugin_id, packages)` (3.12+): the allow-list a plugin's package ships under `/opt/kalinka/allowed_packages`, which the install helper checks every request against. The package builds run the module declaring `OPTIONAL_PACKAGES` as a script, which ends with `if __name__ == "__main__": print_optional_packages_manifest(PLUGIN_ID, OPTIONAL_PACKAGES)`.
- `EventEmitterAPI`: Interface for dispatching events
- `EventListenerAPI`: Interface for subscribing to events
- `LoggerAPI`: Interface for logging
- `PluginContext`: Context provided to plugins
- `DirectPlayback` (3.4+): an input plugin plays on the renderer outside the play queue, as a Connect receiver does; offered as `InputPluginContext.direct_playback`, `None` on older servers
- `DirectPlaybackSession.set_next(source, track)` (3.6+): immediately queues one successor on the renderer, regardless of duration. Pause and seek keep it queued; `set_next(None)`, `play()`, and `stop()` remove it. Plugins decide when to resolve and supply a URL. Implement `DirectPlaybackListener.on_next_started(track)` to update upstream state when the renderer advances, without replaying the track. `on_state` then reports its metadata and progress; `on_finished` only fires when playback ends without a queued successor. Queued sources must be replayable, not sequential.
- `DirectPlaybackSession.show(track, position_ms)` (3.11+): changes the track clients are shown, and where its position counts from, without touching what plays. For a continuous stream whose content moves on by itself, as an AirPlay or Bluetooth sender's does at a track change; `play()` would restart a sequential source. `play()` shows its own track and timeline again. Sequential sources only; raises `ValueError` for another source or while nothing plays
- `DirectPlayback.acquire(title, listener, seekable=False)` (3.11+): for playback no client can seek, such as a receiver's live stream. `PlaybackControl.seekable` tells clients to offer no position control, and a seek they send anyway never reaches the listener
- `DirectPlaybackSession.stop()` (3.6+): stops the source while retaining the renderer session and volume for another track; `release()` gives the output up immediately
- `DirectPlayback.watch_output_capabilities(listener)` (3.9+): follows what the output `acquire()` would take can play, without taking it. `OutputCapabilitiesListener.on_output_capabilities` is called at once, then on every change: a settings change, a reconnect, or playback moving to another renderer. The returned function stops the calls. `OutputCapabilities.dsd` is True while the renderer takes DSD, False while it does not, and None while the server does not know, as with a renderer too old to say; a plugin that advertises formats to a controller lists DSD only while it is True, and advertises again when it changes
- `PlaybackControl` (3.4+): whether the play queue drives the output or an input plugin holds it exclusively; `PlayQueueState.playback_control`, announced by `PlaybackControlChangedEvent`
- `ModulesChangedEvent` (3.17+): every module and device as `GET /server/modules` lists them, as `ModuleDescriptor`s, sent when a health check finds that one of them reports something new. `PlayQueueState.modules` holds the latest as a `ModulesSnapshot`; it is `None` on older servers, which never send the event
- `Album.year` (3.18+): the year the release came out, where the source knows it. Clients show it beside the album's title; `None` leaves it out
- `LiveContent` (3.5+): `ContentInfo.live` exposes an unfinished resource through asynchronous readers, outside the module RPC budget. Its final `size` is `None` until complete; `open(start, end)` pins a reader, and `LiveContentError` explicitly refuses unavailable reads. The server sends unknown-length, non-range HTTP while capture is in progress and finite ranges only after completion. Plugins must bound storage, reader count and idle waits, and wake readers on cancellation. The server abandons a reader that produces nothing for 30 minutes. The renderer decodes live Ogg/Vorbis and FLAC; MP3 plays only once the resource is complete.
- `LiveCapture` (3.11+): a ready-made `LiveContent`, in `kalinka_plugin_sdk.live_capture`, for a plugin that produces a stream while the renderer plays it. The producer `append()`s, `finish()`es or `retire()`s; bytes live in temporary files bounded by `max_bytes` and stay until the last reader closes. With `keep_delivered=False`, for a stream with no end in sight, bytes go once read and `max_bytes` bounds only how far the producer runs ahead of the reader. An unfinished capture serves one reader from byte 0 and refuses a reconnect, so a plugin serves a new capture instead
- `TrackSource.sequential` (3.5+): a source that cannot be reopened at arbitrary offsets. It plays only through direct playback, which refuses to seek it or start it partway and ends its hold on renderer changes/reconnects instead of replaying stale audio; the play queue refuses it. Progress arrives as timestamped control points on real state transitions, not periodic ticks; a plugin pacing itself extrapolates from the latest point's `position` and `timestamp_ns`. `timeline_offset_ms`, only on a sequential source, shifts the reported media position without seeking the decoder; a plugin starts a fresh independently decodable resource after seeking upstream.

### Base Classes
- `InputModule`: Base class for input modules
- `ExternalOutputDevice`: Base class for external device plugins
- `ModuleConfig`: Base class for module configuration

### Data Models
- `EntityId`: Represents an entity identifier
- `TrackInfo`: Contains track information
- `DeviceVolume`: Represents device volume information

### Events
- `EventType`: Enumeration of available event types
- Various event classes for different event types

### Plugin artwork (3.18+)

A plugin may declare image artwork on its `CONFIG_MODEL`. Both `small`
(compact attribution) and `standard` (other source displays) are required
when it opts in. Existing plugins need no change. The server publishes
`icons` in `ModuleDescriptor`, module-change events and settings cards;
the existing Material `icon` remains the fallback for older clients or
images a client cannot load or render.

```python
from pathlib import Path
from typing import ClassVar
from kalinka_plugin_sdk import ModuleConfig, ModuleIconFiles

class MyConfig(ModuleConfig):
    __module_icon__: ClassVar[str] = "music_note_outlined"
    __module_icon_files__: ClassVar[ModuleIconFiles] = ModuleIconFiles(
        small=Path(__file__).with_name("icons") / "small.svg",
        standard=Path(__file__).with_name("icons") / "standard.svg",
        monochrome=True,
    )
```

Include the files as package data in the plugin's wheel. The server reads
them once and serves them at `/server/module-icons/{sha256}`, including
while the plugin is disabled or in error. GET and HEAD support ETags and
immutable caching. Files must be SVG with a `viewBox`, or PNG, at most
256 KiB each, and stay unchanged until the server restarts. Metadata carries
their MIME types and width/height `aspect_ratio`; file paths never reach
clients. Invalid or missing files leave the fallback icon available.

Set `ModuleIconFiles.min_height` when a brand requires a minimum displayed
height in logical pixels. It is carried as `min_height` on each `ModuleIcon`
and defaults to zero; for example, Spotify's icon uses 21. Catalog entries
can publish the same `icons` metadata with hosted URLs for display before
installation. Installed plugins keep their packaged assets available locally.

For artwork already hosted elsewhere, `__module_icons__: ClassVar[ModuleIcons]`
can instead supply `ModuleIcon(url=..., mime_type=..., aspect_ratio=...)` for
each variant. URLs are absolute HTTP(S) or rooted at the Kalinka server;
they must be reachable without credentials. Packaged files take precedence.

SVG and PNG may be mixed. The two variants may reference the same SVG
when no different small-size drawing is needed. Prefer simple,
self-contained SVG paths without scripts, external resources, embedded
bitmaps or font dependencies. For PNG, use transparent backgrounds and
size the files for the largest intended logical size times the supported
device pixel ratio; 96 px small and 256 px standard are useful starting
points, not required dimensions. Clients preserve aspect ratio and fit
the image without cropping, including any supplied clear space.

`monochrome=True` means the supplied artwork is an alpha mask that may
be drawn black on light backgrounds and white on dark backgrounds. It
does not request a source's accent color. The default `False` preserves
the image's original colors. Use approved monochrome brand assets, and
respect their minimum displayed size and attribution requirements: more
pixels in a PNG do not make an undersized on-screen logo acceptable.

The app's attribution mark is 18–20 logical pixels high; a compact wordmark
can be wider, using its `aspect_ratio`, without increasing the row height.
The app rasterizes SVGs to PNG data at 96 px (small) or 256 px (standard) on
the longest edge, once per retained URL/variant. Concurrent uses share the
conversion. A separate 64-entry cache retains these PNGs across navigation
and album-image cache eviction; theme changes tint the bitmap. PNG inputs
skip SVG conversion. An evicted entry or a changed URL loads again.

Plugins importing these types require `kalinka-plugin-sdk>=3.18,<4` in
their wheel/deb dependencies and `REQUIRES_SDK` declaration.

### Storage and media (3.20+)

- `kalinka_plugin_sdk.mounts`: `list_mounts`, `covering_mount`, `is_network_fs` read `/proc/self/mountinfo`; `block_facts("8:1")` reads what udev recorded of a block device (label, UUID, bus); `KALINKA_MEDIA_ROOT` is where the server mounts the drives and shares it manages.
- `kalinka_plugin_sdk.files`: `is_housekeeping_dir`, `is_hidden_file` and `in_housekeeping` tell recycle bins, snapshots and resource forks from music.
- `kalinka_plugin_sdk.media`, with the `media` extra (`kalinka-plugin-sdk[media]`, which brings mutagen and Pillow): `formats` names the playable formats; `tags.TAG_READERS` reads each one's tags and cover, `read_summary` and `read_cover` read a local file; `folder_art.find_folder_cover` picks a cover from the images beside an album; `images.resize_to_jpeg` makes the artwork sizes.
- `PluginBase.config_changed(changed)` is called after the user saves settings of the plugin, with the paths that changed, so a plugin can apply them without a restart.
- `LibraryAdopter` is how a library plugin keeps a folder from a drive. It implements `location_status(location)`, answering with a `LocationStatus` that says whether it already holds the folder and whether one of its sources is exactly that folder, plus `adopt_location` and `release_location`, which return the `SourcesChange` that adds or removes the folder: one record added to, or removed by id from, a record list of its settings. The server asks only plugins that implement all three.
- `StorageBacked` is an input module whose pages are folders on storage; it answers `storage_location(entity_id)`.
- `Catalog.live` marks a listing that changes without Kalinka, so a client lists it again each time it is shown; `Catalog.on_storage` marks a folder of a mounted volume. `ModuleDescriptor.count` is a number a client may show on the source's icon.
- `BrowseItem.icon` names what a client draws where the item has no art (`track`, `folder`, `usb_drive`, `network_share`, `disk`, `internal_storage`), in place of art it would make up.
- `VolumesChangedEvent` on the queue bus, and `PlayQueueState.volumes`, carry the server's volumes as `VolumeDescriptor`s, each with a `summary` to show under its label.

Plugins using these require `kalinka-plugin-sdk>=3.20,<4`.

## License

GPL-3.0-or-later

## Contributing

Please refer to the main Kalinka Player repository for contribution guidelines.


## Live receiver audio (SDK 3.21)

Bluetooth/AirPlay-style inputs can publish decoded PCM through
`context.live_audio` and use `hold.play_live(source, track, policy=LivePolicy())`.
Core owns the bounded memory buffer, HTTP delivery and renderer switching.
The producer must keep draining independently of output acquisition and call
`source.close()` on disconnect/shutdown; it must not encode or spool media.

```python
from kalinka_plugin_sdk import PcmFormat, LivePolicy

source = context.live_audio.create_pcm(PcmFormat(48000, 2, "S24_3LE"))
# In the receiver's event-loop task, without output I/O:
source.offer(complete_pcm_frames)
# In a separate control task:
hold = await context.direct_playback.acquire("Bluetooth", listener, seekable=False)
await hold.play_live(source, track, policy=LivePolicy(60, 250))
```

`offer()` is synchronous and accepts complete interleaved little-endian frames.
Old retained blocks are evicted without blocking reception. `reset(format)`
starts a new epoch after a discontinuity; `show()` only updates metadata.
A compatible renderer move preserves capture and ownership, whereas a queue
or other-input takeover still revokes the hold. Pause/resume starts fresh audio.
Older renderers without `OutputCapabilities.live_pcm >= 1` report an upgrade
requirement. See the [live audio design](../../docs/bluetooth-audio-sink-design.md)
for wire format, lifecycle and validation limits.

## Optional receiver controls (SDK 3.21)

Receiver plugins may declare a `ReceiverDescriptor` in `RECEIVER`, opt into
`SETUP_WHEN_DISABLED = True`, and implement `get_receiver_control()` returning a
`ReceiverControl`. Its asynchronous `snapshot()` reads cached control state;
`execute(ReceiverCommand)` performs one bounded action and returns a snapshot.
Use `ReceiverError(code, message)` for expected refusals.

Initialize the controller and input interface once, even while disabled. Handle
live `enabled`/name changes in `config_changed()` and return `True` for the
saves it took in whole — for any other the server shuts the plugin down and
sets it up again on the new settings — report disabled health until enabled,
and clean up tasks in `shutdown()`.

`InputPluginContext.server_name` is an optional getter for the current streamer
name. `server_name_changed()` lets an inheriting receiver update its name.
`context.receiver_changed` is an optional no-argument notification callback;
call it for control changes, never PCM blocks or playback progress.

The public models and typed command union live in `receiver_control` and are
also exported at package level. See [the API contract](../../docs/receiver-control-api.md)
for wire shapes, opaque target handles, idempotence and client reconciliation.
Existing plugins need no changes unless they implement this capability.
