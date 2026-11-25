# kalinka-renderer

The playback end of Kalinka. A standalone C++ binary that finds Kalinka Cores
on the network, accepts a playback session from one of them, and plays what it
is told to play through the local sound card. Core keeps the play queue, the
library and the UI; the audio graph — HTTP fetch, FLAC/WAV/MP3/Ogg Vorbis/AAC decode,
buffering, output and volume — lives here.

Output is **ALSA only**, which covers any Linux sink ALSA can reach: a card's
`hw:` device, `default`, a `dmix`/`plug` PCM, or a bridge like PulseAudio's or
PipeWire's ALSA layer. No other backend is implemented, and the settings page
offers ALSA as the only driver. The protocol itself does not assume ALSA — see
[../../docs/native-renderer-design.md](../../docs/native-renderer-design.md).

WAV (`.wav`, `.wave`, `audio/wav`, `audio/x-wav`, `audio/wave`, or
`audio/vnd.wave`) supports mono/stereo RIFF/WAVE with 16/24/32-bit integer PCM
and 32/64-bit IEEE float. Both ordinary and WAVE_FORMAT_EXTENSIBLE headers
are supported, including 24 valid bits in a 32-bit container. The decoder
preserves sample rate and significant bits; 24-bit/96 kHz and 24-bit/192 kHz
are covered by automated decoder and ALSA output tests. Mono plays through
both speakers. Unknown metadata chunks and their padding are skipped, as
long as the audio starts within the first 64 MB of the file.
Local files and HTTP streams use the same graph. Seeking and start offsets
require a seekable source; sequential HTTP streams play from the beginning.
RF64, big-endian RIFX, compressed WAV, 8-bit PCM and multichannel WAV are not
supported.

MPEG-DASH (`application/dash+xml` or `.mpd`) supports static, single-period
HTTP/HTTPS manifests carrying unencrypted mono/stereo FLAC, or AAC (`mp4a.40.2`,
`mp4a.40.5`, `mp4a.40.29`), in fragmented MP4.
Manifests use `SegmentTemplate` with a continuous `SegmentTimeline` starting at zero.
The renderer fetches segments with its existing HTTP transport, extracts the
frames from fragmented MP4, and decodes FLAC with libFLAC, preserving the original
sample rate and bit depth, and AAC with faad2. FFmpeg is not a build or runtime
dependency. Seeking and start offsets use the audio timestamps. PCM buffering is
bounded; the server only supplies the manifest and does not remux or transcode
audio. Live DASH, encrypted streams and other DASH audio codecs are not supported.

MP4 and M4A (`audio/mp4`, `audio/m4a`, `audio/x-m4a`, `.m4a` or `.mp4`) play
their first audio track when it is AAC, FLAC or ALAC, from a local file or HTTP.
Progressive files work wherever their index lies, though an index after the audio
needs a seekable source; fragmented files play fragment by fragment and seek
through a `sidx` or trailing `mfra` index, and without either play from the start
with no known length. Raw AAC in ADTS frames (`audio/aac`, `audio/x-aac`,
`audio/aacp` or `.aac`), as internet radio sends it, plays after any ID3v2 tag;
its length and seeks are estimated from the average frame size. AAC-LC, HE-AAC
and HE-AAC v2 decode with faad2 to 16-bit stereo at the stream's output rate:
mono is upmixed and multichannel audio downmixed. HE-AAC v2 broadcasts can
start before their next parametric-stereo header: the initial mono frames
play immediately through both channels, keeping the output format stable.
MP4 and DASH play exactly
what the track's edit list, or else iTunes' `iTunSMPB` tag, selects, so the
encoder's priming and padding are trimmed and AAC albums play gaplessly. ADTS
records neither, so raw AAC loses only its first unit of priming (1024 frames,
or 2048 with SBR) and
plays its padding. ALAC decodes in the renderer itself, keeping the original
sample rate and bit depth, in mono or stereo at 16, 24 or 32 bits; 20-bit and
multichannel ALAC are not supported. Neither are xHE-AAC, and ADTS that
describes its channels in a program config element.

