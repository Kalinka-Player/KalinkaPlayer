# Jamendo Plugin for Kalinka Music Player

## Overview

An input-module plugin that lets you search, browse and play music from [Jamendo](https://www.jamendo.com) — a catalogue of free, Creative-Commons / royalty-free music — inside the Kalinka play queue, alongside local files and other sources.

> **Disclaimer**: This plugin is an independent, community-developed integration and is not affiliated with or endorsed by Jamendo. It is provided "as is". Streaming via the Jamendo API is intended for personal use; commercial use of Jamendo tracks requires a licence from Jamendo Licensing.

## Configuration

| Setting             | Description                                                                 |
| ------------------- | --------------------------------------------------------------------------- |
| `client_id`         | **Required.** Free Jamendo API key. Register an app at https://devportal.jamendo.com |
| `audio_format`      | Streaming quality: MP3 VBR (default), MP3 96 kbps, OGG, or FLAC.             |
| `ai_search_enabled` | Mood / AI search over the catalogue (default on). The index it needs downloads automatically on first use. |
| `ai_index_path`     | Where the mood-search index is stored (defaults to the Kalinka state dir).   |
| `ai_index_url`      | Where the index is fetched from when missing — leave empty to manage the file yourself. |

## Capabilities

Implemented:

- **Search** — tracks, albums, artists, playlists (Jamendo `namesearch`).
- **Browse** — a discovery root catalog (Popular Tracks, New Releases, Popular Albums, Popular Artists, Featured Playlists) and drill-down into albums, artists and playlists.
- **Playback** — `get_track_source` resolves a track's streaming URL in the configured format from its id alone, each time it is about to play; `get_track_info` returns metadata plus the same resolution.
- **`get`** — detail lookup for a track / album / artist / playlist.
- **Mood / AI search** (`ai_search`) — semantic search by mood or description ("something melancholic for tonight"). The query is embedded with the server's shared text embedder and KNN-matched (sqlite-vec) against precomputed embeddings of the [JamendoMaxCaps](https://huggingface.co/datasets/amaai-lab/JamendoMaxCaps) track captions — textual descriptions of the music, one vector per track. The prebuilt index (~140 MB) is downloaded on first use; no audio analysis runs on the device. Results surface as a "Discover on Jamendo" catalog in the app's AI Search.

Intentionally **not** implemented:

- **Favourites** (`list_favorite`, `add_to_favorite`, …) and **playlist management** (`playlist_create`, …) — these require an OAuth2 user session, which is out of scope. They degrade to empty results / no-ops.
- **`list_genre`** — Jamendo has no genre taxonomy (it uses free-form tags), so this returns an empty list and catalogs disable genre filtering.

## Known limitations

- **No exact result totals.** The Jamendo API reports only the size of the current page, never a grand total. Reported totals are therefore estimates tuned to keep "load more" pagination working.
- **Free-tier rate limits.** The Jamendo API rate-limits requests per client_id; heavy browsing may be throttled.
- **FLAC is best-effort.** Lossless streams exist only for tracks whose artist enabled download; otherwise Jamendo serves a lossy stream.
- **Mood search is tied to the server's embedder.** The index vectors were computed with the exact model and version the server's shared text embedder ships (`all-MiniLM-L6-v2`); on a mismatch — or on a server too old to have the shared embedder (SDK < 1.2) — mood search disables itself rather than return near-random neighbours.

## Building

```bash
./scripts/build_wheel.sh   # build the wheel (version from setuptools-scm)
./scripts/build_deb.sh     # build the .deb (wheel + Debian control)
```

Release versions come from a git tag of the form `kalinka-plugin-jamendo-v1.2.3`; untagged builds produce a dev version.

## Testing

```bash
pytest            # smoke + offline mapping tests (no network/client_id needed)
```
