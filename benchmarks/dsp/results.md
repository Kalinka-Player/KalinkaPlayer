# DSP benchmark results

The numbers behind [docs/dsp-design.md](../../docs/dsp-design.md), each table
as `kalinka-dsp-bench` printed it. To add a machine, follow
[RUNBOOK.md](RUNBOOK.md) and add a section; keep the machine header the bench
prints.

## x86_64: Intel Core Ultra 9 185H laptop, 15 November 2026

- CPU: Intel(R) Core(TM) Ultra 9 185H, 22 threads, 30 GB
- Kernel: Linux 7.2.15-300.fc45.x86_64 (Fedora 45)
- Compiler: GCC 16.2.1, `-O3 -DNDEBUG` (CMake Release, as packages build)
- Governor `powersave`: a laptop on battery-friendly clocks. Repeats vary by
  a few percent, and each table is one run; playback was repeated three times.

### Playback today (`playback`)

Played to the end into the ALSA `null` device, volume fixed, from files that
`make-wav` wrote and `flac -5` encoded: 60 s of pink noise under 110, 440 and
3520 Hz tones. 16/44.1 compresses to 73 % and 24/192 to 81 %, denser than
most music. The thread split comes from `/proc` at clock-tick resolution, so it
is approximate; `perf` below is the precise split.

| File | Format | Audio | Wall | CPU | Of one core in real time |
|---|---|---|---|---|---|
| pink-16-44k.flac | 16-bit 44.1 kHz | 60.0 s | 0.04 s | 0.05 s | 0.08 %, 0.08 %, 0.09 % |
| pink-24-192k.flac | 24-bit 192.0 kHz | 60.0 s | 0.18–0.19 s | 0.22–0.24 s | 0.38 %, 0.39 %, 0.37 % |

Through the stage with a 300 ms `DelayLine` (`playback --delay-ms 300`):
0.08 % and 0.40 %, the same within run-to-run variation.

### Where playback's time goes (`perf record -F 20000 -e cpu-clock`, five plays each)

| | 16-bit 44.1 kHz | 24-bit 192 kHz |
|---|---|---|
| Decoder thread | 80.0 % | 76.7 % |
| ALSA thread | 18.1 % | 22.5 % |
| Main thread | 1.9 % | 0.9 % |
| libFLAC rice decoding | 36.3 % | 32.7 % |
| libFLAC LPC restore | 19.5 % | 18.0 % |
| `FlacStreamDecoder::read`, which is `Buffer::read` inlined, on the ALSA thread | 12.7 % | 18.6 % |
| libFLAC frame parsing | 12.6 % | 9.3 % |
| libFLAC CRC | 5.8 % | 8.3 % |
| malloc and free | about 1.5 % | about 2.5 % |

### Kernels (`kernels`)

Per stereo frame, and in brackets of one core in real time at that rate.