Integer output negotiation only accepts containers that preserve the source
precision. True 32-bit PCM requires 32 significant output bits. Float samples
retain their original representation and require the matching ALSA float
format; they are not implicitly quantized to integer PCM. A shared ALSA route
may perform additional conversions beyond the renderer's view.

Software volume uses a cubic 0–100% gain curve, with a float gain coefficient
and double-precision multiplication. Integer samples are rounded and clamped
at the negotiated 16/24/32-bit precision without dithering; float samples are
written at their original float precision. Float32 has 24 significant binary
digits; float64 has 53. Processing does not change sample rate. At 100%, the
sample-processing stage is bypassed; fixed output also uses unity gain.

For the [JAS Hi-Res AUDIO requirements](https://www.jas-audio.or.jp/english/hi-res-logo-en),
FLAC and WAV playback at 24-bit/96 kHz are both required, alongside suitable
I/O, DSP and D/A conversion where applicable. The automated output tests use
ALSA `null` and `file`: they check the negotiated format and compare captured
output samples byte for byte, without a physical DAC.
Hardware qualification still needs a documented 24-bit/96 kHz output run on
the target device and confirmation of its DAC capability. Use the actual
output format reported during playback, not only the device's maximum
capability summary. Logo qualification also requires JAS licensing and the
applicant's listening evaluation process.

DSF (`.dsf`) and uncompressed DSDIFF (`.dff`) play as mono/stereo DSD, or
converted to PCM. **DSD output** in renderer settings chooses how: Automatic
(default), Native DSD, DoP, Convert to PCM, or Disabled. Automatic sends native
DSD when the DAC takes the track's rate as DSD and the volume is **Fixed**, and
converts to PCM otherwise; it never picks DoP. Native DSD and DoP need a direct
ALSA hardware output and **Fixed** volume, so set the listening level on your
amplifier: the renderer does not silently bypass an active volume control.
Converted DSD plays on any output that supports a rate it converts to, with any
volume control. DSDIFF metadata chunks before the audio are skipped, as long as
the audio starts within the first 64 MB of the file.

**Bit depth** and **Sample rate** show the selected output's maximum PCM
capabilities, for example **Up to 24 bit** and **Up to 192 kHz**. These are
read-only summaries; unavailable capabilities are shown as **Unavailable**.
A busy device may use its last successful probe; playback checks the actual
hardware again. Changing the output device sets Native DSD and DoP back to
Automatic, so a DoP selection for one DAC does not carry over to another.

DSD64/128/256 describe the source rate, while native DSD and DoP describe its
transport. Packing preserves the original bits and rate. For example, DSD64
uses a 176.4 kHz DoP carrier; DSD128 needs 352.8 kHz and DSD256 needs 705.6 kHz.
DoP needs at least 24 significant carrier bits. A compatible PCM carrier does
not establish that the DAC understands DoP: select DoP only for a DAC that
does. Automatic never assumes DoP support. Under Native DSD or DoP, a rate or
layout the DAC does not take is a playback error, with no fallback to PCM.
DST-compressed DFF, SACD ISOs and multichannel playback are not supported.

Source state reports the one-bit DSD rate separately from the output's packed
frame rate. Duration, seeking and HTTP ranges use the same playback graph as
PCM tracks. Pauses use DSD silence, preserving the DoP marker sequence.

Before relying on a particular DAC, check its reported capabilities, start
with a known DSF/DFF file at an appropriate amplifier level, confirm the DAC's
DSD/native or DoP indicator, then check seek, pause and PCM↔DSD transitions.
Automated packing and protocol tests do not verify recognition by a real DAC.

On Raspberry Pi, **HDMI N (direct)** selects
`hdmi:CARD=vc4hdmiN,DEV=0,AES3=0x01` (the displayed port number starts at 1).
This is ALSA's IEC958 framing route, without soft volume or resampling. The
renderer verifies that the route reaches hardware through framing and control
hooks before allowing DoP. Custom routes containing mixing, conversion or
unknown plugins remain shared. Saved raw `hw:` selections for an attached
vc4 HDMI card migrate to the explicit framing name; `default:` selections
keep their shared route.
Custom HDMI definitions that do not accept `AES3` keep their existing
arguments, including when the renderer loads a saved selection.

Framing carries at most 24 significant PCM bits, including when ALSA accepts
a 32-bit container. True 32-bit PCM is rejected to avoid silent truncation.
The framing device reopens on format changes so its channel status carries
the new rate and word length. HDMI DoP still requires **Fixed** volume and
explicit **DoP** selection; Automatic converts to PCM instead.

## Build

Dependencies (dev headers): protobuf (+ compiler), Boost (headers only — asio
and beast), spdlog, ALSA, FLAC++, libvorbisfile, libcurl, curlpp, OpenSSL libcrypto, libxml2, faad2, libsoxr, and Highway 1.2
or newer for the DSP's vector kernels (Debian 13 or Ubuntu 26.04; Ubuntu 24.04's is too old). GoogleTest is
optional and only builds the test binary. mDNS is self-contained (vendored public-domain
`third_party/mdns`), so no avahi or Bonjour daemon is needed.

