# Architecture

The core, the plugins and the renderer each live in their own package. A renderer discovers the server over mDNS and connects out to it, then fetches media over HTTP itself, from a URL the server gives it for each track. What a renderer must do to be one is written down in [`native-renderer-design.md`](native-renderer-design.md), so the bundled ALSA renderer is an implementation of that contract rather than the only possible one.

```
packages/
├── kalinka-server            # Core server, REST/WS API, queue, config (pure Python)
├── kalinka-renderer          # Network audio renderer: native ALSA player (C++)
├── kalinka-plugin-sdk        # Shared plugin interface & helpers (mandatory dependency)
├── kalinka-plugin-localfiles # My Library: indexer, enricher, embedder, searcher (most complete)
├── kalinka-plugin-jamendo    # Jamendo streaming source (Creative-Commons catalog)
├── kalinka-plugin-musiccast  # Yamaha MusicCast volume/power control
└── kalinka-plugin-dummydevice# Mock playback device for development/CI
```

Plugins are ordinary Python packages discovered at runtime via package metadata. You can add or remove functionality without touching the server core. To create your own, start from the cookiecutter template under [`template/cookiecutter-kalinka-plugin/`](../template/cookiecutter-kalinka-plugin/) and read its README. Plugin Debian packaging conventions are documented in [`plugin-deb-packaging.md`](plugin-deb-packaging.md).

## Where the audio comes from

The URL a renderer is given points either at the source or at the server. The plugin that owns the track chooses which, with the [`TrackSource`](../packages/kalinka-plugin-sdk/src/kalinka_plugin_sdk/inputmodule.py) it returns:

- A `DirectUrl` is the source's own address. The renderer fetches straight from it, and no audio passes through the server. Jamendo and UPnP use it.
- A `ModuleAsset` names content the plugin holds, which the server serves at `/content/<module>/<asset>`, on the server address the renderer dialed to register. The renderer then only ever has to reach the server it already talks to, and the plugin never has to know which of the server's network interfaces that is. A live stream a plugin produces as it plays (`ContentInfo.live`) is served this way too.

My Library always uses `ModuleAsset`, so every library track is fetched from the server, even by the renderer on the server's own machine:

- A **Folder on the server** is read from disk by the server process. A share the operating system mounts counts as one.
- A **Network share** is read over SMB by the plugin itself, since the service does not mount shares. The server relays each range the renderer asks for, copying it from the share in fixed-size reads ([`ranged_content.py`](../packages/kalinka-server/src/kalinka_server/ranged_content.py)).

So when sizing a server for a library, count the renderers: each one playing a library track is a stream the server sends, and with a share also a stream it receives, since every byte travels NAS → server → renderer. How much a renderer asks for in each request is its *Network request size* setting (`buffers.network_request`).

## API overview

The server exposes a REST API (FastAPI) plus WebSocket channels for live state. Highlights:

| Area      | Endpoints (examples) |
|-----------|----------------------|
| Queue     | `GET /queue/{list,state,control}`, `POST /queue/{add,replace}`, `PUT /queue/{play,pause,next,prev,stop}`, `PUT /queue/current_track/seek`, `PUT /queue/{mode,clear,move}`, `POST /queue/remove` |
| Browse    | `GET /browse`, `GET /browse/{id}`, `GET /get/{entity_id}`, `GET /genre/list` |
| Search    | `GET /search/{search_type}/{query}` (fuzzy), `GET /ai_search?query=...` (semantic) |
| Library   | `GET /favorite/list/{type}`, `PUT /favorite/add/{id}`, playlists (`/playlist/{create,update,delete,list,add_tracks,remove_tracks}`) |
| Devices   | `GET /device/list`, `GET/PUT /device/{get,set}_volume` |
| Server    | `GET /server/{config,config/schema,version,modules,optional_packages}`, `PUT /server/{config,restart}`, `GET /indexer/status`, `POST /indexer/rescan`, `GET /resource/{file}` |
| Live      | `WS /queue/ws`, `WS /device/ws`, plus SSE-style `GET /queue/events`, `GET /device/events` |

`POST /indexer/rescan` (API 0.16) returns `202 {"status":"accepted"}` after
requesting the localfiles librarian's normal folder check. Requests coalesce
into one pending check, including while a check is already running. The
existing librarian process handles it with the same file-stability and
unavailable-storage guards as scheduled checks. The response is 404 when
localfiles is unavailable, 501 for an older plugin, or 503 when its worker
is unavailable.

`GET /indexer/status` retains its module → stage structure. The localfiles
`indexing` entry is present even when idle and adds `rescan_supported`,
`available`, `scan_pending`, `scanning`, and `last_scan_at` (UTC Unix seconds,
or null before the first completed check). `scanning` covers discovery through
cleanup; progress counters stay zero until changed files have been indexed.
The completion time survives restarts and advances on checks with no changes,
but not on an aborted check. Watcher events do not postpone the periodic check
or update that completion time.

`POST /queue/add` takes a JSON list of entity ids, tracks or containers such as albums, playlists and folders, which it expands to their tracks, and appends them or inserts them at `?index=`. `POST /queue/replace` takes the same list without an index and replaces the queue with it in one request, which is what a client's play-now calls. Past the queue limit (1,000 tracks, 100 on a demo server) both answer 409 and change nothing; for a replacement only the new tracks count. A clear followed by an add would instead have emptied the queue before the add was refused. A replacement that comes to no tracks, such as an empty folder, answers 422 with the code `no_tracks` and also leaves the queue alone. Servers before API 0.12 answer `/queue/replace` with 405 from their browser-player mount at `/`, or with 404 where they have no such mount.

On image boxes, `kalinka-supervisor` serves a separate [control page and API](supervisor-control.md) on port 8001, independent of the server: the page at `/`, `GET /info` (version, protocol and the box's identities), `GET /v1/{status,dashboard,reinstall/log,plugin-removal/log,soundcard,timezones}`, and `POST /v1/actions/{restart_core,reboot,poweroff,reinstall,remove_plugin,set_soundcard,set_timezone}`. `set_soundcard` sets a Raspberry Pi up for its DAC HAT; `set_timezone` sets the box's time zone through systemd-timedated. `GET /renderer/list` gives each renderer's `address`, where a client finds the supervisor of a box that runs only a renderer.

## Configuration & tuning

- The server reads `kalinka_conf.cfg` (JSON): `/etc/kalinka/kalinka_conf.cfg` in production, `$KALINKA_PREFIX/etc/kalinka/kalinka_conf.cfg` from a source checkout. Plugin settings live in the same file, which holds only the values changed from their defaults, so a missing file means a server running on defaults.
- Most settings are editable live from the app's **Settings** screen and persisted to the `.cfg` files. The server exposes its config schema at `GET /server/config/schema` so the app can render forms.
- **Smart Search** is opt-in. For My Library, turn on **AI search** in the localfiles module config — one switch covers both indexing the library and answering queries. On first run it downloads the CLAP ONNX models to the model directory (default `/var/lib/kalinka/models`, or `$KALINKA_PREFIX/var/lib/kalinka/models` when running from source) and embeds tracks in the background; watch progress via `GET /indexer/status`.
- **AcoustID** enrichment needs a free API key from the [AcoustID website](https://acoustid.org/) — set it in the localfiles enricher config. The key is the only switch: no key means the plugin isn't loaded.
- Re-embedding is driven by `CLAP_MODEL_VERSION` in `embedding_utils.py`, not by config; bumping it in code forces a rebuild after a model change. See [`clap_onnx_release.md`](../scripts/clap_onnx_release.md).
