# Renderer DSP — design

Status: Phase 0, for review. Nothing here processes audio yet. What exists is
a DSP stage that holds the audio back and changes nothing, the test and
benchmark harness, and the measurements this note rests on. Every number is
from a run named in [benchmarks/dsp/results.md](../benchmarks/dsp/results.md);
nothing was measured on arm64 yet, and the
[runbook](../benchmarks/dsp/RUNBOOK.md) is how the Pi numbers get made.

The note covers the renderer in `packages/kalinka-renderer`. The rules in
[`CLAUDE.md`](../CLAUDE.md) and
[native-renderer-design.md](native-renderer-design.md) bind it.

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
times faster. That makes `Buffer` the largest cost in today's output path
outside the decoder. Section 13 proposes replacing it with a ring.

The Pi 4 numbers will come from the runbook. Expect them several times higher.

## 3. Placement: one stage after the switcher

Confirmed: the stage sits once, after `AudioStreamSwitcher` and before
`AlsaAudioEmitter`. Three measurements back it.

**The graph is unchanged through it.** `DspStage` is in the graph now,
between switcher and emitter, with no processor, which is today's behaviour.
For tests it can hold PCM back through a `DelayLine` (`dsp.delay_ms`, a graph
key, not a setting). Setting `KALINKA_TEST_DSP_DELAY_MS` sends the existing
gapless, seek, pause, position, switcher and integration tests through it.

| Run | Null device, whole suite | PipeWire, the 126 playback tests in real time |
|---|---|---|
| Without the variable | 764 / 764 | 124 / 126 |
| Stage with no processor | 764 / 764 | 125 / 126 |
| Stage holding audio 300 ms | 764 / 764 | 125 / 126 |

Two tests fail on PipeWire, and neither is the stage. The first is
`PlaybackPositionTest.PlaysOggVorbis`. It waits 5 s for a 6 s track to
finish, and fails the same way before this work. The second,
`AlsaAudioEmitterTest.pause`, failed 1 run in 8 without the variable and 1 in
8 with the empty stage, with no failures in 8 at 300 ms. Its captured failure
paused at 293 ms where it expects 580 ± 30. PipeWire had taken about 290 ms
to start the stream, so the test's sleep began before any audio played.
`getState` fails the same way about once in six runs after PipeWire has been
idle. A new test
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
thread. Today that thread spends 18–22 % of 0.1–0.4 % of a core, and the
kernels measured so far cost fractions of a percent per stage on x86
(section 11). Whether a 65536-tap stereo convolver fits beside the ALSA thread
on a Pi 4 is a Phase 2 measurement. If it doesn't, the stage gets its own
thread between switcher and emitter, as the prompt allows. Moving it into each
stream would bring back every error in the table above.

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
| `seekTo` | Everything held is dropped, upstream seeks, a new run starts from the target. Refused while what plays belongs to a source upstream has already left (section 13) |
| `discard(id)`, from `AudioPlayer::remove` | What plays of that stream ends at once and the next held segment is announced; a later segment of it is skipped |
| `discardAll()`, from `clearAll` and `stop` | Everything held is dropped |
| DSD and DoP | The processor declines them; they pass untouched in every phase |

The only gapless decision is still the emitter's format comparison. It now
compares what the stage presents: the processor's output format and rate
(Phase 1, Phase 2 upsampling) rather than the source's. A fixed output rate
keeps that format constant across a mixed-rate queue. The stage's output
`StreamInfo` carries the translated rate, sample format and `streamSize`.

**Pause.** The emitter writes silence while paused and reads nothing, and the
stage sits idle. Phase 2 fades the pause in float, inside the stage, by
sending the gain ramp below to zero. Today a pause cuts mid-waveform. It is the
same for seeks: section 13 lists both as clicks the harness can detect.

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
  1. The decoder writes into its `Buffer`, as today.
  2. The stage reads that into its input scratch.
  3. One conversion pass to planar float64.
  4. The stages run in place.
  5. The quantiser packs straight into the ALSA mmap area. `read(dest)` already
     receives that pointer when formats match.

  Today there are two copies (decoder to `Buffer`, `Buffer` to mmap). Processing
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
   independent per channel. It comes from a seedable xoshiro256+ generator, two
   uniforms per sample, which is fast and has a long period. Noise shaping comes
   in Phase 2, for 16-bit only.
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

## 7. Settings