CMake fetches the SHA-256-pinned PFFFT v1.3.0 archive for FIR convolution.
For an offline build, add `-DFETCHCONTENT_SOURCE_DIR_PFFFT=/path/to/pffft-1.3.0`
to the configure command and point it at the unpacked archive.

- Fedora: `sudo dnf install cmake gcc-c++ protobuf-devel protobuf-compiler protobuf-lite-devel boost-devel spdlog-devel alsa-lib-devel flac-devel libvorbis-devel openssl-devel libcurl-devel curlpp-devel libxml2-devel faad2-devel soxr-devel highway-devel gtest-devel`
- Debian/Ubuntu: `sudo apt install ca-certificates cmake g++ libprotobuf-dev protobuf-compiler libboost-dev libspdlog-dev libasound2-dev libflac++-dev libvorbis-dev libssl-dev libcurl4-openssl-dev libcurlpp-dev libxml2-dev libfaad-dev libsoxr-dev libhwy-dev libgtest-dev`

From the repo root:

```sh
make renderer-build
# or directly:
cmake -S packages/kalinka-renderer -B packages/kalinka-renderer/build
cmake --build packages/kalinka-renderer/build -j
```

A C++23 compiler is required.

Ogg Vorbis (`audio/ogg`, `application/ogg`, `audio/vorbis`, `.ogg` or `.oga`)
decodes to 16-bit PCM, with mono and stereo supported. Seekable files and HTTP
streams support seeking and start offsets; sequential streams play from the
beginning. Chained Vorbis streams must keep the same sample rate and channel
count. Other codecs in an Ogg container, such as Opus, are not supported.

Web radio with ICY headers is read as one continuous HTTP response, even when
the server advertises byte ranges and a placeholder file length. Legacy
SHOUTcast responses starting with `ICY 200 OK` are accepted too. Endless MP3
uses minimp3's sequential frame decoder without scanning for duration or
rewinding. Xing/LAME delay and padding are also honoured for finite MP3
responses without `Content-Length`, without adding a PCM tail buffer.
MP3, FLAC and ADTS AAC broadcasts of unknown length accumulate one
second of decoded audio before playback starts or resumes after starvation,
bounded by their usable decoder buffer capacity. Decoders that write whole
frames round their capacity down to a frame boundary so reserve waits can
complete. This absorbs delivery bursts without increasing the output device's
latency or changing interactive
live PCM playback. Sources marked `X-Kalinka-Live: 1`, including live captures,
do not request that broadcast reserve.

