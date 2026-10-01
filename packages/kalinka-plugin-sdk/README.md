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
- `PlayQueueAPI`: Interface for playqueue operations
- `EventEmitterAPI`: Interface for dispatching events
- `EventListenerAPI`: Interface for subscribing to events
- `LoggerAPI`: Interface for logging
- `PluginContext`: Context provided to plugins
- `DirectPlayback` (3.4+): an input plugin plays on the renderer outside the play queue, as a Connect receiver does; offered as `InputPluginContext.direct_playback`, `None` on older servers
- `DirectPlaybackSession.set_next(source, track)` (3.6+): immediately queues one successor on the renderer, regardless of duration. Pause and seek keep it queued; `set_next(None)`, `play()`, and `stop()` remove it. Plugins decide when to resolve and supply a URL. Implement `DirectPlaybackListener.on_next_started(track)` to update upstream state when the renderer advances, without replaying the track. `on_state` then reports its metadata and progress; `on_finished` only fires when playback ends without a queued successor. Queued sources must be replayable, not sequential.
- `DirectPlaybackSession.stop()` (3.6+): stops the source while retaining the renderer session and volume for another track; `release()` gives the output up immediately
- `PlaybackControl` (3.4+): whether the play queue drives the output or an input plugin holds it exclusively; `PlayQueueState.playback_control`, announced by `PlaybackControlChangedEvent`
- `LiveContent` (3.5+): `ContentInfo.live` exposes an unfinished resource through asynchronous readers, outside the module RPC budget. Its final `size` is `None` until complete; `open(start, end)` pins a reader, and `LiveContentError` explicitly refuses unavailable reads. The server sends unknown-length, non-range HTTP while capture is in progress and finite ranges only after completion. Plugins must bound storage, reader count and idle waits, and wake readers on cancellation. The server abandons a reader that produces nothing for 30 minutes. The renderer decodes live Ogg/Vorbis and FLAC; MP3 plays only once the resource is complete.
- `TrackSource.sequential` (3.5+): a source that cannot be reopened at arbitrary offsets. It plays only through direct playback, which refuses to seek it or start it partway and ends its hold on renderer changes/reconnects instead of replaying stale audio; the play queue refuses it. The server polls real renderer progress once per second while it plays, for pacing. `timeline_offset_ms`, only on a sequential source, shifts the reported media position without seeking the decoder; a plugin starts a fresh independently decodable resource after seeking upstream.

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

## License

GPL-3.0-or-later

## Contributing

Please refer to the main Kalinka Player repository for contribution guidelines.
