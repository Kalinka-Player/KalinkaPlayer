# Renderer DSP — design

Status: Phase 1 is done: the DSP stage, the output stage's gain, dither,
packing and fades, DSD to PCM with its settings, and the signal path and
clipping the renderer reports (sections 5, 7, 8 and 14). Phase 2 has begun.
REW profiles, parametric EQ, preamp and bypass are implemented. The FIR
convolution backend is implemented and benchmarked. Loading a room impulse
from a Core profile and upsampling are still ahead. Measurements cover
x86-64, Raspberry Pi 4 and Raspberry Pi 5.
See the [results](../benchmarks/dsp/results.md) and the
[benchmark runbook](../benchmarks/dsp/RUNBOOK.md).

The note covers the renderer in `packages/kalinka-renderer`. The rules in
[`CLAUDE.md`](../CLAUDE.md) and
[native-renderer-design.md](native-renderer-design.md) bind it.

It builds for:

- Debian 13 debs, arm64 and amd64, which the images and the Raspberry Pi run;
- Fedora 44 RPMs;
- Ubuntu 26.04.1 LTS debs.

Debian 13 is the floor: GCC 14.2, CMake 3.31 and Highway 1.2.0 are the
oldest any language or library feature the DSP uses may need. Ubuntu 24.04
is not a constraint: its Highway 1.0.7 is older than the DSD converter needs,
so with Phase 1 the Ubuntu debs moved to 26.04.1 LTS, and the renderer's CI
tests run on Debian 13.

The release workflow also has a flatpak job, on freedesktop 24.08, but no
flatpak has shipped. Its manifest matches `.gitignore`'s `*.yml` and was
never committed, so the job fails on every release, and `continue-on-error`
hides that.

## 1. How the graph behaves today

### Source changes

The graph is pulled. The sink, `AlsaAudioEmitter`, reads from the node in
front of it, and every node reports a `StreamState`. A node never pushes audio.

`AudioStreamSwitcher` holds one current source and a queue of the next ones.
When the current source reports `FINISHED` and another is queued, the switcher
drops it and announces `SOURCE_CHANGED`. It stamps the announcement with the
incoming stream's id and stops serving data. Reads return 0 until the reader
calls `acceptSourceChange()`. Only then does the switcher take the next source
from the queue, subscribe to its states, and forward the first of them. That
state may be `STREAMING` with a format, or still `PREPARING`. The reader decides
when a boundary is crossed, and the switcher only tells it that one is there.

The emitter meets a source change in two places:

- **While idle** (`waitForInputToBeReady`): it adopts the new stream id, reports
  `SOURCE_CHANGED` and accepts. Then it waits for `STREAMING` and sets the
  device up for the format it carries.
- **While streaming** (`handleInputNodeStateChange`, after every write to
  ALSA): it accepts at once, so it can read the incoming source's format.
  - If the source is `STREAMING`, has the same `StreamAudioFormat` and live
    policy as the device runs, and playback isn't paused, the emitter keeps
    writing. It defers the `SOURCE_CHANGED` + `STREAMING` report with
    `PlayedFramesCounter` until the frames ALSA still holds, `snd_pcm_delay`,
    have played. The listener hears the switch when the report says.
  - Otherwise it drains ALSA, reports `SOURCE_CHANGED` and returns to the idle
    path. That path reconfigures the device and waits
    `output.format_change_delay_ms`.

So gapless is exactly this: the reader is shown an equal format while it is
still streaming. Whichever node stands in front of the emitter decides whether
the hardware reconfigures.

### Positions

Positions are source frames, and a source need not start at zero: a start
offset, or a seek, sets where it begins. After every accepted source change,
seek or fresh start, the emitter asks its input for `streamReadPosition()`:
how far the frames handed out so far reach. It counts what it writes from
there, `currentSourceTotalFramesWritten`. What the listener has heard is that
count minus what ALSA still holds. A paused state is stamped with the written
count once the queued frames have played. A buffering state reports what was
reached. Gapless track starts are reported from the deferred callback with the
frames played past the boundary. `streamReadPosition()` is the only place a
node reports position, and it is called right after an accept or a seek.

## 2. Today's cost

Measured with `kalinka-dsp-bench playback`, which plays 60 s of 16/44.1 and
24/192 FLAC (pink noise under three tones, FLAC ratio 73 % and 81 %) through
`AudioPlayer` into the ALSA `null` device at fixed volume. Those runs used an
Intel Core Ultra 9 185H, GCC 16 at `-O3`, the `powersave` governor.

| Source | CPU for 60 s | Of one core in real time |
|---|---|---|
| 16-bit 44.1 kHz | 0.05–0.06 s | 0.08–0.09 % |
| 24-bit 192 kHz | 0.22–0.25 s | 0.36–0.42 % |

`perf record` over five plays of each puts 77–79 % of that CPU time on the
decoder's thread and 18–22 % on the ALSA thread. libFLAC is most of the
decoder's thread: rice decoding 33–35 %, LPC restore 18–19 %, frame parsing
10–13 %, CRC 6–8 %. On the ALSA thread nearly everything is
`FlacStreamDecoder::read`, which is `Buffer<uint8_t>::read` inlined: 12 % of
all samples at 16/44.1 and 18 % at 24/192. Allocator calls from the deque's
block churn add 2–3 % more.

`Buffer` is a mutex around a `std::deque<uint8_t>`. Its copies go element by
element through deque iterators, and every block it frees or allocates goes
through malloc. Measured on its own it costs 2.2 ns per stereo S32 frame for a
write and a read, about 3.6 GB/s. The same machine copies flat memory tens of
times faster. That made `Buffer` the largest cost in the output path outside
the decoder. Phase 1 replaced it with a ring (section 13, decision 4).