HTTP requests identify the player as `KalinkaRenderer`, including after a
redirect or range restart, for radio CDNs that refuse unidentified clients.
HTTP streams follow up to three HTTP or HTTPS redirects per request by default,
configurable with `network.max_redirects`. Range requests, retries, and seeks
start from the original URL so a controller such as BubbleUPnP can issue a fresh
temporary media URL each time. A server that answers the first range with
`416 Range Not Satisfiable`, as BubbleUPnP's AudioCast does for its live
capture, is asked again without a range and read from the start to its end. It
cannot be sought unless that answer offers ranges after all. Any other 416, to a
resume, a seek, or once a request has succeeded, fails the track.

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
| `output.bit_depth` | Read-only maximum significant PCM bit depth for the selected output, such as `Up to 24 bit`. |
| `output.sample_rate` | Read-only maximum PCM sample rate for the selected output, such as `Up to 192 kHz`. |
| `output.dsd_mode` | `auto` (default: native DSD where the DAC and Fixed volume allow, otherwise PCM), `native`, `dop`, `pcm` or `disabled`. Native DSD and DoP require fixed volume and direct hardware. Changing it stops playback, and every connected Core is told whether DSD is now taken. |
| `output.dsd_pcm_rate` | The PCM rate converted DSD plays at: `auto` (default), or `1x`, `2x`, `4x` or `8x` of 44.1 or 48 kHz. |
| `output.dsd_filter` | The low-pass filter DSD is converted with: `auto` (default: 30 kHz for DSD64, 50 kHz above), `30k`, `50k` or `wide`. |
| `output.dsd_gain_db` | The level converted DSD plays at: `safe` (default, +2.8 dB), `0` or `6`. Applies at once. |
| `output.volume_mode` | `auto`, `hardware` (card mixer), `software`, or `fixed` (ignore volume, play at full level — for an amp that sets the level itself). |
| `output.session_start_volume_ceiling_percent` | Maximum allowed level when a renderer-controlled session starts. It lowers a louder existing level but never raises a quieter one. Fixed output bypasses it. |
| `output.latency_ms`, `output.period_ms`, `output.format_change_delay_ms`, `output.reopen_on_format_change` | How the ALSA sink is opened and driven. |
| `buffers.*` | How much audio is held in memory and how far ahead of the card it runs. |
| `network.stall_timeout_s` | How long a stream may go without receiving a byte, in seconds, before the request is dropped and made again from where it stopped (default 15, 5 to 300). The track fails after four failed requests in a row — stalls, dropped connections or 5xx answers — with less than 16 KB received between them, or after the first from a server that cannot resume. |
| `network.max_redirects` | Maximum HTTP or HTTPS redirects per request (default 3, 0 to 10). Set to 0 to disable redirects. |

Changing the device or how the sink is driven (`output.latency_ms` and the
rest of that row) rebuilds the graph and stops playback. The buffer sizes and
the network settings apply to each track handed to the renderer after the change,
and leave the one playing alone. The schema says so per field, so the settings
page can warn first.

The Core also adds **ReplayGain** (`core.output.replaygain`) to each renderer's
settings: **Disabled** (default), **Album level**, or **Track level**. It works
with every volume control except Fixed output, and with a connected device such
as MusicCast that sets the volume. With Fixed output it is read-only and reads
Disabled. The saved choice applies again once volume control changes.

The Core reads the selected gain tag and sends volume changes to the current
volume owner. Renderers report the dB scale of their software volume or of the
card's mixer. A PipeWire or PulseAudio mixer has no dB scale of its own in ALSA,
so the renderer reports the cubic scale those servers use. Any other card mixer
without a dB scale gets no ReplayGain, as the Core cannot tell how far a step
moves it. The log says so once per renderer. MusicCast reports its volume
step. Gain is applied on track start and removed on stop, error, or a change to
an untagged track, preserving manual volume adjustments.
Album mode uses album tags without substituting track gain when they are absent.
Apply and restore operations are logged. The choice is saved per renderer in
the Core's renderer preferences, and replaces MusicCast's old automatic volume
correction setting. Existing library album tags are recovered from saved tag
evidence where available; files without saved evidence need to be rescanned.

