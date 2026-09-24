<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/kalinka_logo.svg">
  <img src="docs/images/kalinka_logo_light.svg" alt="Kalinka" width="380">
</picture>

### Self-hosted Hi-Fi for Raspberry Pi and Linux

Bit-perfect playback from your own library, on your own hardware — with on-device semantic search. No cloud, no subscription, no account.

[![Release](https://github.com/Kalinka-Player/KalinkaPlayer/actions/workflows/release.yml/badge.svg)](https://github.com/Kalinka-Player/KalinkaPlayer/actions/workflows/release.yml)
[![Renderer release](https://github.com/Kalinka-Player/KalinkaPlayer/actions/workflows/renderer-release.yml/badge.svg)](https://github.com/Kalinka-Player/KalinkaPlayer/actions/workflows/renderer-release.yml)
[![Latest release](https://img.shields.io/github/v/release/Kalinka-Player/KalinkaPlayer?filter=kalinka-v*&label=release&color=2ea043)](https://github.com/Kalinka-Player/KalinkaPlayer/releases/latest)
[![License](https://img.shields.io/badge/license-GPL--3.0--or--later-2ea043)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-arm64%20%7C%20amd64-2ea043)](#-requirements)

**[💿 Flash an image](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true) · [📦 Releases](https://github.com/Kalinka-Player/KalinkaPlayer/releases/latest) · [📱 Control app](https://github.com/Kalinka-Player/KalinkaAI) · [🌐 kalinkaplayer.com](https://kalinkaplayer.com) · [💬 Help test it](https://github.com/Kalinka-Player/KalinkaPlayer/discussions/133)**

<img src="docs/images/app-screenshot.png" alt="Kalinka playing a bit-perfect FLAC through a network renderer, with the play queue alongside" width="820">

</div>

---

## What is it

Kalinka turns a Raspberry Pi or any Linux box into a music player you control from your phone, desktop or a browser. The server holds your library; a separate **renderer** does the playing, talking to ALSA directly — so it can sit on the same machine, or on a Pi next to each amplifier in the house.

Your files stay yours — nothing is uploaded, there is no account and no telemetry, and the semantic search runs on the device. Two things do reach the internet, both switchable: the metadata lookups that repair your tags (MusicBrainz, Wikidata, Deezer, Cover Art Archive), and an hourly check for a published release.

## 🚀 Get started

**The easy way — flash a card.** [Ready-to-flash images](https://github.com/Kalinka-Player/KalinkaPlayer/releases?q=kalinka-image-v&expanded=true) carry a minimal Debian 13 with everything already installed — one for the Raspberry Pi 4 / 400 / CM4, one for any x86-64 PC or VM. Write it, boot it, open `http://<its-ip>:8000` and it plays. The filesystem grows into the card by itself and a file on the boot partition sets up a login and Wi-Fi.

**On a machine you already run**, one command installs the server, the plugins, the browser player and a local renderer:

```bash
curl -fsSL https://kalinkaplayer.com/install.sh | sudo bash
```

**Then pick a client.** The [Kalinka app](https://github.com/Kalinka-Player/KalinkaAI/releases/latest) (Android, Linux, Windows) finds the server on its own — or just open `http://<server-ip>:8000` in any browser, which plays audio itself with nothing installed.

Whichever you open walks you through first-time setup — naming the server, pointing it at your music, choosing where the sound comes out. There are no config files to edit; the server has nothing to hand-edit on it.

Full walkthrough, troubleshooting and adding renderers on other machines: **[docs/installation.md](docs/installation.md)**.

## ✨ What it does

| | |
|---|---|
| 🎵 **Your library** | Indexes your directories, watches them for changes, and repairs the metadata — MusicBrainz, Wikidata and Deezer, plus AcoustID fingerprinting once you add a key of your own, falling back to filename heuristics. Artwork is extracted, cached, and generated when there is none. |
| 🔍 **Smart Search** | Ask for *"dreamy ambient guitar"* and get matching tracks. My Library analyses the audio itself with a CLAP model; Jamendo matches a prebuilt index of track descriptions. Opt-in per plugin. |
| 🔊 **Bit-perfect playback** | A C++ renderer with direct ALSA access. FLAC and MP3 up to 192 kHz / 24-bit, gapless between tracks of the same format, and the samples are not altered unless you turn on software volume. |
| 🏠 **Renderers anywhere** | Put a renderer on any box on your network and it appears as an output. They find the server over mDNS and upgrade themselves. |
| 🌐 **Browser player** | The server serves a web player, so any browser is both a remote control and an output. |
| 🧩 **Plugins** | Sources, enrichers and device integrations are ordinary Python packages discovered at runtime. My Library and Jamendo ship in the box; MusicCast handles Yamaha volume and power. |
| 🪄 **Guided first run** | A setup wizard runs in the app and in the browser alike — name the server, point it at your music, pick an output, done. Nothing to edit on the server itself. |
| ⚙️ **Live configuration** | Everything stays editable afterwards from Settings, with a simple tier of common fields and an `about:config`-style search for the rest. |
| 🔄 **Updates itself** | The server checks published releases hourly; with auto-upgrade on it installs during quiet hours while playback is stopped, otherwise the app offers a button. Server, plugins, browser player and renderers move together — renderers first, so the pair never lands on a combination that cannot play. |

## 💻 Requirements

A **64-bit OS is required** — packages are built for arm64 and amd64 only. On Raspberry Pi that means Raspberry Pi OS (64-bit); the Pi 2, Pi 1 and original Pi Zero are not supported.

| Configuration | Minimum hardware | Notes |
|---|---|---|
| Playback + library | Raspberry Pi 3 / Zero 2 W, or any arm64/amd64 box with **512 MB RAM** | Headless OS recommended at 512 MB; enable swap for the first install and large scans. 1 GB is comfortable. |
| With Smart Search | Raspberry Pi 4B with **2 GB RAM**, or any arm64/amd64 box with 2 GB+ | Measured on a 4 GB Pi 4B: ~1.15 GB resident with the library indexed and idle, and ~1.5 GB at the peak — the embedding pass, which loads a second model on top and is CPU-heavy. 2 GB should therefore work; 4 GB is what has actually been tested end to end. Allow ~1 GB extra disk. |

Smart Search can be toggled per install, so you can start small and enable it after moving the library to a bigger board.

## 🔊 About bit-perfect

Audio is played by a renderer, not by the server, so gapless and bit-perfect behaviour depend on which renderer you use.

The default one (`kalinka-renderer`, C++) talks to the sound card directly. It does not alter the audio unless specifically instructed to — software volume being the one thing that does. The app shows whether the current route is 1:1: a `hw:` device takes the samples unchanged, `plughw:` resamples and reformats whatever the card will not accept, and `default` or a sound server mixes with everything else. What it cannot see is a resampler configured elsewhere in your ALSA stack.\*

In-browser playback goes through the browser's audio stack, so there is no gapless and no bit-perfect guarantee there — it is a convenience path.

## 📖 Documentation

| | |
|---|---|
| [Installation](docs/installation.md) | Images, quick install, first-run setup, troubleshooting, renderers elsewhere, updating |
| [Architecture](docs/architecture.md) | How the pieces fit, the package layout, the REST/WS API, configuration and tuning |
| [Development](docs/development.md) | Building the Debian packages, running from source, tests |
| [Renderer design](docs/native-renderer-design.md) | The contract a renderer implements |
| [Releasing](RELEASING.md) | Version model and release procedure |

## 🙋 Help test it

Kalinka is young, and the interesting problems are the ones that only happen on someone else's hardware, with someone else's music. If you try it, say how it went in [the testing thread](https://github.com/Kalinka-Player/KalinkaPlayer/discussions/133) — what you ran it on, where your music lives, what you played it through, and where you got stuck. Setup questions belong there too; something you can reproduce is easier to act on as an [issue](https://github.com/Kalinka-Player/KalinkaPlayer/issues).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to send a change, and for the project's disclosure on AI-assisted development.

## License

Source code is **GPL-3.0-or-later** — see [LICENSE](LICENSE).

The visual assets are not. The Kalinka logo, icon and related artwork under `docs/images/` are covered by the [Kalinka Asset License](LICENSE-ASSETS) and need the author's permission to use. Fork the code freely; put your own branding on it.

Third-party components and their terms are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

---

\* The audio engine uses ALSA directly and relies on its configuration. If automatic resampling is configured it will likely affect the path but should still work. Developed and tested on a Raspberry Pi 4 with a HiFiBerry Digi2 card configured per its manual; it should work with any ALSA-compatible card, though some may need extra quirks.
