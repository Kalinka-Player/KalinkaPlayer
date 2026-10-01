# Kalinka UPnP renderer

Bundled input plugin exposing Kalinka as a UPnP MediaRenderer. Enable **UPnP**
in the input source settings, select an output renderer in Kalinka, then select
the configured device name in a UPnP control app on the same network.

The controller supplies an HTTP(S) audio URL and optional DIDL-Lite metadata.
The selected renderer fetches the resource directly; the plugin does not
download, transcode, or add it to the Kalinka queue. MP3, FLAC, and Ogg/Vorbis
are advertised, matching the native renderer's decoders. Extensionless URLs
need a supported MIME type in the matching DIDL `res` element. The URL must
be reachable from the renderer and support byte ranges for seeking.

`Play` takes exclusive control through SDK `DirectPlayback`. Kalinka's queue
keeps its contents. Queue playback, another input taking control, renderer
loss, or idle revocation stops the UPnP session and notifies its subscribers.
Only a new controller `Play` can acquire another hold. `Stop` and end of the
last track retain the renderer session and its volume for the server's normal
15-second idle timeout. A following track reuses that session. Selecting another
renderer while stopped releases the retained hold. Disabling the plugin and
shutdown release the output immediately. This requires SDK 3.6.

AVTransport supports URI loading, play, pause, stop, absolute/relative time
seeking, seeking to the current track, one next URI, and up to 32 previous tracks.
Replacing the URI while playing or paused preserves that transport state.
A next URI is queued immediately through `DirectPlaybackSession.set_next`,
even when duration is unknown. Pause and seek keep it queued; manual Next/Previous
or a new current URI cancels the previous successor before queuing the correct one.
The renderer advances automatically and reports the transition. Previous
walks the tracks supplied so far; Next walks forward again or plays the supplied
next URI. Kalinka cannot skip to a track the controller has not supplied.
Kalinka transport commands drive the same session. Renderer playback and
volume updates go back to control points through GENA `LastChange` events.
UPnP control points observe these events; there is no separate command API
on the sending phone that the receiver calls.

With [BubbleUPnP's standard UPnP AV playback](https://bubblesoftapps.com/bubbleupnpserver2/docs/features_and_requirements.html),
the playlist belongs to the controller. If it does not supply
`SetNextAVTransportURI`, Next cannot advance into that private playlist.
`GetCurrentTransportActions` and GENA report Next/Previous only when available;
Kalinka clients currently display both buttons for all exclusive inputs.
Unavailable button requests are logged without putting playback into an error state.

RenderingControl exposes Master volume (0–100), mute, and the factory preset.
Volume is scaled to the output device's range. Mute temporarily sets volume
to zero and restores its previous value. Setting volume/mute requires an active
UPnP hold and an output that supports volume, since the SDK exposes device
control through that hold. Queries outside a hold report the last known values.
ConnectionManager publishes the supported protocols and connection 0.

The plugin defaults to disabled. Settings include the advertised name, a local
IPv4 interface address (empty selects the default interface), and the HTTP
port (49152). Allow UDP multicast `239.255.255.250:1900` and inbound TCP on
the configured port. One IPv4 interface is advertised. The device UUID persists
under Kalinka's state directory in `upnp/uuid`, including with `KALINKA_PREFIX`.
GENA callback URLs must use the subscribing controller's literal IP address;
subscriptions expire after at most 30 minutes and can be renewed. Delivery is
ordered with bounded queues, subscriber count, and request timeouts.

The package follows the bundle's `kalinka-v*` version and is picked up by
`make dev-setup`, `make test`, the Debian plugin build, and the server RPM build.
To run its tests: `python -m pytest packages/kalinka-plugin-upnp/tests`.

Protocol references: [MediaRenderer:1](https://upnp.org/specs/av/UPnP-av-MediaRenderer-v1-Device.pdf),
[AVTransport:1](https://upnp.org/specs/av/UPnP-av-AVTransport-v1-Service.pdf),
[RenderingControl:1](https://upnp.org/specs/av/UPnP-av-RenderingControl-v1-Service.pdf),
and [ConnectionManager:1](https://upnp.org/specs/av/UPnP-av-ConnectionManager-v1-Service.pdf).
