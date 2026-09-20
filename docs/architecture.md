# Architecture

The core, the plugins and the renderer each live in their own package. A renderer discovers the server over mDNS and connects out to it, then fetches media over HTTP itself — audio never flows through the core. What a renderer must do to be one is written down in [`native-renderer-design.md`](native-renderer-design.md), so the bundled ALSA renderer is an implementation of that contract rather than the only possible one.

```
packages/
├── kalinka-server            # Core server, REST/WS API, queue, config (pure Python)
├── kalinka-renderer          # Network audio renderer: native ALSA player (C++)
├── kalinka-plugin-sdk        # Shared plugin interface & helpers (mandatory dependency)
├── kalinka-plugin-localfiles # Local Library: indexer, enricher, embedder, searcher (most complete)
├── kalinka-plugin-jamendo    # Jamendo streaming source (Creative-Commons catalog)
├── kalinka-plugin-musiccast  # Yamaha MusicCast volume/power control
└── kalinka-plugin-dummydevice# Mock playback device for development/CI
```

Plugins are ordinary Python packages discovered at runtime via package metadata. You can add or remove functionality without touching the server core. To create your own, start from the cookiecutter template under [`template/cookiecutter-kalinka-plugin/`](../template/cookiecutter-kalinka-plugin/) and read its README. Plugin Debian packaging conventions are documented in [`plugin-deb-packaging.md`](plugin-deb-packaging.md).

## API overview

The server exposes a REST API (FastAPI) plus WebSocket channels for live state. Highlights:

| Area      | Endpoints (examples) |
|-----------|----------------------|
| Queue     | `GET /queue/list`, `POST /queue/add`, `PUT /queue/{play,pause,next,prev,stop}`, `PUT /queue/current_track/seek`, `PUT /queue/{mode,clear,move}`, `POST /queue/remove` |
| Browse    | `GET /browse`, `GET /browse/{id}`, `GET /get/{entity_id}`, `GET /genre/list` |
| Search    | `GET /search/{search_type}/{query}` (fuzzy), `GET /ai_search?query=...` (semantic) |
| Library   | `GET /favorite/list/{type}`, `PUT /favorite/add/{id}`, playlists (`/playlist/{create,update,delete,list,add_tracks,remove_tracks}`) |
| Devices   | `GET /device/list`, `GET/PUT /device/{get,set}_volume` |
| Server    | `GET /server/{config,config/schema,version,modules,optional_packages}`, `PUT /server/{config,restart}`, `GET /indexer/status`, `GET /resource/{file}` |
| Live      | `WS /queue/ws`, `WS /device/ws`, plus SSE-style `GET /queue/events`, `GET /device/events` |

## Configuration & tuning

- Most settings are editable live from the app's **Settings** screen and persisted to the `.cfg` files. The server exposes its config schema at `GET /server/config/schema` so the app can render forms.
- **Smart Search** is opt-in. For the Local Library, turn on **AI search** in the localfiles module config — one switch covers both indexing the library and answering queries. On first run it downloads the CLAP ONNX models to the model directory (default `/var/lib/kalinka/models`, or `$KALINKA_PREFIX/var/lib/kalinka/models` when running from source) and embeds tracks in the background; watch progress via `GET /indexer/status`.
- **AcoustID** enrichment needs a free API key from the [AcoustID website](https://acoustid.org/) — set it in the localfiles enricher config. The key is the only switch: no key means the plugin isn't loaded.
- Re-embedding is driven by `CLAP_MODEL_VERSION` in `embedding_utils.py`, not by config; bumping it in code forces a rebuild after a model change. See [`clap_onnx_release.md`](../scripts/clap_onnx_release.md).
