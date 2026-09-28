# kalinka-renderer

The playback end of Kalinka. A standalone C++ binary that finds Kalinka Cores
on the network, accepts a playback session from one of them, and plays what it
is told to play through the local sound card. Core keeps the play queue, the
library and the UI; the audio graph — HTTP fetch, FLAC/MP3 decode, buffering,
output and volume — lives here.

Output is **ALSA only**, which covers any Linux sink ALSA can reach: a card's
`hw:` device, `default`, a `dmix`/`plug` PCM, or a bridge like PulseAudio's or
PipeWire's ALSA layer. No other backend is implemented, and the settings page
offers ALSA as the only driver. The protocol itself does not assume ALSA — see
[../../docs/native-renderer-design.md](../../docs/native-renderer-design.md).

## Build

Dependencies (dev headers): protobuf (+ compiler), Boost (headers only — asio
and beast), spdlog, ALSA, FLAC++, libcurl, curlpp. GoogleTest is optional and
only builds the test binary. mDNS is self-contained (vendored public-domain
`third_party/mdns`), so no avahi or Bonjour daemon is needed.

- Fedora: `sudo dnf install cmake gcc-c++ protobuf-devel protobuf-compiler protobuf-lite-devel boost-devel spdlog-devel alsa-lib-devel flac-devel libcurl-devel curlpp-devel gtest-devel`
- Debian/Ubuntu: `sudo apt install cmake g++ libprotobuf-dev protobuf-compiler libboost-dev libspdlog-dev libasound2-dev libflac++-dev libcurl4-openssl-dev libcurlpp-dev libgtest-dev`

From the repo root:

```sh
make renderer-build
# or directly:
cmake -S packages/kalinka-renderer -B packages/kalinka-renderer/build
cmake --build packages/kalinka-renderer/build -j
```

A C++23 compiler is required.

## Run

Paths below are relative to this package directory.

```sh
# Discover Cores via mDNS and register with each:
./build/kalinka-renderer --name "Living Room"

# Fixed endpoint (skips discovery), e.g. against a dev server:
./build/kalinka-renderer --server 127.0.0.1:8000

# Background daemon (logs to $KALINKA_PREFIX/var/log/kalinka/renderer.log):
KALINKA_PREFIX=$HOME/kalinka ./build/kalinka-renderer --daemon
```

| Option | Meaning |
|---|---|
| `--name <name>` | Friendly name announced to Cores. Overrides the stored setting for this run; default is the setting, else `Kalinka Renderer on <hostname>`. |
| `--server <host:port>` | Connect to a fixed Core instead of browsing. Repeatable; any use disables discovery. |
| `--daemon` | Detach (double fork, stdio to `/dev/null`). |
| `--log-file <path>` | Log to a file. Defaults to `$KALINKA_PREFIX/var/log/kalinka/renderer.log` when daemonized. |
| `--session-grace <s>` | How long a session outlives its Core going away before it is closed and playback stops (default 60). |

The renderer connects to every Core it finds, but plays for one at a time: the
first to open a playback session holds the audio graph until it closes it,
disconnects for longer than the grace period, or the renderer shuts down. Other
Cores are told `BUSY` and can still read and write settings.

Stop with SIGINT/SIGTERM — playback stops, a protocol Goodbye goes out on every
connection, and the process exits once they have closed (2s hard deadline).

## Settings

Settings belong to the renderer and are edited from any Core's settings page —
no playback session needed. They live in memory, and anything that differs from
the default is written to `config_overrides` in the state directory.