A value in `config_overrides` that the setting's field would refuse as a write,
such as a number out of its range, is ignored with a warning in the log, and
the default applies.

## State on disk

Under `$KALINKA_PREFIX/var/lib/kalinka-renderer` (`KALINKA_PREFIX` defaults to
`/`, the same convention as the server):

- `renderer_id` — stable identity, minted on first run. Cores tell renderers
  apart by it, so losing it makes this renderer look new.
- `config_overrides` — `key=value` lines, only for settings that differ from
  the defaults.

If the directory is not writable the renderer still runs, with an ephemeral id
and settings that last only for the run.

## Room correction

### Filter impulse WAV

Use filter coefficients from any application that exports a compatible WAV.

In REW, choose **File → Export → Export filters impulse response as WAV**.
Choose the playback sample rate and **32-bit Float**. Turn off peak normalization
to preserve the gain designed in REW. A mono export applies the same filter to
both speakers. A stereo export holds the left and right filters in that order.

In **Room EQ → FIR Convolution**, choose **Import impulse WAV**. Select one
or more filter WAVs and name the preset. A single WAV covers multiple playback rates. To use a
particular design at a given rate, include that WAV in the same import. An exact
match always wins. For a missing rate the renderer converts the nearest original
by octave distance, preferring the higher rate in a tie. It caches the last
three prepared rates in memory for the selected profile. For stereo DSD
converted to PCM, it matches the conversion output rate and applies FIR before
PEQ and headroom adjustment. Native DSD and DoP pass through unchanged. The Core stores presets and the renderer caches
the selected preset for offline playback. In the released app’s schema-based
settings, the same selector appears under **Room correction → FIR Convolution**.
Existing WAVs in `/var/lib/kalinka-renderer/dsp/fir/` remain selectable by filename.

The renderer accepts up to 262,144 taps per channel. A preset can hold up to
eight rates and 32 MiB. Converted filters can contain up to 4,194,944 taps
per channel. If conversion exceeds that limit, the signal path asks for a WAV
at the playback rate. Stereo PCM crossfades when the selected filter changes.
The live path buffers 640 frames to align filtered and dry audio. The signal
path shows **FIR room correction** and identifies the source rate of a converted filter.
Room EQ shows the configured FIR magnitude as a dashed line, with separate
left and right traces for different stereo responses. The graph labels its
preview rate. Before first PCM playback it uses 48 kHz.

The [176.4 kHz test impulse](../../examples/fir/1kHz-cut-176.4kHz.wav) and
[192 kHz test impulse](../../examples/fir/1kHz-cut-192kHz.wav) reduce a 1 kHz
tone by 9 dB. Import both WAVs in one preset and play a track at either rate.
Each file has 4,096 taps per channel. Trimming trailing zeros leaves 463 taps
at 176.4 kHz and 502 at 192 kHz.

For sustained CPU testing at 176.4 kHz, import the
[65,536-tap performance impulse](../../examples/fir/performance-65536-176.4kHz.wav)
as its own preset. Both channels have distinct, fully populated filter tails.
Compare that preset with FIR set to **Off** on the same track.

Now Playing shows a red **!** beside the format when the renderer clamps output samples. Open **Signal path** for the count. Each track and each room correction change starts a fresh count after the filters settle.

### Parametric EQ text export

