# Final settings copy

Final descriptions and popup text for renderer and module settings, applied in KalinkaPlayer and the KalinkaAI client.

Keep descriptions short and use plain language. Connect related clauses when it reads naturally. Keep requirements, special values and important consequences beside the control. Put longer explanations in an optional info popup.

Descriptions that are already clear can stay as they are. Empty descriptions can stay empty. “No description” means omit the helper text.

## Renderer settings

Sources: [NativePlayer.cpp](../packages/kalinka-renderer/src/player/NativePlayer.cpp), [RendererName.cpp](../packages/kalinka-renderer/src/config/RendererName.cpp) and [renderer_core_settings.py](../packages/kalinka-server/src/kalinka_server/renderer_core_settings.py).

| Setting | Description |
|---|---|
| Name | Name shown in Kalinka. Leave empty to use the default name, which includes the hostname. |
| Name overridden by command line | Set by the `--name` command-line option. |
| Output section | *(No description.)* |
| Driver | Audio backend used to reach the sound card. |
| Output device | Changing the device interrupts playback and sets DSD output to Disabled. |
| Output device unavailable | Device not found. It may be disconnected or renamed. |
| Volume control | *(No description.)* |
| Volume: Automatic | Use the sound card’s volume control when available, otherwise software. |
| Volume: Card mixer | Use the sound card’s volume control. Needs a card that has one. |
| Volume: Software | Adjust volume in the renderer. |
| Volume: Fixed output | Play at full volume and control volume on your amplifier or receiver. |
| Session start volume ceiling | Highest volume playback can start at. A louder setting is lowered to it. Not used with Fixed output or a connected device. |
| Output latency | Audio buffered ahead of playback. Increase if playback stutters. |
| Pause after a format change | Wait after the audio format changes. Increase if track beginnings are cut off. |
| Reopen on a format change | Close and reopen the sound card when the audio format changes. Some DACs need this. |
| Network buffer | Memory used to buffer streamed audio. |
| FLAC buffer | Memory for audio kept ready when playing FLAC. |
| MP3 buffer | Memory for audio kept ready when playing MP3. |
| Ogg Vorbis buffer | Memory for audio kept ready when playing Ogg Vorbis. |
| WAV buffer | Memory for audio kept ready when playing WAV. |
| DSD buffer | Memory for audio kept ready when playing DSD. |
| AAC and MP4 buffer | Memory for audio kept ready when playing AAC and MP4. |
| Buffering section | Audio held in memory during playback. |
| Stall timeout | Seconds without data before a download is retried. Increase on a very slow connection. |
| Maximum redirects | Maximum redirects when loading a track. Set to 0 to disable redirects. |
| DSD output | Requires an output device marked (direct), and either Fixed output volume or a connected device. |
| DSD: Automatic | Send native DSD when the DAC supports the track’s rate, and never DoP. |
| DSD: Native | Send DSD directly to a compatible DAC. |
| DSD: DoP | Send DSD wrapped in PCM. Choose only if the DAC supports DoP, as other DACs play it as noise. |
| Connected device: Volume and power | The amplifier or receiver this renderer is connected to. Kalinka then controls its volume and power, and the renderer plays at full level from the next track. |
| ReplayGain | Match loudness using ReplayGain tags, leaving untagged tracks as they are. Unavailable with Fixed output unless a connected device controls the volume. |
| ReplayGain: Disabled | Do not adjust loudness. |
| ReplayGain: Album level | Match loudness across albums while preserving volume differences within each album. |
| ReplayGain: Track level | Match loudness across tracks. |

## Output-device choices

Source: [AlsaDeviceEnumeration.cpp](../packages/kalinka-renderer/src/native_player/AlsaDeviceEnumeration.cpp).

| Choice | Description |
|---|---|
| System default | Use the system’s default output, which may mix and resample audio. |
| PipeWire / PulseAudio / JACK | Play through the system’s sound server, which mixes and resamples audio. |
| Direct | Play without conversion. The card must support the track’s format, and other apps cannot use it while Kalinka plays. |
| Converted | Convert audio only when the sound card cannot play its format. |
| Shared | Share the output with other apps and convert audio when needed. |

## Module summaries

| Module | Description |
|---|---|
| [My Library](../packages/kalinka-plugin-localfiles/src/kalinka_plugin_localfiles/config_model.py) | Browse and play music from this streamer or a network share. |
| [Jamendo](../packages/kalinka-plugin-jamendo/src/kalinka_plugin_jamendo/config_model.py) | Stream music from independent artists. Requires a free Jamendo Client ID. |
| [MusicCast](../packages/kalinka-plugin-musiccast/src/kalinka_plugin_musiccast/config_model.py) | Control input, volume and power on a Yamaha MusicCast receiver or speaker. |
| [UPnP](../packages/kalinka-plugin-upnp/src/kalinka_plugin_upnp/config_model.py) | Play from UPnP apps through the selected renderer. |
| [Bluetooth](../packages/kalinka-plugin-bluetooth/src/kalinka_plugin_bluetooth/__init__.py) | Play audio from a phone or computer through the selected renderer. |
| [Dummy device](../packages/kalinka-plugin-dummydevice/src/kalinka_plugin_dummydevice/config_model.py) | Stand-in connected device for development. |

## Library settings

Source: [Local Files config_model.py](../packages/kalinka-plugin-localfiles/src/kalinka_plugin_localfiles/config_model.py).