| Path | What it is |
|---|---|
| `renderer.name` | Announced name. Read-only for the run when `--name` was passed. |
| `output.driver` | ALSA. The only choice today. |
| `output.device` | ALSA PCM to open, chosen from the devices enumerated at request time. |
| `output.volume_mode` | `auto`, `hardware` (card mixer), `software`, or `fixed` (ignore volume, play at full level — for an amp that sets the level itself). |
| `output.session_start_volume_ceiling_percent` | Maximum allowed level when a renderer-controlled session starts. It lowers a louder existing level but never raises a quieter one. Fixed output bypasses it. |
| `output.latency_ms`, `output.period_ms`, `output.format_change_delay_ms`, `output.reopen_on_format_change` | How the ALSA sink is opened and driven. |
| `buffers.*` | How much audio is held in memory and how far ahead of the card it runs. |
| `network.stall_timeout_s` | How long a stream may go without receiving a byte, in seconds, before the request is dropped and made again from where it stopped (default 15, 5 to 300). The track fails after four failed requests in a row — stalls, dropped connections or 5xx answers — with less than 16 KB received between them, or after the first from a server that cannot resume. |

Changing the device, the buffering or the stall timeout rebuilds the graph and
stops playback; the schema says so per field, so the settings page can warn
first.

## State on disk

Under `$KALINKA_PREFIX/var/lib/kalinka-renderer` (`KALINKA_PREFIX` defaults to
`/`, the same convention as the server):

- `renderer_id` — stable identity, minted on first run. Cores tell renderers
  apart by it, so losing it makes this renderer look new.
- `config_overrides` — `key=value` lines, only for settings that differ from
  the defaults.

If the directory is not writable the renderer still runs, with an ephemeral id
and settings that last only for the run.

## Upgrading

The renderer runs unprivileged and cannot install anything itself. It asks, by
writing the version it was told to install into `/run/kalinka-renderer/upgrade-request`; the root-owned `kalinka-renderer-upgrade.path` unit picks that up and runs `/opt/kalinka/upgrade-renderer.sh`, which fetches the current `install-renderer.sh` and installs that release. The renderer reports whether that machinery is present in its Hello, so a Core never offers an upgrade to a build installed some other way (flatpak, from source), which could not perform it.

There are two ways in, and the second exists because the first can be cut off:

- **From a Core.** `POST /renderer/<id>/upgrade` sends an Upgrade message down the renderer's own connection. It is carried by every protocol version, so it still reaches a renderer whose protocol the Core no longer speaks — the case where nothing else can. The renderer refuses while a playback session is running.
- **On its own.** `systemctl enable --now kalinka-renderer-upgrade.timer` has the box install the latest published renderer nightly, with no Core involved at all. Shipped inert, because it restarts the renderer to apply — which is why it runs in the small hours. Enable it on a renderer whose Core is not reliably reachable, or where nobody will be around to press the button.

## Test

```sh
cmake --build packages/kalinka-renderer/build --target kalinka-renderer-tests -j
cd packages/kalinka-renderer/build && ctest --output-on-failure
```

The suite covers the transport, protocol and session planes, the config plane,
and the audio graph itself. Playback runs against the ALSA `null` device by
default, which takes frames as fast as they are written — cases whose subject is
what happens *while* a track plays skip unless you point them at a device that
plays in real time:

```sh
KALINKA_TEST_ALSA_DEVICE=default ctest --output-on-failure
```

## Packaging

`make renderer-deb` and `make renderer-rpm` build a stripped Release binary plus
its systemd unit, on (a container of) the distro being targeted. There is also a
flatpak manifest in [flatpak/](flatpak/). The unit runs as `kalrndr` — its own
account, in the `audio` group, separate from the server's `kalusr` so the
network-facing half cannot read the server's config or trigger its upgrades —
and keeps its state in `/var/lib/kalinka-renderer`.

Releases ride their own train — `kalinka-renderer-v*` tags, separate from the
app bundle — and install with:

```sh
./scripts/install-renderer.sh          # latest
./scripts/install-renderer.sh 0.1.0    # a specific version
```

The server's own installer runs that script for the machine it installs on, so
a stock install already has a local renderer and upgrades it along with the
server. Boxes that only render run the script themselves.