In Room EQ Wizard, choose the **Generic** equaliser and **Export filter settings as text**.
Import one curve for all channels, or export each speaker's correction separately.
Mono PCM is duplicated before EQ so each speaker still receives its own correction.
The Core accepts up to 20 active bands per channel from decimal-point text exports.
It supports PK and Modal peaks, LS and HS shelves, and LSQ and HSQ shelves with
adjustable Q. LP and HP are 12 dB/octave Butterworth low and high passes, LPQ
and HPQ take their own Q, and LP1 and HP1 are first-order, 6 dB/octave. Two
identical LP or HP rows make a 24 dB/octave Linkwitz-Riley edge, and an LP1 or
HP1 row with an LPQ or HPQ row of Q 1 makes a third-order Butterworth one. An
unsupported active filter, such as a notch or an all-pass, produces an error
with its line number.
Disabled and zero-gain bands take no processing time.
See the [REW EQ window guide](https://www.roomeqwizard.com/help/help_en-GB/html/eqwindow.html)
for the export controls.

With `CORE` set to the Core's base URL, import a shared curve with the following command.

```sh
jq -n --arg name "Listening room" --rawfile shared room.txt '{ $name, $shared }' |
  curl --fail-with-body --json @- "$CORE/dsp/profiles/rew"
```

For separate left and right curves, send `left` and `right` in place of `shared`.

```sh
jq -n --arg name "Listening room" --rawfile left left.txt --rawfile right right.txt '{ $name, $left, $right }' |
  curl --fail-with-body --json @- "$CORE/dsp/profiles/rew"
```

Select the imported name under **Room correction → Room profile** in the renderer's
settings and enable **Parametric EQ**. The import returns the profile's SHA-256 as `id`, which also works as
`dsp.profile` in `PUT /renderer/{renderer_id}/config`. The renderer verifies and
caches the selected profile and restores it on restart.

Room EQ’s **Bypass DSP** switch bypasses FIR convolution, parametric EQ and
headroom adjustment during playback. Switching it off restores the configured
filters and gain. Output volume and the format required by the playback device still apply.

**Headroom** has its own switch and keeps its selected mode and gain while off.
Automatic mode reserves headroom for FIR peaks using the sum of the absolute
impulse coefficients at the playback rate, taking the larger channel bound.
This can lower the level even for a filter that only cuts frequencies. It also
reserves the sum of the PEQ bands' maximum boosts on the channel that needs the
most headroom. A pass band with a Q above 0.707 peaks at
its corner, and that peak counts as a boost.

In Manual mode, **Gain** accepts −30 to +30 dB in 0.1 dB steps and ramps over
25 ms. Negative gain leaves more headroom. Positive gain makes the signal
louder and can clip peaks at the output. Room correction runs on PCM, including
DSD converted to PCM. Native DSD and DoP pass through. If a band's frequency is at or above Nyquist, the
renderer skips the curve for that sample rate and logs the reason.

## Upgrading

The renderer runs unprivileged and cannot install anything itself. It asks, by
writing the version it was told to install into `/run/kalinka-renderer/upgrade-request`; the root-owned `kalinka-renderer-upgrade.path` unit picks that up and runs `/opt/kalinka/upgrade-renderer.sh`, which fetches the current `install-renderer.sh` and installs that release. The renderer reports whether that machinery is present in its Hello, so a Core never offers an upgrade to a build installed some other way (flatpak, from source), which could not perform it.

There are two ways in, and the second exists because the first can be cut off:

- **From a Core.** `POST /renderer/<id>/upgrade` sends an Upgrade message down the renderer's own connection. It is carried by every protocol version, so it still reaches a renderer whose protocol the Core no longer speaks — the case where nothing else can. The renderer refuses while a playback session is running, so the Core stops its own session there first.
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

Software and fixed volume raise the device's ALSA mixer to 100 % first, so that
fixed output is bit-perfect. On a desktop's own device that turns up everything
else it plays, and leaves it there. With `KALINKA_LEAVE_MIXER=1` the renderer
leaves the mixer alone; fixed output is then bit-perfect, and plays DSD, only if
the mixer already stands at 100 %. The test binary sets it for every test, so no
run of the suite touches a real device's mixer:

```sh
KALINKA_TEST_ALSA_DEVICE=pipewire ctest --output-on-failure
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
server. Boxes that only render run the script themselves. With
`KALINKA_SUPERVISOR=1` it also installs the supervisor, on Debian-based
systems, which lets the app set such a Pi up for its DAC HAT
([the sound card](../../docs/supervisor-control.md#the-sound-card)).