| Setting | Description |
|---|---|
| Music folders | Folders on this streamer, including mounted drives and shares. To add a network share, update the Kalinka app. |
| Music sources | Folders on this streamer and network shares. |
| Local folder path | Enter the folder path on this streamer. |
| Local source option | A drive, or a share mounted on this streamer. |
| Network share option | An SMB share on a NAS or computer. No mounting needed. |
| SMB server | Server name or IP address, such as 192.168.1.20. |
| SMB folder | Share name, then any folder inside it, such as Music/Jazz. |
| Guest sign-in | Requires guest access on the share. |
| Require encryption | Encrypt the connection. Requires an account sign-in and SMB3 encryption on the server. |
| Scan interval | Minutes between library scans, or 0 to disable scheduled scans. |
| Enable file watching | Scan when files change. |
| Folder-first album grouping | Group albums mainly by folder, starting with the next scan. |
| Repair garbled tags | Enter the original tag encoding, such as cp1251, or leave empty to skip. |
| Upload quiescence window | Seconds a file must remain unchanged before indexing. |
| Rebuild library | Clear the index and artwork cache, then rescan everything. Completed AI analysis is kept. The switch turns itself off afterwards. |
| Enable AI search | Search by mood, genre or sound. Needs about 500 MB of memory. |
| Audio model idle timeout | Unload the indexing model after this many idle seconds, or set to 0 to keep it loaded. |
| Override model path | Choose a custom CLAP model folder, or leave empty to use the standard model. |
| Mood weight | How strongly mood affects ranking in mood searches. |
| Mood candidates before re-ranking | Tracks considered for mood ranking. Higher values are slower. |
| CLAP-text nearest-neighbour fallback | Estimate the mood when the search has no mood keyword. |
| NN fallback confidence threshold | Minimum confidence required to use an estimated mood. |
| Mood backfill batch size | Tracks processed together when calculating mood data. |
| Fill in missing details | Find missing artist, album, genre and artwork details online. |
| Parallel lookups | Metadata lookups to run at once. High values help little, as the services limit request rates. |
| AcoustID API key | Identify tracks by their sound when tags are missing or wrong. [Get a free API key](https://acoustid.org/new-application). |
| Enable Cover Art Archive | Find album covers missing from other sources. |
| Generate missing album art | Create abstract artwork for albums without covers. |

## Other module settings

| Module / setting | Description |
|---|---|
| Jamendo / Client ID | Required. Paste the Client ID of your application in the [Jamendo developer portal](https://devportal.jamendo.com). |
| Jamendo / Audio quality | Streaming format, with MP3 used when FLAC is unavailable. |
| Jamendo / Mood / AI search | Search by mood or description. The search data downloads automatically. |
| Jamendo / Mood index download URL | Download missing search data from this URL, or leave empty to supply the file yourself. |
| MusicCast / Device IP address | Leave empty to find the device automatically. |
| MusicCast / Connected input | Receiver input connected to the renderer’s audio output. |
| UPnP / Device name | Name shown in UPnP apps. |
| UPnP / Network interface address | Enter a local IPv4 address for UPnP, or leave empty to use the default interface. |

## Info popups

Show an `(i)` button for these settings. The popup text is separate from the short description.

| Setting | Popup text |
|---|---|
| Output device | Direct output requires the sound card to support the track’s sample rate and format. Converted output adapts unsupported formats. Shared output allows other apps to play audio too. Changing the device turns DSD output off, as the new card may not support it, and switches Card mixer volume to Automatic when the new card has no volume control. |
| Volume control | Automatic uses the sound card’s volume control when it has one, otherwise software. Card mixer uses the card’s volume control and leaves the audio samples unchanged. Software adjusts the samples and leaves the card at full volume. It works with any card, but quiet levels lose a little quality. Fixed output leaves volume control to an amplifier or receiver. |
| Session start volume ceiling | When playback starts, a volume above the ceiling is lowered to it and a lower volume stays as it is. The check runs when playback starts from a stop or a long pause, not between tracks. |
| DSD output | Plays DSF and DSDIFF files without converting them to PCM. The DAC must support the selected mode and the track’s DSD rate. Unsupported tracks fail to play. |
| ReplayGain | The volume adjusts for each track and returns to its previous level when playback stops. Fixed output does not support ReplayGain unless a connected device controls the volume. A card mixer must report its volume in dB. |
| Pause / reopen after format changes | Some DACs need extra time or must be reopened after a format change. Adjust these settings if tracks fail to start correctly. |
| Network buffer | A larger buffer can help with an unreliable connection, but uses more memory. |
| Scan interval | Setting this to 0 stops scheduled scans, but startup scans, manual rescans and file watching remain available. |
| File watching | Network shares may not report every change, but scheduled or manual scans pick up missed changes. |
| Folder-first album grouping | Uses folders and tags to keep an album together when its tags differ between tracks. |
| Repair garbled tags | This setting repairs tags written with an older character encoding, while UTF-8 repair runs automatically even when the field is empty. |
| AI search | The first analysis of a large library can take hours and uses more memory while it runs. Searches run on this streamer. |
| Override model path | The folder must contain `clap_audio_encoder.onnx`, `clap_text_encoder.onnx` and `clap_tokenizer.json`. |
| Jamendo Client ID | Create an account and an application in the Jamendo developer portal, then copy the application’s Client ID into this field. |

## Popup support

| Area | Change |
|---|---|
| Module settings data | An optional `help_details` field alongside the existing short `help` text. |
| Renderer settings data | `help_details` in `ConfigField` and the fields built by `renderer_core_settings.py`, exposed through the server API. |
| Settings UI | Show one `(i)` button beside a setting when details exist. Open the popup on click, tap or keyboard activation. |
| Popup behaviour | Use the setting name as its title. Provide a close button and return focus to the `(i)` button when closed. |

The KalinkaAI client uses the same popup control for renderer and module settings, including expert settings and setup. Missing or blank details produce no info button.