The protocol has no decimal field type, and preamp and filter frequencies
need one. Section 13 asks how to add it. Until then a decimal setting is an
INT in hundredths, shown with its unit.

Phase 1:

| Path | Type | Values | Default | Apply cost | Importance |
|---|---|---|---|---|---|
| `output.dsd_mode` | ENUM | gains `pcm`; `auto` becomes native when verified, else PCM | `disabled` | interrupts playback (as now) | simple |
| `dsd.pcm_rate` | ENUM | `auto`, `2x`, `4x`, `8x` of 44.1/48 kHz, by source family | `auto` | instant: the next DSD source takes it | expert |
| `dsd.filter` | ENUM | `auto` (30 kHz for DSD64, 50 kHz above), `30k`, `50k`, `wide` | `auto` | instant, crossfaded | expert |
| `dsd.gain_db` | ENUM | `0`, `3`, `6` | `6` | instant, ramped | expert |

Phase 2:

| Path | Type | Values | Default | Apply cost | Importance |
|---|---|---|---|---|---|
| `dsp.enabled` | BOOL | | false | instant, crossfaded | simple |
| `dsp.profile` | ENUM | profiles the Core has published to this renderer, and `none` | `none` | instant, crossfaded | simple |
| `dsp.bypass` | BOOL | A/B | false | instant, crossfaded | simple |
| `dsp.preamp` | ENUM | `auto`, `manual` | `auto` | instant, ramped | expert |
| `dsp.preamp_db` | INT, hundredths | -3000 to 0 | 0 | instant, ramped | expert |
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
  as "DSD64 to PCM 176.4 kHz, 30 kHz filter, +6 dB"), and the device format.
  It is empty when the stage is untouched. With `device_info` and
  `VolumeState` it settles whether playback is bit-perfect.
- `dsp_latency_ms`: what the stage holds back.
- `DspMeters`, sent at most twice a second while processing: clipped samples
  since the source began, true peak from 4× oversampling, and DSP load as
  processing time over real time, a moving average.

## 9. SIMD: Highway, pending the Pi

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
  Apache-2.0, compatible with GPL-3, and packaged as `libhwy-dev` 1.0.3 in
  bookworm and `highway-devel` 1.3.0 in Fedora.
- **Intrinsics** give full control and nothing to depend on, but every
  kernel has to be written three times (SSE2, AVX2+FMA, NEON), plus hand-made
  dispatch.
- **std::experimental::simd** picks its width at compile time only, so
  runtime dispatch means one translation unit per instruction set and an ODR
  hazard between them. It has no byte shuffle. It is libstdc++-only, and its
  successor `std::simd` is C++26, which bookworm's GCC 12 lacks.

Recommendation: Highway, as the system package, coded against the 1.0.3 API,
with SVE disabled until a measured gain on hardware we have. The Pi run of the
same table decides it: if Highway's NEON trails hand NEON on the A72, the
answer changes.

## 10. FFT: PFFFT, vendored, pending the Pi

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
  - FFTW: GPL-2+, in bookworm (`libfftw3-dev` 3.3.10) and Fedora
    (`fftw-devel` 3.3.10).
  - PFFFT: BSD-style, in neither distribution. It is a few C99 files.
  - KFR: GPL-2+, in neither. Its build peaked at 7.4 GB here.
  - zldsp_fft: about six months old, with no release, and it needs a newer
    Highway than bookworm's.
  - pocketfft: BSD-3, in Fedora only.
- **arm64**, from the documentation and sources, not measured: PFFFT has
  NEON in float and double. Debian builds its arm64 FFTW with NEON for float
  only, so FFTW's double there is scalar.

Recommendation: **PFFFT**, vendored under `third_party/` with
`THIRD-PARTY-NOTICES.md` updated. Its spectra stay in its unordered layout,
and `pffft_zconvolve_accumulate` does the partitioned multiply-accumulate.
Reasons:

1. It never allocates on execute, by construction: the caller supplies the
   work buffer.
2. It has NEON double, which matters if the Pi 4's 5 % budget allows a
   float64 convolver.
3. It plans in microseconds, so a profile change or a rate change never
   stalls.
4. It is small enough to carry.

Its cost is x86: a generic build uses SSE, so it runs 1.27–1.54× FFTW in
float. At 8192 points that is 14 µs a pair, and x86 has headroom. If it ever
matters, two builds behind a dispatch fix it. KFR is the strongest
alternative on speed, but a 7.4 GB build is not something a Pi can do. The Pi
run of `kalinka-fft-bench` confirms or overturns this choice.

