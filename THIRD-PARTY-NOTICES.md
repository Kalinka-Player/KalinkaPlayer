# Third-party notices

Kalinka is licensed GPL-3.0-or-later (see [LICENSE](LICENSE)). It bundles, links against, or downloads at runtime the components below, each under its own terms. This list covers third-party material; it is not a dependency manifest.

## Project branding

The Kalinka logo, icon and related artwork under `docs/images/` are **not** under the GPL that covers the source. They are licensed separately — see [LICENSE-ASSETS](LICENSE-ASSETS) — and require the author's permission to use or redistribute.

## Bundled source

| Component | Where | License |
|---|---|---|
| [minimp3](https://github.com/lieff/minimp3) | `packages/kalinka-renderer/src/native_player/minimp3/` | CC0-1.0 (public domain dedication) |

minimp3 is vendored verbatim, with its upstream notice retained in the headers.

## Linked by the renderer (C++)

These are system libraries resolved at build time; the Debian, RPM and Flatpak packages depend on the distribution's own builds rather than shipping copies.

| Component | License |
|---|---|
| [FLAC / libFLAC++](https://xiph.org/flac/) | BSD-3-Clause |
| [ALSA (libasound)](https://www.alsa-project.org/) | LGPL-2.1-or-later |
| [libcurl](https://curl.se/) | curl license (MIT-style) |
| [Protocol Buffers](https://protobuf.dev/) | BSD-3-Clause |
| [Boost](https://www.boost.org/) | BSL-1.0 |
| [spdlog](https://github.com/gabime/spdlog) | MIT |
| [GoogleTest](https://github.com/google/googletest) (tests only) | BSD-3-Clause |

## Python dependencies

Installed from PyPI at build or install time, not redistributed in source form. Principal ones: FastAPI, Starlette, Uvicorn and Pydantic (MIT); zeroconf, ssdpy, netifaces, aiosqlite, musicbrainzngs and watchfiles (LGPL/MIT/BSD per project); Mutagen (GPL-2.0-or-later); Pillow (MIT-CMU); httpx, requests, PyYAML, schedule, rapidfuzz and sqlite-vec (MIT/Apache-2.0/BSD per project). Each package carries its own license metadata in the installed environment.

## Downloaded at runtime

Not shipped with the packages. Fetched on first use when the corresponding feature is enabled, and subject to the terms of their publishers.

| Component | Used for | Notes |
|---|---|---|
| [LAION CLAP](https://github.com/LAION-AI/CLAP) (ONNX conversions) | Smart Search over local audio | Model weights carry their own terms; see the upstream project |
| Sentence-Transformers MiniLM (ONNX conversion) | Shared text embedder | Apache-2.0 upstream |
| Jamendo caption index | Smart Search over the Jamendo catalog | Derived from the Jamendo API under its terms of use |

## Services

The Local Library enricher talks to [AcoustID](https://acoustid.org/), [MusicBrainz](https://musicbrainz.org/), Wikidata and Deezer; the Jamendo plugin talks to the [Jamendo API](https://developer.jamendo.com/). Each requires you to supply your own API key where one is needed, and use is subject to those services' terms rather than this project's license.

## Appliance images

The `kalinka-image-v*` releases are Debian 13 systems with Kalinka installed. Everything outside this repository in those images is Debian-packaged software under its own license; `/usr/share/doc/<package>/copyright` inside a running image is authoritative.