On a Raspberry Pi 5 (Cortex-A76, Debian 13's GCC 14.2 and libraries) the
same files cost 0.14–0.15 % and 0.50 % of a core. There the decoder's thread
takes 91 % of the time and the ALSA thread 6–8 %. Debian 13's GCC already
turns the deque's copies into `memmove`, so `Buffer::read` is 0.02–0.03 % of
the samples on the Pi 5. The ring is still worth having for its lock, its
allocations and x86, but on the Pi it is not a speed fix.

A Pi 4 has not been measured. Expect its numbers to be higher still.

## 3. Placement: one stage after the switcher

Confirmed: the stage sits once, after `AudioStreamSwitcher` and before
`AlsaAudioEmitter`. Three measurements back it.

**The graph is unchanged through it.** `DspStage` is in the graph now,
between switcher and emitter, with no processor, which is today's behaviour.
For tests it can hold PCM back through a `DelayLine` (`dsp.delay_ms`, a graph
key, not a setting). Setting `KALINKA_TEST_DSP_DELAY_MS` sends the existing
gapless, seek, pause, position, switcher and integration tests through it.

| Run | Null device, whole suite | PipeWire, the 135 playback tests in real time |
|---|---|---|
| Without the variable | 776 / 776 | 134 / 135 |
| Stage with no processor | 776 / 776 | 134 / 135 |
| Stage holding audio 300 ms | 776 / 776 | 134 / 135 |

The PipeWire failure is `PlaybackPositionTest.PlaysOggVorbis`. It waits 5 s
for a 6 s track to finish, and fails the same way before this work. Three
older tests are flaky with or without the stage. `AlsaAudioEmitterTest.pause`
failed 1 run in 8: PipeWire took about 290 ms to start the stream, so the
test's sleep began before any audio played. `getState` fails about once in six
runs after PipeWire has been idle. `PlaybackPositionTest.SeekAfterSeekLandsOnTheLastOne`
failed 2 runs in 60 on HEAD and 1 in 60 here: the second seek's state lands
after the test's mark. A new test
writes through the ALSA `file` plugin and compares bytes with the decoder's
own output, for one track and across a gapless join. The bytes are identical
with no stage delay and through 300 ms of delay.

Four tests are not routed through the delay, because their subject is how the
emitter behaves with what a source hands it straight. One checks the ALSA
buffer the first wait asks for. Two starve a live input. One expects a source
that stalls at 1000 ms to buffer at 1000 ms; through a 300 ms stage it buffers
at 700 ms, which is what has been heard. That last behaviour is covered by its
own stage test (`ARunDryHoldsTheLatencyBackUntilMoreArrives`).

**Per stream breaks continuity.** One continuous 6 s multitone, split in two,
went through each filter per stream (each part from silence, the first part's
tail flushed and cut) and continuously, at 44.1 kHz:

| Filter | Largest error | Error above one 24-bit step lasts | Click |
|---|---|---|---|
| PEQ: 1 kHz +6 dB Q 2, 100 Hz shelf +6 dB | -25.7 dBFS | 35 ms, all after the join | no |
| FIR: 65535 taps, linear phase, +6 dB at 50 Hz | -22.2 dBFS | 291 ms, from 148 ms before | no |
| Resampler, libsoxr VHQ to 96 kHz, join off the 147-frame grid | -21.6 dBFS | the rest of the track | no |
| Resampler, join on the grid | -21.3 dBFS | 5 ms, from 2.6 ms before | yes |

An error 22–26 dB under full scale is plainly audible. Continuous processing
at the stage has no join error by construction. The resampler row shows a
second failure of per-stream processing that no tail handling repairs. 44.1 to
96 kHz is 147 frames in to 320 out, so a track whose length is not a multiple
of 147 leaves the next one a fraction of an output sample off the continuous
grid. That timing slip lasts for the whole track. "Click" means a step at the
join larger than any the continuous output makes. The FIR and PEQ errors are
smooth enough to pass that test and still plainly audible.

**The pulling thread has room.** The stage processes inline on the emitter's
thread. Today that thread spends 18–22 % of 0.1–0.4 % of a core on x86, and
6–8 % of 0.14–0.5 % on the Pi 5. The kernels measured so far cost fractions
of a percent per stage on x86, and about 1 % at most on the Pi 5
(section 11). The 65,536-tap stereo convolver uses 2.88 % of a Pi 4 core in the
processor benchmark, including PCM conversion. The longest measured
256-frame callback took 1.83 ms at 44.1 kHz. An end-to-end ALSA playback
measurement follows profile integration.

### What the stage does

Without a processor, `DspStage` forwards every read, wait, seek, position and
state untouched. That path is unconditional: the test that compares bytes
written to ALSA is its guard.

With a processor, the stage hides the processor's latency L. It reads L frames
ahead of the sink and drops the first L outputs of each run. At the end of a
run it feeds L frames of silence to bring the tail out. So output frame n of a
source is that source's input frame n, and nothing downstream corrects for the
latency. The upstream position stays ahead of what has been handed on, and
`streamReadPosition()` reports what has been handed on.

| Event | What the stage does |
|---|---|
| Upstream `SOURCE_CHANGED`, nothing held | Shown downstream as is; the sink's accept is passed up |
| Upstream `SOURCE_CHANGED` while frames are held | Accepted upstream at once. Same format: the run continues into the new source, which becomes a segment behind the old. Other format: the old run's input closes and its tail is flushed |
| The sink reads up to the end of a segment | The read stops there, and `SOURCE_CHANGED` with the next segment's id is shown; accepting pops the old segment |
| The sink accepts a change across formats | The old run ends; the next read starts a new run in the new format |
| Upstream `FINISHED` or `ERROR` | The input closes and the tail is flushed; the state is shown once the last frame has been handed on |
| Upstream runs dry | The stage holds what it has and shows upstream's `PREPARING`; nothing is flushed, so a short stall leaves no seam |
| `seekStream`, `seekTo` | Everything held is dropped, upstream seeks, a new run starts from the target. The emitter names the stream it reports, so the seek lands in the track being heard even after upstream has left it (section 13). An unnamed `seekTo` is still refused then |
| `discard(id)`, from `AudioPlayer::remove` | What plays of that stream ends at once and the next held segment is announced; a later segment of it is skipped |
| `discardAll()`, from `clearAll` and `stop` | Everything held is dropped |
| DSD and DoP | The processor declines them; they pass untouched in every phase |

**Threads.** Everything the stage holds belongs to the thread that pulls
audio, and the stage has no lock of its own. Upstream state callbacks, from
decoder and control threads, and `discard()` from the control thread hand
their part over through `AudioGraphNode`'s existing state lock. They do it
with `updateState()`, which decides and sets the shown state under that one
lock. A discard of what plays shows the next source at once, even while the
sink is paused and reads nothing. The pulling thread then applies it on its
next call.

The only gapless decision is still the emitter's format comparison. It now
compares what the stage presents: the processor's output format and rate
(Phase 1, Phase 2 upsampling) rather than the source's. A fixed output rate
keeps that format constant across a mixed-rate queue. The stage's output
`StreamInfo` carries the translated rate, sample format and `streamSize`.

**Pause.** The emitter writes silence while paused and reads nothing, and the
stage sits idle. A pause, a resume, a seek, a stop and a skip fade briefly
rather than cut mid-waveform (section 5).

**Live streams.** `LiveAudioPolicy` targets 60 ms. Stages with no latency
(PEQ, crossfeed, channel tools, gain, dither) apply to live PCM. A stage with
latency, such as a uniformly partitioned FIR or an upsampler, would add its
latency to the live path. It applies only when that fits the policy's budget,
and otherwise the stage reports it as skipped. `liveSourceFrame()` already
subtracts what the stage holds, so the Core's backlog accounting sees the
latency, and the reported `dsp_latency_ms` (section 9) names it.

## 4. Internal format

- **Between stages:** planar float64, one 64-byte-aligned array per channel,
  sized when a run starts and never on the audio thread. The convolver may
  hold float32 internally if Phase 2 measures it enough (section 11).
- **Block:** the stage processes what the sink asks for, in chunks of at most
  1024 frames, so each chunk's working set fits in L1/L2 on the A72. The
  convolver keeps its own partition buffer and is fed and drained through it.
- **Copies from decoder to ALSA, processing on:**
  1. The decoder writes into its ring, as today.
  2. The stage reads that into its input scratch.
  3. One conversion pass to planar float64.
  4. The stages run in place.
  5. The quantiser packs straight into the ALSA mmap area. `read(dest)` already
     receives that pointer when formats match.

  Today there are two copies (decoder to ring, ring to mmap). Processing
  adds one copy and two conversion passes, and nothing else.

## 5. The end of the stage: gain, dither, packing

Each runs once, last, in this order:

1. **Gain:** one float64 coefficient, preamp × DSD gain × software volume. It
   ramps over 25 ms as `GainFade` does today, and settles at once after
   silence. It sits after every internal buffer, so a volume change is heard
   after the ALSA queue and nothing else. The coefficient has room for a
   levelling gain later; ReplayGain keeps reaching it as volume changes from
   the Core.
2. **Dither:** TPDF, 2 LSB peak to peak at the output's significant bits,
   independent per channel. It comes from xoshiro256+, which is fast, seedable
   and has a long period: four generators taken in turn, so no draw waits for
   the one before, each draw giving a sample its two uniforms. Noise shaping
   comes in Phase 2, for 16-bit only.
3. **Clamp and pack:** to S16, S24 in 3 bytes, S24 in 4, or S32. Clipped
   samples are counted.

**How the stage knows the significant bits.** The stage chooses the integer
format it presents, so it can dither at that format's bits. It picks from
`probeOutput()`, the same cached probe DSD output selection already uses: the
source's own width when the device takes it at the output rate, or else the
next container `pcmOutputFormats()` would pick. It dithers at the significant
bits the probe reports for that format, which is 24 for an IEC958 S32
container. After negotiation the emitter refuses a device that keeps fewer
bits than the stream has, and keeps that refusal. If the probe has nothing,
the stage presents the source's format. `dsp.output_bits` overrides the choice
for a DAC that reports more bits than it resolves.

**The off path stays byte-identical.** With no stage active and gain at unity
the stage takes the no-processor path, so the sink reads the decoder's bytes.
Turning processing on or off during playback crossfades in float. The last
block of a fade to "off" has weight 1 on the input and gets no dither, so it
equals the input exactly, and the stage switches to the untouched path at that
block boundary.

**Volume moves here.** `AudioPlayer` keeps its API and backend logic. The
software backend sets the gain through a narrow `GainControl` interface,
`setGain(double linear)`, which the stage implements. `softwareGain`,
`softwareFade` and the gain calls leave `AlsaAudioEmitter`. Below 100 % the
stream takes the float path and is dithered, which changes today's bytes at
those levels; the volume tests are updated for it in Phase 1. These stay as
they are:

- the cubic curve and the dB scale reported to the Core;
- `output.session_start_volume_ceiling_percent`;
- `auto` preferring a hardware mixer;
- the refusal of software gain for native DSD and DoP (`setDsdAllowed`).

**Pause, seek, stop and skip fade** over 10 ms: no click, and too short to
hear as a fade (section 13, decision 8). The gain step does it for the
emitter, through `OutputFade`, multiplying the volume, and between fades what
is written is untouched.
- **A pause** fades out over the next 10 ms the emitter writes, after what the
  device already holds. It is reported once that has been heard, and silence
  follows. A resume fades in from there, so nothing is lost or repeated. A
  fade that reaches a gapless join carries on into the next track. A track
  ending inside it, or with it, any other way plays out, and the next track
  starts paused. A skip while paused plays the next track, whether the fade
  had finished or not. An input running dry inside the fade fades out the
  device's last 10 ms instead, and the pause holds until it is back, unless
  a resume comes first. A seek plays from where it lands, as it does under
  any pause. A stall outside a pause is not faded, since an input that is
  merely late would then have its bytes changed.
- **A seek** takes back what the device holds unplayed with `snd_pcm_rewind`,
  unless that is only a pause's silence.
  It leaves 5 ms that may already be on its way to the DAC, fades the first
  10 ms of the rest out in place (`PackedFade`) and silences the remainder.
  Once the fade has been heard, the device is dropped as before. The audio
  after the seek fades in.
- **A stop** fades out what the device holds the same way before it closes.
- **A skip,** or any other removal of the stream being played, is named to
  the emitter first: `AudioPlayer` calls `cut()`. What the device holds then
  fades out as at a seek and is dropped, rather than played out into the
  next track, which starts a run of its own. Only the stream being written
  counts: removing a prepared one, or one already played out, changes
  nothing that plays.
- **A device that cannot take frames back,** a sound server's for one, drops
  at a seek, a stop or a skip without a fade.
- **Native DSD and DoP** are not faded.

## 6. Profiles reach the renderer by URL and SHA-256

Three ways were compared.

| | Core serves files, renderer fetches by URL, caches by SHA-256 | A chunked protocol message | Text in settings |
|---|---|---|---|
| Carries a 262144-tap float64 stereo FIR (4 MB) | yes | yes, in many messages | no: `config_overrides` is `key=value` lines |
| New code in the renderer | a cache and a fetch on its existing curl stack | framing, reassembly, flow control, resume | none |
| Effect on the session | none: separate connection, worker thread | competes with state and commands on the one WebSocket | none |
| Works when the renderer reaches the Core's HTTP | yes, as audio already must | yes | yes |
| Survives a restart or another Core | yes, by content hash | only if the renderer also caches | yes |

**Recommendation: URL and SHA-256.** A profile is one small document, and
each FIR file it names is referenced by URL, length and SHA-256. The setting
`dsp.profile` holds the profile's id and hash. The renderer fetches on a
worker thread with its HTTP code. It caps each file at 16 MB and verifies the
hash. It writes atomically (temp file and rename) to
`/var/lib/kalinka-renderer/dsp/<sha256>`, evicts least recently used past
256 MB, and hands the parsed profile to the audio thread by atomic swap. A
profile whose files are cached plays with the Core offline.

**Importers belong in the Core.** HouseCurve, REW, Equalizer APO and
CamillaDSP files are parsed there into one canonical profile. The renderer
accepts nothing else. That keeps its attack surface to one strict parser,
gives every renderer (the browser one too) the same import, and lets import
fixes ship without a renderer release. The cost is that a renderer cannot
import a file on its own, which the Core-driven settings model never offered
anyway.

**The canonical profile is a protobuf message** in `renderer.proto`
(`DspProfile`): versioned, schema-checked on both sides, already linked into
the renderer, and well fuzzed. FIR files are raw little-endian float64 with
rate, channel and length declared in the profile, so the renderer parses no
WAV for them. The Core converts WAV and resamples or not; per-rate files are
listed per rate. The renderer still validates everything:

- frequency below Nyquist and Q positive;
- gain within ±30 dB;
- finite coefficients, and stable poles for raw biquads;
- file sizes and counts capped.

It gets a libFuzzer target like the existing ones.

**Implemented for PEQ.** The Core imports REW Generic text into a version 1
`DspProfile` with a shared curve or independent left and right curves. The
profile digest identifies the immutable document. Import and selection are
described in the [renderer guide](../packages/kalinka-renderer/README.md#rew-room-correction).
The renderer checks each band, builds the coefficients for the PCM rate and
verifies stability. It keeps filter history across compatible gapless joins.
A profile change blends the old and new responses over 10 ms, or 25 ms when
the preamp also changes. Each branch has its own headroom during the fade.
The v1 PEQ document is limited to 16 KiB. CamillaDSP import and FIR asset
delivery are future work.

## 7. Settings

Protocol v3 adds DECIMAL for the manual preamp. A v2 Core receives the field
as STRING and the renderer validates the same bounds (section 13).

Phase 1:

| Path | Type | Values | Default | Apply cost | Importance |
|---|---|---|---|---|---|
| `output.dsd_mode` | ENUM | gains `pcm`; `auto` becomes native when verified, else PCM | `auto` | interrupts playback (as now) | simple |
| `output.dsd_pcm_rate` | ENUM | `auto`, `1x`, `2x`, `4x`, `8x` of 44.1/48 kHz, by source family | `auto` | interrupts playback: the converter is built with it | simple |
| `output.dsd_filter` | ENUM | `auto` (30 kHz for DSD64, 50 kHz above), `30k`, `50k`, `wide` | `auto` | interrupts playback: the converter is built with it | simple |
| `output.dsd_gain_db` | ENUM | `0`, `safe`, `6` | `safe` | instant, ramped | expert |

The DSD settings, `output.dsd_mode` among them, are a DSD section of
their own. Their paths keep the `output.` prefix the mode has always had,
so stored settings carry over, and the graph reads the rate and the filter
as `dsd.*`. They are simple, so that they show in the section rather than
apart in the expert list. The rate was to apply to the next DSD source and
the filter to switch at once with a crossfade, but the converter presents
its rate for queued tracks ahead of time, so a change to either rebuilds
the graph as `output.dsd_mode` does. A rate the output does not take falls
back to `auto`.

The gain is the DSD section's DSD to PCM level. The output stage applies it
with the volume, on DSD the stage converted and nothing else, so a change
ramps over 25 ms in the track that plays. `auto` sends a track natively
where the output takes its rate as DSD and the volume is Fixed output, and
converts it otherwise; it never sends DoP. DSD chosen for conversion leaves
the container as `DSD_RAW`, a format no device is opened in, so a DAC's own
`DSD_U8` passes the converter untouched.

Phase 2. The first five settings are implemented.

| Path | Type | Values | Default | Apply cost | Importance |
|---|---|---|---|---|---|
| `dsp.enabled` | BOOL | | false | instant, crossfaded | simple |
| `dsp.profile` | ENUM | profiles the Core has published to this renderer, and `none` | `none` | instant, crossfaded | simple |
| `dsp.bypass` | BOOL | A/B | false | instant, crossfaded | simple |
| `dsp.preamp` | ENUM | `auto`, `manual` | `auto` | instant, ramped | expert |
| `dsp.preamp_db` | DECIMAL, dB | -30 to 0 | 0 | instant, ramped | expert |
| `dsp.crossfeed` | ENUM | `off`, bs2b presets | `off` | instant, crossfaded | simple |
| `dsp.upsampling` | ENUM | `off`, `power_of_two`, `fixed` | `off` | interrupts playback: the output format changes | expert |
| `dsp.upsampling_rate` | ENUM | rates `probeOutput()` reports | highest | interrupts playback | expert |
| `dsp.upsampling_filter` | ENUM | linear, intermediate, minimum phase; standard, steep | linear, standard | instant, crossfaded | expert |
| `dsp.dither` | ENUM | `auto`, `tpdf`, `shaped`, `rounding` | `auto` | instant | expert |
| `dsp.output_bits` | ENUM | `auto`, `16`, `24`, `32` | `auto` | interrupts playback | expert |
| `dsp.limiter` | BOOL | | false | instant | expert |
| `dsp.live` | ENUM | `latency_free`, `off` | `latency_free` | instant | expert |

Parameter changes are instant. Only a change of output format interrupts
playback, as the prompt asks.

## 8. Reporting

What the Core gains, as additions to `renderer.proto` in Phase 1 and 2:

- `Capabilities.dsp`: whether DSD converts to PCM, the stages built in, the
  most FIR taps, the most output channels.
- `StateSnapshot` and `PlaybackStateChanged` gain `signal_path`. It holds the
  source format, the active stages in order (name plus a short summary, such
  as "DSD64 to PCM 176.4 kHz, 30 kHz filter, +2.8 dB"), and the device format.
  It is empty when the stage is untouched. With `device_info` and
  `VolumeState` it settles whether playback is bit-perfect.
- `dsp_latency_ms`: what the stage holds back.
- `DspMeters`, sent at most twice a second while processing: clipped samples
  since the source began, true peak from 4× oversampling, and DSP load as
  processing time over real time, a moving average.

**Landed in Phase 1,** for DSD to PCM, and differing from the plan in four
ways:
- `SignalPath` holds the steps alone, each a stable `kind`, a `name` and a
  `summary`, with the latency they add. The formats at either end are the
  state's `format` and `device_info`, and the volume stays in
  `VolumeState`. The steps so far are `dsd_to_pcm` ("DSD64 to 176.4 kHz,
  30 kHz filter"), `dsd_gain` and `dither`, the last only where float meets
  an integer device. The path is put together as a state is sent, from the
  stream's decoded format and each stage's own description, so a level
  change is restated at once.
- `dsp_latency_ms` is the path's `latency_ms`.
- `DspMeters` became `PlaybackEvent`: something that happened, sent as it
  happens rather than sampled. It states nothing that lasts, so no snapshot
  repeats it, and it does not stop playback as `PlaybackError` does. Its
  first kind is `Clipping`, the samples the output stage clamped since the
  source began. The audio threads post to a mailbox that keeps the latest of
  each kind and never allocates. One listener sends what it takes and then
  holds off half a second, so a run of clipping reaches the Core at most
  twice a second, and a track that never clips sends nothing. A Core ignores
  a kind it does not know, so others, such as an output underrun or how far
  ahead a source is buffered, can follow without changing it.
- The true peak and the DSP load wait for Phase 2. In Phase 1 only the +6 dB
  DSD level can clip, so there is no setting they would inform yet. The load
  is for the benchmarks and the log.
- `Capabilities.dsp` says whether DSD converts to PCM, the step kinds and
  the most output channels. The most FIR taps waits for the convolver.

The Core passes the path on with the output a playback state describes,
where a path with steps rules out a lossless one, and playback events as
`playback_event` on the queue channels, a `kind` with its `details`, for the
playing source on the current renderer alone. It relays every kind the same
way. The app shows the steps between the decoded stream and the output, and
beside them the latency and, once the track has clipped, its clipped
samples.

## 9. SIMD: Highway

`kalinka-dsp-bench simd` compares the mechanisms on a float64 FIR dot product
(64 and 256 taps) and a 10-section stereo biquad with left and right in one
vector. Every variant was null-tested against the scalar reference.

| Mechanism | FIR 64 | FIR 256 | 10 biquads | FIR null | Biquad null |
|---|---|---|---|---|---|
| scalar | 29.6 ns | 155.6 ns | 21.0 ns | — | — |
| intrinsics SSE2 | 17.0 ns | 62.9 ns | 10.6 ns | -295 dB | bit-exact |
| intrinsics AVX2+FMA | 8.9 ns | 40.0 ns | 8.7 ns | -295 dB | -258 dB |
| std::experimental::simd, SSE2 | 16.8 ns | 64.7 ns | 10.8 ns | -295 dB | bit-exact |
| std::experimental::simd, AVX2+FMA | 8.8 ns | 40.4 ns | 8.5 ns | -295 dB | -259 dB |
| Highway, dispatched to AVX2 | 9.0 ns | 42.2 ns | 9.4 ns | -295 dB | -258 dB |

All costs are per stereo frame.

On x86 the three are within 10 % of each other at each instruction set.
Across two runs Highway trailed AVX2 intrinsics by 0–2 % in one and 1–8 % in
the other. Speed
does not decide this, so the other properties do:

- **Highway** gives one source per kernel with runtime dispatch built in
  (SSE2, SSE4, AVX2, AVX-512, NEON, SVE). It has the byte lookups the DSD
  stage needs (`TableLookupBytes`, which is `pshufb` and `tbl`). It is
  Apache-2.0, compatible with GPL-3. It is packaged as `libhwy-dev` 1.2.0 in
  Debian 13 and as `highway-devel` 1.3.0 in Fedora 44.
- **Intrinsics** give full control and nothing to depend on, but every
  kernel has to be written three times (SSE2, AVX2+FMA, NEON), plus hand-made
  dispatch.
- **std::experimental::simd** picks its width at compile time only, so
  runtime dispatch means one translation unit per instruction set and an ODR
  hazard between them. It has no byte shuffle. It is libstdc++-only, and its
  successor `std::simd` is C++26, which arrives with GCC 16; Debian 13 has
  GCC 14.

On the Pi 5, with Debian 13's Highway 1.2.0, the three are within 7 % of
each other on every kernel:

| Mechanism, Cortex-A76 | FIR 64 | FIR 256 | 10 biquads | FIR null | Biquad null |
|---|---|---|---|---|---|
| scalar | 110.9 ns | 430.6 ns | 54.6 ns | — | — |
| intrinsics NEON | 37.4 ns | 151.3 ns | 31.1 ns | -294 dB | -258 dB |
| std::experimental::simd, NEON | 36.2 ns | 130.3 ns | 31.4 ns | -294 dB | -258 dB |
| Highway, dispatched to NEON | 37.3 ns | 139.9 ns | 30.7 ns | -294 dB | -258 dB |
| Highway, sections pipelined | 37.4 ns | 139.6 ns | 23.0 ns | -294 dB | -258 dB |

Intrinsics' 256-tap FIR measured 139.5 ns in an earlier build with the same
kernel code, so read that cell as ±8 %.

The biquad cascade is bound by latency, not width: each section waits for
the one before it. Pipelined, each section works a frame behind the one
before it, so the ten are independent within a step. Each call fills and
drains the pipeline, so the output lines up with the input, there is no
added latency, and the state carries between calls as before. That takes
26 % off on the A76 and 20 % on x86, written once in Highway. Production
PEQ uses this cascade on x86 and as the fallback for the AArch64 parallel
kernel (section 11).

Recommendation: Highway, as the system package, coded against Debian 13's
1.2.0 API, with SVE disabled until a measured gain on hardware we have. The
Pi 5 confirms it. The `simd` comparison has not run on a Pi 4's A72.
The production PEQ and DSD Highway kernels have run there (sections 11 and 14). If Highway's NEON trails
hand NEON there, the answer changes.

## 10. FFT: PFFFT, fetched at build time

`kalinka-fft-bench`, a standalone project in
[`benchmarks/dsp/fft/`](../benchmarks/dsp/fft/README.md), compared real
forward plus inverse transforms from 256 to 131072 points, in float and
double. The [results](../benchmarks/dsp/fft/results.md) are from the same
laptop, pinned to one performance core, in a generic build with no `-march`,
as packages ship. Below are the medians over three runs at the convolver's
sizes, in ns per forward plus inverse pair. A spot check at 2048 points
afterwards agreed within 7 %.

| float | 512 | 2048 | 8192 |
|---|---:|---:|---:|
| FFTW 3.3.10 (Fedora, AVX2 codelets at run time) | 447 | 1962 | 9236 |
| PFFFT 1.3.0, unordered layout | 567 | 2499 | 14237 |
| KFR 7.2.0 (run-time dispatch) | 506 | 1942 | 8013 |
| zldsp_fft (Highway, run-time dispatch) | 341 | 1548 | 8876 |
| pocketfft | 1921 | 8566 | 43705 |

| double | 512 | 2048 | 8192 |
|---|---:|---:|---:|
| FFTW | 647 | 3226 | 17577 |
| PFFFT, unordered | 984 | 4814 | 27456 |
| PFFFT, unordered, built `-march=x86-64-v3` | 554 | 2949 | 25155 |
| KFR | 937 | 4193 | 19708 |
| zldsp_fft | 659 | 3223 | 18855 |

- **Accuracy:** all within about 2 dB of each other, -134 to -138 dB in
  float and -309 to -313 dB in double, for the spectrum and the round trip,
  against a long-double reference.
- **Allocation in execute:** none for PFFFT, KFR and zldsp_fft once given
  their buffers. pocketfft allocates on every call. FFTW allocates on every
  inverse when planned to preserve its input, because its planner then picks
  a buffered solver. So FFTW's freedom from allocation depends on the plan it
  chooses on each machine. FFTW's `FFTW_MEASURE` planning takes 0.15–0.6 s per
  size; the others take microseconds.
- **Licence and packaging:**
  - FFTW: GPL-2+, packaged everywhere: `libfftw3-dev` 3.3.10 in Debian 13,
    `fftw-devel` 3.3.10 in Fedora 44.
  - PFFFT: BSD-style, in neither distribution. It is a few C99 files.
  - KFR: GPL-2+, in neither. Its build peaked at 7.4 GB here.
  - zldsp_fft: about six months old, with no release. It was built here on
    Highway 1.4.0; whether Debian 13's 1.2.0 suffices is not checked.
  - pocketfft: BSD-3, in Fedora only.
- **arm64**, measured on the Pi 5 and built on it with GCC 14.2, in ns per
  pair:

  | Cortex-A76 | float 512 | float 2048 | float 8192 | double 512 | double 2048 | double 8192 |
  |---|---:|---:|---:|---:|---:|---:|
  | FFTW 3.3.10 (Debian 13) | 2301–2536 | 10107–11108 | 50020–52906 | 4212 | 20222 | 106921 |
  | PFFFT, unordered | 1407–1553 | 6340–6824 | 32795–34356 | 2758 | 13059 | 74154 |
  | KFR | 12475–13833 | 57120–61512 | 280035–291104 | 12865 | 59856 | 245948 |
  | zldsp_fft | 1661–1833 | 7693–8295 | 38876–40293 | 3365 | 16006 | 82898 |
  | pocketfft | 5021–5536 | 23108–24919 | 118388–123105 | 5716 | 25713 | 143850 |

  PFFFT is the fastest in both precisions at every size. It runs 1.4–1.7×
  as fast as FFTW, and 1.1–1.3× as fast as zldsp_fft. Debian 13 builds its
  arm64 FFTW with NEON for single precision only, and FFTW's own report of
  its double plans confirms they are scalar. In float, FFTW's
  `FFTW_MEASURE` planning took 20–190 s per size on the Pi. Accuracy matched
  x86. The ranges are over two runs in which the board reached 84 C, where a
  Pi 5 may throttle. The libraries were timed in interleaved rounds, so the
  order between them holds.

Recommendation: **PFFFT**, fetched at configure time rather than committed:
`FetchContent` from the v1.3.0 release archive, pinned by its SHA-256. The
renderer's CMake builds its own static library from PFFFT's C sources
rather than PFFFT's build, which carries tests, benchmarks and extra libraries.
The deb and RPM builds run with network access, as they do today.
`FETCHCONTENT_SOURCE_DIR_PFFFT` points an offline build at an unpacked copy,
and a flatpak manifest would list the archive as a source.
`THIRD-PARTY-NOTICES.md` and the packages' documentation still carry its
notice, as its licence asks. The FIR backend builds the double-precision
implementation and the common allocator. Its spectra stay in PFFFT's unordered layout. The first spectral
product initializes the sum and the remaining partitions accumulate into it.
Reasons:

1. It never allocates on execute, by construction: the caller supplies the
   work buffer.
2. It has NEON double, which matters if the Pi 4's 5 % budget allows a
   float64 convolver.
3. It plans in microseconds, so a profile change or a rate change never
   stalls.
4. It is small: 23 files and 5,727 lines, three of them compiled.

Its cost is x86: a generic build uses SSE, so it runs 1.27–1.54× FFTW in
float. At 8192 points that is 14 µs a pair, and x86 has headroom. If it ever
matters, two builds behind a dispatch fix it. KFR is the strongest
alternative on x86, but on the Pi 5 it took 8.5–9.9× PFFFT's time in float
and 3.3–4.8× in double. The Pi 5
run confirms the choice.

### FIR convolution backend

`FirRoomCorrection` is a `DspProcessor` for stereo PCM at the impulse
response's sample rate. It accepts up to 262,144 finite taps per channel and
returns float64 PCM. Preparation builds the filter spectra off the playback
thread. A restart clears the history and reuses the plans and storage.

The default overlap-save filter uses hops of 640 and 6,400 frames. The longer
bank starts at tap 5,760, so its first sample lines up with the shorter bank.
Its right-channel FFT runs half a long hop away from the left channel's. Both
channels share the transform plan and scratch for each size. The processor
reports 640 frames of buffering at every rate. Any delay encoded in the
impulse remains part of its frequency and phase response.

The stage carries the filter across same-rate stereo PCM joins, including
changes of sample width or encoding. A seek can use up to one impulse length
minus one frame of source history, including before the first read. Users will be able to
select the stage once the Core can import FIR assets and the renderer can
load them from a profile. [Pi measurements](../benchmarks/dsp/results.md#partitioned-fir-room-correction)

## 11. Budgets

These are per-stage budgets on a Pi 4 core in real time, under the 5 % rule:
a stage above 5 % is optimised for speed, one below for memory. The figures
are targets for later phases, not measurements. The two right-hand columns
are measured, scaled where noted.

| Stage | CPU target, Pi 4 | Memory target | Measured so far, x86 | Measured so far, Pi 5 |
|---|---|---|---|---|
| DSD to PCM, DSD64 to 176.4 kHz stereo | < 2 % | stage 1 tables ≤ 32 KB | 0.32 %, tables 20 KB | 1.00 % |
| DSD to PCM, DSD256 to 352.8 kHz stereo | < 6 % | ≤ 32 KB tables, then by benchmark | 1.17 %, tables 14 KB | 3.60 % |
| PEQ, 20 bands stereo at 192 kHz | < 1 % | coefficients only | 0.282 %, or 0.322 % including the output stage | 0.698 %, or 0.885 % including the output stage |
| FIR, 65536 taps stereo at 44.1 kHz | < 3 % (2.88 % measured) | 5.04 MiB retained on x86-64 | FFT table (section 10) | 0.670 % including PCM conversion |
| Upsampling 44.1 to 352.8 kHz | < 5 % | filter tables | libsoxr VHQ 2×: 10 ns per input frame, 0.04 % at 44.1 kHz | libsoxr VHQ 2×: 60 ns per input frame, 0.27 % at 44.1 kHz |
| Gain, dither, pack | < 0.5 % | none | today's GainFade 2.5 ns/frame | today's GainFade 8.4 ns/frame |
| Stage overhead (FIFO, segments) | < 0.2 % | one 32768-frame FIFO | DelayLine stage 0.4 ns/frame | DelayLine stage 1.8 ns/frame, 0.07 % at 352.8 kHz |

The DSD rows are measured on a Pi 4 too: 3.24 % and 11.0 %, over both
budgets. Review accepted that (section 14).

On a Pi 4, twenty active stereo PEQ bands at 192 kHz take 49.72 ns per
frame, or 0.955 % of one core, within the 1 % budget. Conversion, preamp,
dither and packing bring the full output stage to 72.34 ns per frame
(1.389 %). On the Pi 5 the same binary takes 36.33 ns for EQ alone and
46.10 ns for the full stage. These are medians of three runs. The AArch64 renderer processes
well-conditioned profiles as parallel pole pairs and uses the original
cascade for repeated poles and other unsuitable profiles. Disabled and
zero-gain bands are omitted. [Three-platform measurements](../benchmarks/dsp/results.md#production-peq-after-review-21-november-2026)

The AArch64 parallel path now covers eligible profiles with 3–5 or 7–20 active
bands. It fills the final group of five with zero-residue modes. Shared profiles
reuse the same coefficients on both channels, and a preamp edit reuses the plan
for the active profile and PCM format. The original nine-band KEF R3 preset
takes 25.87 ns per stereo frame on the Pi 4 and 18.46 ns on the Pi 5 at 192 kHz.
[Preset and settings benchmarks](../benchmarks/dsp/results.md#raspberry-pi-peq-across-preset-sizes-and-live-edits-22-november-2026)

## 12. Harness

All of it is in `tests/dsp/` (library `kalinka_dsp_harness`), and its own
tests are `Harness*`:

- **Generators:**
  - sine;
  - multitone with Schroeder phases;
  - impulse;
  - Farina log sweep;
  - a seventh-order one-bit sigma-delta modulator, with DSF (both bit orders)
    and DFF writers. Its zeros sit at Legendre roots and its Butterworth poles
    give out-of-band gain 1.4. DSF and DFF fixtures round-trip bit-exact
    through `DsdContainer`, mono and stereo.
- **Analysers:**
  - a reference FFT, checked against the DFT;
  - a 7-term Blackman-Harris window, sidelobes under -170 dB;
  - band power in dBFS (AES17);
  - magnitude and phase from an impulse, both on an FFT grid and exact at any
    frequency;
  - THD+N by sine fit and removal;
  - a null tester that finds lag and gain;
  - the click detector, which bounds the sample-to-sample step by the largest
    a clean tone can make.
- **Reference filters:** cookbook biquads in TDF2, a linear-phase FIR
  designed by frequency sampling, FFT convolution.

The modulator's own quality, a 1 kHz tone at half modulation over 0.5 s:

| | Tone THD+N, 20 Hz – 20 kHz | Silence, in band |
|---|---|---|
| DSD64 | -128.0 dB | -142.4 dBFS |
| DSD128 | -172.9 dB | -187.7 dBFS |
| DSD256 | -193.3 dB | -200.9 dBFS |

## 13. Decided in review

1. **Seek near a track's end** should do what the listener expects: seek in
   the track they hear, not refuse and not land in the next one. A Seek
   carries only a position, so it means the source the renderer last
   reported. Two windows break that, because upstream has already moved on
   in both: the ALSA queue, today, and the stage's read-ahead. Phase 1 starts
   with the fix:
   - the switcher keeps each finished source until it is no longer audible,
     and can make it current again with the next one requeued, wound back to
     where it began;
   - `AudioPlayer` keeps those nodes alive until then;
   - the emitter seeks the stream it reports, and drops a pending
     source-change report instead of sending it;
   - `AudioGraphOutputNode::seekTo` names the stream.

   The latency that widens the window is a consequence, not a requirement.
   Phase 2 reduces it where it can: minimum-phase and intermediate filters,
   and partitions sized for latency.

   **Landed in Phase 1, with two differences from the plan:**
   - The stream is named by a new `seekStream(stream, position)` rather than
     by changing `seekTo`. `seekTo` is also the byte seek that decoders make
     on their inputs, and a node holding one stream answers `seekStream` with
     its own `seekTo`. Only the switcher, the stage and the emitter act on the
     name.
   - The switcher holds a finished source until it is disconnected.
     `AudioPlayer` does that at the next append, once the emitter reports a
     later stream.

   The emitter cancels its pending source-change report instead of waiting
   for it, which held a seek near a track's end for as long as the queued
   audio lasted, and then announced the next track anyway. The same
   work found the FLAC decoder reporting FINISHED on any seek made after it
   had decoded to the end of the file: within the last 9 s of a 16/44.1
   FLAC, a seek jumped to the next track. It now clears the end mark before
   emptying the buffer.
2. **DSD level: full scale, with no clipping the source did not have.** The
   SACD specification, as commonly quoted, puts 0 dB at half modulation and
   allows peaks 3.1 dB above it, about 71 % modulation. The default gain,
   `safe`, lands that maximum 0.1 dB under full scale: +2.8 dB. +6 dB stays
   a choice for those who accept that loud masters clip; it is counted and
   reported. Phase 1's acceptance adds a fixture at 71 % modulation that must
   convert with no clipped sample.

   **Landed:** a 1 kHz tone at 71 % modulation, in DSD64 and DSD128, peaks at
   -0.12 dBFS after conversion at the safe gain. Its modulator runs a gentler
   loop, out-of-band gain 1.25: at 1.4 a seventh-order loop goes unstable at
   that level.
3. **THD+N is measured before dither**, against DSD64 and DSD128 fixtures.
   The dither is tested on its own, against the noise statistics it must have.
4. **`Buffer` becomes a ring in Phase 1.** It will be single producer, single
   consumer, with atomic positions. A read or write is at most two `memcpy`
   calls over contiguous spans, never an iterator copy: a ring whose
   iterators wrap falls back to a scalar loop per element, which is what took
   `boost::circular_buffer` from 2 % to 25 % of a Pi. Waiters are woken only
   when the ring goes from empty to holding data, or from full to having
   room. The `kernels` benchmark gets a row for it beside `std::deque`, and
   it replaces the deque only once it is faster on the Pi.

   **Landed in Phase 1** as `ByteRing`, which both the decoders and the HTTP
   stream use. It differs from the plan in two ways:
   - **Wake-ups.** A waiter says how much it waits for, and is woken once that
     much is there. Waking on every empty-to-data change would wake a reader
     waiting for 4 KB once per smaller write. Neither side takes a lock
     unless the other is waiting.
   - **Seeks.** A seek discards from the writer's side while the reader may
     be copying. The discard waits for that copy, one `memcpy` at most, so
     the room is free at once, as `Buffer::clear()` freed it. Had the reader
     dropped the data on its next read instead, a decoder writing during a
     seek would wait for room behind a reader that waits for the seek.

   The HTTP stream no longer gets a callback when the buffer empties.
   Whichever thread finds it empty at the end marks the stream FINISHED, so
   a reader that finds the end also finds the state.

   It is faster on both machines (benchmarks/dsp/results.md). Per frame, one
   thread writing and reading:
   - laptop: 0.21 ns against the deque's 2.2 ns;
   - Pi 5: 0.53 ns against 1.3 ns.

   Read from another thread while a writer refills it:
   - laptop: 0.26–0.30 ns against 4.1 ns;
   - Pi 5: 0.64 ns against 5.3–5.5 ns.

   Playing 24/192 FLAC:
   - laptop: from 0.31 % of a core to 0.26–0.27 %, and the ALSA thread from
     84 ms to 8 ms over three plays;
   - Pi 5: from 0.50–0.51 % to 0.45–0.46 %, the ALSA thread from 67 ms to
     47 ms.

   16/44.1 is unchanged at the bench's resolution.
5. **Highway: the system package**, on Debian 13's 1.2.0 floor.
6. **Targets** are the ones at the top of this note, not Debian 12. The
   runbook and the FFT project now say so.
7. **The stage is lock-free now** (section 3): its only lock is
   `AudioGraphNode`'s, which existed before it.
8. **Pause and seek fade**, briefly, and stay bit-perfect between. In
   Phase 1 the new gain step gives the emitter a fade of a few milliseconds
   at a pause and a resume. At a seek, a stop and a skip, the frames ALSA has
   queued but not yet played are rewound with `snd_pcm_rewind`, faded in
   place and committed again before the drop. Everywhere else the stream
   stays untouched.
9. **PFFFT** is fetched and built with the renderer, pinned by its
   checksum, not committed (section 10).
10. **Decimal settings get a new field type**, `CONFIG_FIELD_TYPE_DECIMAL`.
    Its bounds come in a `ConfigDecimalRange` of doubles, and the value is
    decimal text such as `-3.5`. A renderer declares it only to a Core whose
    negotiated protocol version knows it. An older Core gets the same field
    as STRING, which its app shows as a text box with the right value and
    the renderer validates. The Core's `enum_name()` learns to tolerate
    unknown values, so a future type cannot break a settings page again. It
    lands with the first decimal setting, the Phase 2 preamp.
11. **A device that takes none of the converter's rates.** The converter
    divides by powers of two only, so DSD comes out at 2, 4 or 8 times its
    own family's base: 88.2, 176.4 or 352.8 kHz from the 44.1 kHz family,
    96, 192 or 384 kHz from the 48 kHz one. `output.dsd_pcm_rate` `auto`
    takes 4 times when the device does, else the highest it does.
    Through `default` or `plughw:` a sound server or ALSA's plug layer
    resamples any of them. Through `hw:` nothing does, and many devices
    take only 44.1 and 48 kHz, or only the 48 kHz family: HDMI, onboard
    codecs, simple USB DACs. So:
    - when the device takes none of the three, the converter goes down to
      the base rate itself, 44.1 or 48 kHz, with one more half-band;
    - when it takes no rate of the source's family at all, DSD in `pcm`
      mode is refused with a message saying so, as DSD is refused today
      when native output is not available;
    - crossing into the other family, through a resampler after the
      converter, waits for Phase 2's.

    **Landed** with `output.dsd_pcm_rate`: `dsdPcmRate()` makes the choice
    for both the converter and `chooseDsdOutput()`, which refuses DSD no
    rate of its family can play: "This output cannot play DSD64 as PCM: it
    takes none of 44.1, 88.2, 176.4 or 352.8 kHz".

## 14. DSD to PCM, as built

`DsdToPcm` is Phase 1's converter. It plays `output.dsd_mode` `pcm`, and
`auto` where a track does not play natively, through `DsdToPcmProcessor` in
the stage, which keeps its filters running across a gapless join. All of
section 7's DSD settings have landed.

**Seek pre-roll.** A processor states how far back the input its output
depends on reaches, `history()`. The converter's is its latency and a frame,
as its filters are linear phase. A seek asks upstream for that much before
the target and runs it through, and the stage hands on from the target. The
first frames after a seek are then what a run under way makes, where they
used to start from the idle pattern: a test holds them to an uninterrupted
run's within 1e-12. The history is known only for the format of the run
started last, so a seek in another before its first run starts on time. An
upstream that lands further back than asked plays on from where it landed.

**Where the code is.** All of the DSP is in `native_player/dsp/`:
- the stage, `DspStage`, and `DspProcessor`, the interface it runs audio
  through, with `DelayLine` as its first processor;
- `fir/`, the linear-phase FIR blocks later phases reuse: `SymmetricFir`
  and `HalfBandDecimator`, with their Highway loops;
- `dsd/`, the converter. `DsdToPcm` runs the chain. Stage 1 is an
  interface with a class for each kernel, made by `makeStage1()`, so a
  kernel is added without touching the chain.

**Chains.** Every DSD rate from DSD64 to DSD1024, in both families, converts
to 1, 2, 4 or 8 times 44.1 or 48 kHz with each of the three filters: 120
chains, designed by `scripts/design_dsd_filters.py` into `DsdFilterTaps.h`.
Stage 1 divides by 8 a byte at a time from tables, a half-band
(`HalfBandDecimator`) takes each further factor of 2, and a band filter at the
output rate (`SymmetricFir`) sets the cut-off:
- 30 kHz is flat to 20 kHz and 110 dB down from 40 kHz;
- 50 kHz is flat to 40 kHz and down from 60 kHz;
- wide is flat to 0.45 of the output rate.

At the base rate there is no room for a band filter. The last half-band's
transition, from 20 kHz (21.6 kHz at 48 kHz) to its mirror, is the
cut-off, with 247 taps at 88.2 kHz and 231 at 96 kHz, and the three filters
are one chain. What folds below 20 kHz is 171 dB down.

Where wide comes straight out of stage 1, at DSD64 to 8 times the base, stage
1 alone protects the band, and its 30 groups end it at 123.5 kHz for
352.8 kHz output and 135.9 kHz for 384 kHz. `passbandHz()` reports that.

**Stage 1.** Its taps are whole numbers in units of 2^-36, so every table
entry and every sum is an integer below 2^53, exact in a double in any order.
Zeros at the multiples of fs/8 are part of the design, so the eight phases
sum alike and the idle pattern converts to exact zero. Up to 16 groups, 32 KB,
the tables are whole and built in the stream's own bit order, so neither
DSF's bytes nor DFF's need reversing. Past that only half are stored, MSB
first, and each mirrored group reads its twin's table through bit-reversed
bytes.

**Equiripple, not Kaiser.** Kaiser windows are the usual choice for DSD
decimation: past the edge their stopband keeps falling, so what folds in is
far below the edge figure. An equiripple stopband sits at its limit
throughout. The rejection that matters here is not the acceptance's 130 dB.
It is what keeps THD+N within 0.1 dB of the DSD128 fixture's, near -172 dB,
against DSD's near full-scale ultrasonic noise. Kaiser filters met that with
stage 1 at 170 dB and the half-bands at 162 dB. Equiripple ones were 0.21 dB
over there, and need 176 and 168 dB. They are still much shorter, because a
multiband design leaves free the gaps between the bands that fold into the
passband: DSD64's stage 1 has 80 taps against 152. On the same kernels, with
channel pairs, of one core:

| Conversion | Filters | Laptop | Pi 5 | Pi 4 |
|---|---|---|---|---|
| DSD64 to 176.4 kHz, 30 kHz | equiripple | 0.32 % | 1.07 % | 3.43 % |
| | Kaiser | 0.41 % | 1.47 % | 4.5–5.1 % |
| DSD256 to 352.8 kHz, 50 kHz | equiripple | 1.15 % | 3.85 % | 11.5 % |
| | Kaiser | 1.60 % | 5.44 % | 20.1–21.3 % |

The equiripple figures are medians of three runs, ten on the Pi 4. The Kaiser
ones are one run each, two on the Pi 4. Both are from before stage 1's
kernels became classes, which took up to a further 11 % off on the Pis
without changing a loop; the figures below are from after.

**Stage 1 kernels.** Beside the scalar reference, the three variants the plan
asked for, and rows, added after a profile of the Pi 4. Stage 1 adds whole
numbers, so all five agree bit for bit, and the FIRs after them are the same
vector code:
- four sums: one channel, four partial sums at a time, from the byte tables;
- channel pairs: two channels in one vector, from the byte tables;
- nibble lookups: each 4-bit partial sum split into five byte planes, 16
  entries each, looked up in registers with `tbl` on NEON and `pshufb` on x86;
- rows: the byte tables turned around, so that a byte looks up one row of all
  it adds to the outputs it reaches.

Of the first three, channel pairs were fastest on x86-64 in 29 of 33 rows,
and within 5 % in the rest. They were fastest on the A76 in every row, by
3–16 %. On the Pi 4's A72, nibble lookups were fastest below DSD256, by up to
9 % on medians, and the only kernel steady between runs, within 2 %. The
byte-table kernels moved by 5–38 %, most likely because the A72's L1 data
cache is 32 KB and two-way. Channel pairs led there at DSD256 with the 30 kHz
filter and from DSD512 up, by 10–11 %. Review chose one kernel for every
machine over a pick per CPU, so `DsdKernel::Fast` was channel pairs, and the
Pi 4 ran about 8 % above its best at DSD64
([results](../benchmarks/dsp/results.md)).

**Rows.** A profile of a Pi 4 playing DSD512 as 176.4 kHz PCM put channel
pairs at 34 % of the renderer's CPU, nearly all of it on loads. With G groups
of 8 taps, two outputs of a channel took 2G byte loads and 2G table loads,
each entry from a different table, and the A72 has one load pipeline. Rows
turn the tables around: row b holds byte b's entry in every group's table,
after a zero and padded to an even count. Bytes go a pair at a time. The
first's row is added to a ring of (G + 2) / 2 pending vectors of two outputs
as they stand, the second's a lane on. The first pending vector is then
complete: it goes out and comes back, zeroed, as the last. That is 2 byte
loads and G + 1 vector loads for the same two outputs, and a row of 5 or 6
groups is one cache line. The ring's rotation is a template parameter and the
loop is unrolled by the ring's length, so the pending sums stay in registers
and none moves. Each call starts from zero and first runs the G − 1 bytes
carried from the block before.

Two channels share the loop up to 8 groups. At 10 and 12, the sizes DSD64
uses, their pending sums no longer fit NEON's 32 registers together and the
compiler kept some on the stack, so there the channels run one after the
other. That took a further 6–8 % off stage 1 on a Pi 5 and up to 9 % on a
Pi 4.

The kernel is built for 5, 6, 7, 8, 10 and 12 groups, which is every 30 kHz
and 50 kHz chain; the wide filter's 15 and 30 groups keep channel pairs.
`DsdKernel::Fast` is rows wherever they are built. Against channel pairs,
stage 1 takes 58–63 % less on a Pi 5 and 40–58 % less on a Pi 4, and a
conversion 18–37 % and 14–32 % less. DSD512 to 176.4 kHz, as far as S32,
falls from 4.3 % to 3.0 % of a Pi 5 core and from 11.5 % to 9.1 % of a Pi 4
core. On x86-64 a conversion takes 20–37 % less.

**FIR loops.** The band filter and the half-bands run one loop: a centre
tap, then one tap for each pair of samples either side of it, summed for four
vectors of outputs at a time so that a multiply-add's latency is hidden. On
NEON, where a vector is 2 doubles and there are 32 registers, the four chains
also share their loads. At tap j + 2 a chain needs a vector its neighbour read
at tap j, so a group keeps those in registers and loads 4 vectors for a pair
of taps instead of 16. Every path adds an output's terms in the same order,
so the output is the same bit for bit. For filters of 10 taps and more that
took 13–17 % off the loops on a Pi 5 and 20–34 % on a Pi 4, and 4–12 % and
6–22 % off a conversion. x86-64 keeps the plain loop: with AVX2's 4 lanes the
same sharing would take 32 registers of its 16.

`HalfBandDecimator` keeps its input's two phases apart between blocks and
splits only a block's own samples, in one loop the compiler turns into paired
loads; before, it split its whole history again every block. Each phase's
samples start at an even index, and the input two samples into its buffer, so
that vectors are written where they are aligned. That takes a further 2–9 %
off a conversion on a Pi 5, most at the high rates. On a Pi 4 it is within the
spread between launches, and on x86-64 it measures 1–2 % slower than before.

**Over the Pi 4 budget, accepted.** Section 11's 2 % and 6 % were estimates.
The Pi 4 measures 3.24 % (2.82–3.81 %) and 11.0 % (10.3–12.4 %). It takes
3.1–3.2 times as long as the Pi 5 on the same code, 2.3–2.4 times per clock.
The filter lengths are set by the rejection that THD+N needs, and review
accepted the cost rather than looser filters. A Pi 5 is well inside both
budgets.

**Acceptance**, measured by the converter tests' own analysis over every
chain:

| Measure | Target | x86-64 | arm64, Pi 5 and Pi 4 |
|---|---|---|---|
| Passband ripple, 0 to 20 kHz | within 0.005 dB | 0.00094 dB | 0.00094 dB |
| Rejection of what folds into 0 to 20 kHz | at least 130 dB | 172.7 dB | 172.7 dB |
| Idle pattern 0x69 | exact zero | exact | exact |
| THD+N, DSD64 to 176.4 kHz, 30 kHz, against the fixture | within 0.1 dB | +0.000 dB of -128.3 dB | +0.000 dB of -127.9 dB |
| THD+N, DSD128 to 176.4 kHz, 50 kHz, against the fixture | within 0.1 dB | +0.040 dB of -172.4 dB | +0.073 dB of -172.5 dB |
| Level at 1 kHz | 0.000 dB within 0.001 dB | 0.0002 dB | 0.0002 dB |
| Optimised against the scalar reference | null below -150 dBFS | -313 dBFS | exact |
| DSD64 to 176.4 kHz stereo on a Pi 4 | under 2 % of one core | | 3.24 % |
| DSD256 to 352.8 kHz stereo on a Pi 4 | under 6 % of one core | | 11.0 % |

The Pi 5's test build, run on the Pi 4 as well, gives both boards the same
figures in every chain. The response columns agree with x86-64's to every
printed digit. On arm64 the null is exact
because GCC fuses the reference's multiply-adds there, as the vectors do;
x86-64's baseline has no FMA. The fixtures themselves differ a little between
the two, because the modulator's arithmetic does.

DSD256 is outside the acceptance, which names DSD64 and DSD128 (section 13,
decision 3), and there the conversion does not keep its fixture's THD+N. At
352.8 kHz with the 50 kHz filter it measures -190.2 dB against -193.9 dB on
x86-64, 3.7 dB over, and 4.2 dB over on arm64. That is near the floor of
32-bit output, far below any DAC's. The cause is not established; the likeliest
is what folds in past the decimators, as at DSD128 before the targets rose.

## 15. Open questions

1. **DSD256's THD+N** comes out 3.7–4.2 dB over its fixture's, near -190 dB
   (section 14). It is outside the acceptance; whether to chase it, and at
   what cost on a Pi 4, is open.