## 11. Budgets

These are per-stage budgets on a Pi 4 core in real time, under the 5 % rule:
a stage above 5 % is optimised for speed, one below for memory. The figures
are targets for later phases, not measurements. The x86 column is measured,
scaled where noted.

| Stage | CPU target, Pi 4 | Memory target | Measured so far, x86 |
|---|---|---|---|
| DSD to PCM, DSD64 to 176.4 kHz stereo | < 2 % | stage 1 tables ≤ 32 KB | — |
| DSD to PCM, DSD256 to 352.8 kHz stereo | < 6 % | ≤ 32 KB tables, then by benchmark | — |
| PEQ, 20 bands stereo at 192 kHz | < 1 % | coefficients only | 10 biquads 8.7 ns/frame, so 20 bands at 192 kHz would be about 0.33 % |
| FIR, 65536 taps stereo at 44.1 kHz | < 3 % | spectra of every partition, float64 ≈ 4 MB stereo | FFT table (section 10) |
| Upsampling 44.1 to 352.8 kHz | < 5 % | filter tables | libsoxr VHQ 2×: 10 ns per input frame, 0.04 % at 44.1 kHz |
| Gain, dither, pack | < 0.5 % | none | today's GainFade 2.5 ns/frame |
| Stage overhead (FIFO, segments) | < 0.2 % | one 32768-frame FIFO | DelayLine stage 0.4 ns/frame |

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

## 13. Open questions

1. **Seeking in the last moments of a track.** Once the stage has read into
   the next source, upstream has left the one still playing, so a seek can't
   reach it. Today that window is the ALSA queue, and a seek there lands in
   the *next* track. With the stage it grows by the latency, up to about
   0.75 s for a 65536-tap FIR, and the stage refuses such a seek (it returns
   -1, the emitter keeps playing). Should it refuse, or keep today's landing
   in the next track?
2. **What "-6 dBFS" means for DSD.** The harness counts DSD level against
   full modulation, so half modulation is -6 dB, which is 0 dB SACD. With the
   +6 dB default gain such a tone converts to full-scale PCM. Should the
   Phase 1 acceptance tone be at half modulation, which makes 0 dBFS PCM with
   no headroom at +6 dB, or lower?
3. **Phase 1's THD+N row is tight.** The DSD64 fixture is -128 dB. For the
   converter to stay within 0.1 dB of it, the converter's own noise and
   aliasing must sit about 16 dB lower, near -144 dB, which is where 24-bit
   TPDF dither already is. Should the row be measured before dither, or
   against DSD128 fixtures?
4. **Replace `Buffer`.** It is the largest cost in today's output path after
   libFLAC (section 2). A single-producer single-consumer ring with `memcpy`
   would remove most of it. It is used by every decoder and input node, so
   that touches many files: may Phase 1 do it, or should it be its own change?
5. **A decimal setting type.** Preamp, PEQ and shelf settings need decimals,
   and `ConfigFieldType` has none. Should we add `decimals` to `ConfigRange`
   for INT fields, so older Cores show the raw integer, or a new DECIMAL
   type?
6. **Highway as a system package or vendored.** Bookworm has 1.0.3 and
   Fedora 1.3.0. Coding to 1.0.3 works with both, and vendoring a pinned
   release removes the version skew at the cost of carrying it. Which do you
   prefer?
7. **The stage's lock.** In Phase 0 the stage takes one mutex on the pulling
   thread, which upstream state callbacks also take, briefly, on decoder
   threads. That breaks the "never waits on a lock a control thread holds"
   rule in spirit if not in measured effect. Phase 2 can make the callback
   path lock-free (an atomic snapshot of upstream state). Is that acceptable
   for Phase 1?
8. **Pause and seek clicks.** Today both cut mid-waveform, and the click
   detector will flag them. They are emitter behaviour, older than this work.
   Should Phase 1 fade them through the new gain step, or leave them for the
   Phase 2 transition work?
9. **Vendoring PFFFT.** The recommendation (section 10) carries a third-party
   library in the tree for the first time in the DSP. Do you accept that, or
   would you rather depend on FFTW? FFTW is packaged everywhere but has scalar
   double on Debian arm64, and stays allocation-free only by the plan it
   happens to choose.