| Kernel | 44.1 kHz | 96.0 kHz | 192.0 kHz | 352.8 kHz |
|---|---|---|---|---|
| Buffer<uint8_t> write + read, S32 stereo | 2.26 ns (0.010 %) | 2.26 ns (0.022 %) | 2.26 ns (0.043 %) | 2.26 ns (0.080 %) |
| DspStage without a processor, S32 stereo | 0.04 ns (0.000 %) | 0.04 ns (0.000 %) | 0.04 ns (0.001 %) | 0.04 ns (0.001 %) |
| DspStage with a 300 ms DelayLine, S32 stereo | 0.40 ns (0.002 %) | 0.43 ns (0.004 %) | 0.41 ns (0.008 %) | 0.42 ns (0.015 %) |
| convertSampleFormat S16 to S32, stereo | 3.38 ns (0.015 %) | 3.37 ns (0.032 %) | 3.38 ns (0.065 %) | 3.29 ns (0.116 %) |
| GainFade at -6 dB, S32 stereo (today's software volume) | 2.46 ns (0.011 %) | 2.52 ns (0.024 %) | 2.51 ns (0.048 %) | 2.51 ns (0.089 %) |
| libsoxr VHQ, float64, 2x up, stereo | 9.97 ns (0.044 %) | 10.43 ns (0.100 %) | 10.11 ns (0.194 %) | 9.99 ns (0.352 %) |

### SIMD mechanisms (`simd`)

Per stereo frame; percentages are of one core at 44.1 kHz. The null is the
largest absolute difference from the scalar reference, -400 dB meaning
bit-identical.

| Mechanism | FIR 64 taps, f64 stereo | FIR 256 taps, f64 stereo | 10 biquads, f64 stereo | FIR null | biquad null |
|---|---|---|---|---|---|
| scalar reference | 29.57 ns (0.130 %) | 155.59 ns (0.686 %) | 20.95 ns (0.092 %) | -400.0 dB | -400.0 dB |
| intrinsics SSE2 | 17.01 ns (0.075 %) | 62.92 ns (0.277 %) | 10.57 ns (0.047 %) | -295.0 dB | -400.0 dB |
| intrinsics AVX2+FMA | 8.91 ns (0.039 %) | 39.97 ns (0.176 %) | 8.74 ns (0.039 %) | -295.0 dB | -257.9 dB |
| std::experimental::simd, SSE2 | 16.78 ns (0.074 %) | 64.69 ns (0.285 %) | 10.78 ns (0.048 %) | -295.0 dB | -400.0 dB |
| std::experimental::simd, AVX2+FMA | 8.83 ns (0.039 %) | 40.44 ns (0.178 %) | 8.48 ns (0.037 %) | -295.0 dB | -259.0 dB |
| Highway, dispatched to AVX2 | 8.97 ns (0.040 %) | 42.24 ns (0.186 %) | 9.40 ns (0.041 %) | -295.0 dB | -257.9 dB |
| Highway, sections pipelined, AVX2 | 9.25 ns (0.041 %) | 42.19 ns (0.186 %) | 7.05 ns (0.031 %) | -295.0 dB | -257.9 dB |

Highway 1.3.0, Fedora's package. The pipelined row comes from a later run, in
which intrinsics AVX2+FMA took 8.96 ns for the biquads. Two more runs gave
7.24 and 7.34 ns against 9.10 and 9.27 ns.

In the pipelined cascade each section works a frame behind the one before it.
The ten sections are then independent within a step and run side by side,
instead of each waiting for the latency of the one before. Each call fills
and drains the pipeline, with every section stepping only over that call's
frames, so the output lines up with the input and the state carries over
between calls exactly as in the plain cascade. The null is taken over three
calls of uneven length, one shorter than the cascade, to prove that. It is
20-21 % faster on x86.

### DSD fixtures (`fixtures`)

A 1 kHz tone at half modulation (-6 dB against full modulation) and silence,
0.5 s each, through the harness's seventh-order modulator, out-of-band gain
1.4. THD+N and noise are measured in 20 Hz – 20 kHz on the one-bit stream.

| Rate | tone level | tone THD+N | silence noise |
|---|---|---|---|
| DSD64 | -6.021 dBFS | -128.0 dB | -142.4 dBFS |
| DSD128 | -6.021 dBFS | -172.9 dB | -187.7 dBFS |
| DSD256 | -6.021 dBFS | -193.3 dB | -200.9 dBFS |

With an out-of-band gain of 1.5 the same loop went unstable at DSD128 and
DSD256 within a second, at half modulation.

### Join error, per stream against continuous (`join`)

One continuous 6 s multitone (55, 440, 3520 Hz, peak -6 dBFS) at 44.1 kHz,
split in two. Per stream, each part goes through its own filter from
silence; the first part's tail is flushed with silence and cut at its end.
Continuously, the whole signal goes through one filter. The error is per
stream minus continuous. The jump is how far the error moves between two
samples at the join. A click is a step at the join larger than any the
continuous output makes.

| Stage | Largest error | Jump at the join | Error above one 24-bit step lasts | Click |
|---|---|---|---|---|
| PEQ: 1 kHz +6 dB Q 2, 100 Hz low shelf +6 dB, split at 3 s + 123 | -25.7 dBFS | -25.7 dBFS | 35.1 ms, from 0.0 ms before the join | no |
| FIR: 65535 taps, linear phase, +6 dB at 50 Hz over a quarter octave, split at 3 s + 123 | -22.2 dBFS | -26.6 dBFS | 291.2 ms, from 148.3 ms before the join | no |
| Resampler: libsoxr VHQ, 44.1 to 96 kHz, split at 3 s + 123, off the 147-frame grid | -21.6 dBFS | -22.0 dBFS | 2999.7 ms, from 2.5 ms before the join | no |
| Resampler: libsoxr VHQ, 44.1 to 96 kHz, split at 3 s, on the grid | -21.3 dBFS | -22.6 dBFS | 5.1 ms, from 2.6 ms before the join | yes |

Continuous processing, which the stage does, has no join error by
construction. The stage's own tests prove it delivers a gapless join frame
for frame.

### The ring against the deque (16 November 2026)

Phase 1 replaced `Buffer`, a deque under a lock, with `ByteRing`. Same
machine and build. The kernels ran pinned to cpus 2 and 3, playback and
`perf` to cpu 2. Playback alternated a build of `main` from before the change
with the change itself, three runs each.

| Kernel | 44.1 kHz | 96.0 kHz | 192.0 kHz | 352.8 kHz |
|---|---|---|---|---|
| std::deque buffer write + read, S32 stereo | 2.15 ns (0.009 %) | 2.18 ns (0.021 %) | 2.23 ns (0.043 %) | 2.24 ns (0.079 %) |
| ByteRing write + read, S32 stereo | 0.21 ns (0.001 %) | 0.22 ns (0.002 %) | 0.22 ns (0.004 %) | 0.21 ns (0.008 %) |
| std::deque buffer read, written from another thread | 4.15 ns (0.018 %) | 4.08 ns (0.039 %) | 4.10 ns (0.079 %) | 4.10 ns (0.145 %) |
| ByteRing read, written from another thread | 0.30 ns (0.001 %) | 0.27 ns (0.003 %) | 0.26 ns (0.005 %) | 0.26 ns (0.009 %) |

The deque row is a copy of the old `Buffer`'s hot path kept in the bench, so
the comparison survives the class. The two-thread rows read 1024 frames at a
time while a writer on its own thread keeps the queue full, as a decoder does.

| File | Before: CPU, of one core | After |
|---|---|---|
| pink-16-44k.flac | 0.04 s; 0.06 %, 0.06 %, 0.06 % | 0.03–0.04 s; 0.06 %, 0.06 %, 0.06 % |
| pink-24-192k.flac | 0.19 s; 0.31 %, 0.31 %, 0.31 % | 0.16 s; 0.27 %, 0.26 %, 0.26 % |

`perf record -F 20000 -e cpu-clock` over three 24/192 plays of each build, in
CPU time:

| | Before | After |
|---|---|---|
| All threads | 531 ms | 450 ms |
| Decoder thread | 446 ms | 441 ms |
| ALSA thread | 84 ms | 8 ms |
| The deque's copy, inlined into `BufferedStreamDecoder::read` | 73 ms | gone |
| `memmove` on the ALSA thread | none: the deque copied element by element | 4 ms |

## arm64: Raspberry Pi 5, 15 November 2026

- CPU: Arm Cortex-A76, 4 cores at up to 2.4 GHz; Raspberry Pi 5 Model B
  Rev 1.1, 4 GB
- Kernel: Linux 6.18.50+rpt-rpi-2712 aarch64 (Raspberry Pi OS, Debian 13)
- Compiler: Debian 13's GCC 14.2, `-O3 -DNDEBUG`, built on the Pi against
  Debian 13's own libraries, as the packages are
- Every run pinned to core 3, with the governor left at `ondemand`. Changing
  it needs root, which the run did not have. Core 3 was sampled at 2400 MHz
  throughout the kernel, SIMD and fixture runs, and the board peaked at
  66.1 C. `vcgencmd get_throttled` could not be read without root.
- The installed renderer was running but idle (0.1 % CPU) during all runs.

### Playback today (`playback`)

The same 60 s files as on x86, written by `make-wav` and encoded by Debian
13's `flac -5` on the Pi. Three runs:

| File | Format | Audio | CPU | Of one core in real time |
|---|---|---|---|---|
| pink-16-44k.flac | 16-bit 44.1 kHz | 60.0 s | 0.09 s | 0.15 %, 0.14 %, 0.14 % |
| pink-24-192k.flac | 24-bit 192.0 kHz | 60.0 s | 0.30 s | 0.50 %, 0.50 %, 0.50 % |

Through the stage with a 300 ms `DelayLine`: 0.15 % and 0.54 %.

### Where playback's time goes (`perf record -F 10000 -e cpu-clock`, five plays each)

| | 16-bit 44.1 kHz | 24-bit 192 kHz |
|---|---|---|
| Decoder thread | 90.7 % | 90.8 % |
| ALSA thread | 5.5 % | 7.9 % |
| Main thread | 3.8 % | 1.3 % |

Debian's libFLAC is stripped, so its symbols show as addresses. On the ALSA
thread the time is in libc's copies and frees and in the atomics of
reference counting; `Buffer::read` itself is 0.02-0.03 % of all samples. With
Debian 13's GCC the deque's copies already run as `memmove`, so on the Pi 5
the deque costs far less than on x86, where it was 13-19 % of playback.

### Kernels (`kernels`)

| Kernel | 44.1 kHz | 96.0 kHz | 192.0 kHz | 352.8 kHz |
|---|---|---|---|---|
| Buffer<uint8_t> write + read, S32 stereo | 1.69 ns (0.007 %) | 1.66 ns (0.016 %) | 1.66 ns (0.032 %) | 1.37 ns (0.048 %) |
| DspStage without a processor, S32 stereo | 0.23 ns (0.001 %) | 0.23 ns (0.002 %) | 0.23 ns (0.004 %) | 0.23 ns (0.008 %) |
| DspStage with a 300 ms DelayLine, S32 stereo | 1.72 ns (0.008 %) | 1.79 ns (0.017 %) | 1.83 ns (0.035 %) | 1.87 ns (0.066 %) |
| convertSampleFormat S16 to S32, stereo | 17.60 ns (0.078 %) | 17.60 ns (0.169 %) | 17.60 ns (0.338 %) | 17.56 ns (0.620 %) |
| GainFade at -6 dB, S32 stereo (today's software volume) | 8.38 ns (0.037 %) | 8.38 ns (0.080 %) | 8.38 ns (0.161 %) | 8.37 ns (0.295 %) |
| libsoxr VHQ, float64, 2x up, stereo | 60.27 ns (0.266 %) | 60.25 ns (0.578 %) | 60.27 ns (1.157 %) | 60.08 ns (2.120 %) |

### SIMD mechanisms (`simd`)

| Mechanism | FIR 64 taps, f64 stereo | FIR 256 taps, f64 stereo | 10 biquads, f64 stereo | FIR null | biquad null |
|---|---|---|---|---|---|
| scalar reference | 110.87 ns (0.489 %) | 430.59 ns (1.899 %) | 54.60 ns (0.241 %) | -400.0 dB | -400.0 dB |
| intrinsics NEON | 37.42 ns (0.165 %) | 151.25 ns (0.667 %) | 31.05 ns (0.137 %) | -294.0 dB | -258.0 dB |
| std::experimental::simd, NEON | 36.23 ns (0.160 %) | 130.31 ns (0.575 %) | 31.35 ns (0.138 %) | -294.0 dB | -257.6 dB |
| Highway, dispatched to NEON | 37.32 ns (0.165 %) | 139.88 ns (0.617 %) | 30.74 ns (0.136 %) | -294.0 dB | -258.0 dB |
| Highway, sections pipelined, NEON | 37.41 ns (0.165 %) | 139.64 ns (0.616 %) | 23.03 ns (0.102 %) | -294.0 dB | -258.0 dB |

Highway 1.2.0, Debian 13's package. A second run gave the same to within
1.1 %. Core 3 was at 2400 MHz and the board at 72-75 C. Pipelining takes
26 % off the cascade on the A76.

An earlier build, whose pipelined kernel left wrong state between calls,
measured hand NEON's 256-tap FIR at 139.5 ns over three runs. This build
measures 151.2 ns in both runs, though that kernel's code did not change and
the others moved by under 2 %. So that one figure depends on something like code
alignment, and the 8 % is noise in the comparison, not a property of
intrinsics.

Highway's NEON code is within 7 % of hand-written NEON and
`std::experimental::simd` on every kernel, and ahead of hand NEON on the
longer FIR in this build. A 20-band PEQ at 192 kHz, extrapolated from the ten-section
figure, costs about 2 × 23.0 ns × 192000 = 8.8 ms a second: 0.9 % of a
core pipelined, 1.2 % not.

### FFT

PFFFT is the fastest of the five libraries on the Pi 5 too, in float and in
double; see [fft/results.md](fft/results.md#raspberry-pi-5).

### DSD fixtures and join error

`fixtures` matches x86 to within 0.5 dB at DSD64 and DSD128 (tone -128.1
and -172.4 dB, silence -142.5 and -187.6 dBFS). At DSD256 the figures sit
near the analyser's own floor and differ by up to 3.4 dB: tone -189.9 dB,
silence -203.0 dBFS. `long double` is 80-bit on x86 and 128-bit on arm64.
`join` printed the x86 tables digit for digit.

### The ring against the deque (16 November 2026)

Same board and build as above, still without root, the installed renderer
idle. The kernels ran pinned to cores 2 and 3, playback and `perf` to core 3,
alternating a build of `main` from before the change with the change itself,
three runs each. Core 3 ran at 1500–2400 MHz and the board at 58–62 C.

| Kernel | 44.1 kHz | 96.0 kHz | 192.0 kHz | 352.8 kHz |
|---|---|---|---|---|
| std::deque buffer write + read, S32 stereo | 1.39 ns (0.006 %) | 1.31 ns (0.013 %) | 1.31 ns (0.025 %) | 1.30 ns (0.046 %) |
| ByteRing write + read, S32 stereo | 0.54 ns (0.002 %) | 0.53 ns (0.005 %) | 0.54 ns (0.010 %) | 0.53 ns (0.019 %) |
| std::deque buffer read, written from another thread | 5.49 ns (0.024 %) | 5.28 ns (0.051 %) | 5.41 ns (0.104 %) | 5.39 ns (0.190 %) |
| ByteRing read, written from another thread | 0.65 ns (0.003 %) | 0.64 ns (0.006 %) | 0.64 ns (0.012 %) | 0.65 ns (0.023 %) |

| File | Before: CPU, of one core | After |
|---|---|---|
| pink-16-44k.flac | 0.09 s; 0.14 %, 0.14 %, 0.14 % | 0.08 s; 0.14 %, 0.14 %, 0.14 % |
| pink-24-192k.flac | 0.30 s; 0.51 %, 0.50 %, 0.50 % | 0.27 s; 0.46 %, 0.45 %, 0.46 % |

`perf record -F 10000 -e cpu-clock` over three 24/192 plays of each build, in
CPU time:

| | Before | After |
|---|---|---|
| All threads | 831 ms | 774 ms |
| Decoder thread | 764 ms | 727 ms |
| ALSA thread | 67 ms | 47 ms |

Debian's libc is stripped, so most of the ALSA thread's samples sit at
unnamed libc addresses in both builds. Debian 13's GCC already turned the
deque's copy into `memmove`, so a single thread gains least here; the larger
gain is in the two-thread row, where writer and reader share the deque's lock.

## arm64: Raspberry Pi 4

Not measured yet; see [RUNBOOK.md](RUNBOOK.md).
