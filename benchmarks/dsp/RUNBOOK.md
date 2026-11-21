# DSP benchmark — runbook

How to rerun the renderer's DSP measurements, on a Raspberry Pi or anywhere
else, what each number means, and what a wrong one looks like. The results go
into [results.md](results.md); the design they support is
[docs/dsp-design.md](../../docs/dsp-design.md).

## What it measures

`kalinka-dsp-bench` is built with the renderer, from
`packages/kalinka-renderer/bench/`, and links the real audio graph.

| Command | Answers |
|---|---|
| `playback FILE...` | What playing a file costs today: CPU over audio duration, played to the end into the ALSA `null` device. `--delay-ms N` sends it through the DSP stage holding N ms back |
| `kernels` | Per-frame cost of each audio kernel at 44.1, 96, 192 and 352.8 kHz, and what that is of one core in real time |
| `simd` | The SIMD mechanisms side by side on a float64 FIR and a stereo biquad cascade, each null-tested against the scalar reference |
| `dsd` | What DSD-to-PCM conversion costs, per output frame and of one core, for each stage 1 kernel at the rates the budgets name and beyond |
| `fixtures` | The quality of the DSD test fixtures the harness writes, at DSD64, 128 and 256 |
| `join` | How big a discontinuity per-stream processing leaves at a gapless join, for a PEQ, a 65535-tap FIR and a resampler |
| `make-wav FILE RATE BITS SECONDS` | A dense stereo test signal to encode with `flac` for `playback` |

"Of one core in real time" is processing time over audio time: 5 % means the
stage keeps one core busy 5 % of the time while it plays.

The FFT library comparison is a separate project with its own README:
[fft/](fft/README.md).

## On a Raspberry Pi 4 or 5 (Raspberry Pi OS or Debian 13, arm64)

Stop playback first. The renderer competing for the CPU spoils every number:

```
sudo systemctl stop kalinka-renderer
```

Build. Every package but `flac` and `linux-perf` is the renderer's own
build set from its README. With 2 GB of RAM or less, use
`-j2`.

```
sudo apt install cmake ninja-build g++ libprotobuf-dev protobuf-compiler \
  libboost-dev libspdlog-dev libasound2-dev libflac++-dev libvorbis-dev \
  libssl-dev libcurl4-openssl-dev libcurlpp-dev libxml2-dev libfaad-dev libsoxr-dev \
  libgtest-dev libhwy-dev flac linux-perf
cd KalinkaPlayer/packages/kalinka-renderer
cmake -G Ninja -B build-bench -DCMAKE_BUILD_TYPE=Release -DKALINKA_BUILD_BENCHMARKS=ON
cmake --build build-bench -j3 --target kalinka-dsp-bench
```

On arm64 the `simd` table has scalar, NEON intrinsics,
`std::experimental::simd` and Highway (NEON).

Fix the clock, or the first table runs at one frequency and the last at
another:

```
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
vcgencmd measure_temp; vcgencmd get_throttled
```

Record `get_throttled` before, during and after the measurements. The low
four bits report current conditions and should be clear. Bits 16–19 retain
past events until reboot. Record those separately and let a hot board cool
before starting.

Run, keeping the outputs:

```
B=build-bench/kalinka-dsp-bench
$B make-wav pink-16-44k.wav 44100 16 60 && flac -5 -f pink-16-44k.wav
$B make-wav pink-24-192k.wav 192000 24 60 && flac -5 -f pink-24-192k.wav
for run in 1 2 3; do $B playback pink-16-44k.flac pink-24-192k.flac; done > playback.md
$B playback --delay-ms 300 pink-16-44k.flac pink-24-192k.flac > playback-delay.md
$B kernels > kernels.md
$B simd > simd.md
for run in 1 2 3; do $B dsd; done > dsd.md
$B fixtures > fixtures.md
$B join > join.md
```

`fixtures` and `join` measure maths, not the board. They print the same
numbers everywhere to within rounding, which makes them a check that the build
is sane, not data. The board's data is `playback`, `kernels`, `simd` and
`dsd`.

Where the time goes. Raspberry Pi OS kernels may have no matching
`linux-perf`; if `perf` refuses the running kernel, leave this part out and
say so with the results:

```
sudo sysctl kernel.perf_event_paranoid=1
perf record -F 10000 -e cpu-clock -o perf-24.data \
  $B playback pink-24-192k.flac pink-24-192k.flac pink-24-192k.flac
perf report -i perf-24.data --sort comm --stdio | head
perf report -i perf-24.data --sort comm,dso,symbol --stdio | head -30
```

Restore the governor (`ondemand` on Raspberry Pi OS) and start the renderer
again:

```
echo ondemand | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
sudo systemctl start kalinka-renderer
```

## On x86_64

Same build. On Fedora the packages are in the renderer README plus
`highway-devel flac perf`. Packaged binaries are generic x86-64, so measure the
Release build as it is and never add `-march=native`. The AVX2 rows come from
runtime dispatch and per-function target attributes, so a CPU without AVX2
prints "not on this CPU" for them.

On a laptop, plug it in and note the governor the header prints. Don't build
or run anything else meanwhile: two builds at once can exhaust the memory of a
16–32 GB machine.

## Reading the numbers

- **Playback** is under 1 % of a core on a desktop. On a Pi 4 it is several
  times that, and most of it is libFLAC on the decoder's thread. If the ALSA
  thread's share is far above the decoder's, the device is not `null` or the
  build is not Release.
- **Kernel costs** per frame should not depend on the rate, except the
  `DelayLine` row, whose ring grows with it. A kernel ten times slower at one
  rate than at the others was disturbed: rerun it.
- Run `taskset -c 3 $B kernels PEQ 192000` to measure the production 20-band cascade
  and the dithered output stage in isolation. Compare the 192 kHz cascade row
  with the Pi 4 target of 1% of one core. Record at least three Release runs.
- **SIMD**: each row's null must be at or below -250 dB. -400 dB means
  bit-identical, which is expected without FMA. Anything above -200 dB is a
  bug in that kernel, not rounding.
- **DSD**: compare the kernels within one run. On a Pi 4 the kernels that read
  byte tables move by up to a third between runs while nibble lookups hold
  within a few percent, so a Pi 4 needs several runs and their medians. No
  kernel should be slower than the scalar reference.
- **Fixtures**: THD+N near -128 dB at DSD64, -173 dB at DSD128, -193 dB at
  DSD256. A much worse number means the modulator changed.
- **Join**: four rows, each with an error around -22 to -26 dBFS. Zero would
  mean the per-stream path is no longer cutting anything, which means the
  measurement broke.

## Adding the results

Add a section to [results.md](results.md) under "arm64", named after the
board. Paste the machine header the bench prints and the tables as printed,
with the date, the `get_throttled` value before and after, and anything that
was running.
