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

### DSD to PCM (`dsd`, 16 November 2026)

The converter as committed, with its equiripple filters. Each column times one
stage 1 kernel with the same vectorised FIRs after it, stereo; the reference
column is the scalar code every kernel nulls against. Pinned to cpu 2, three
runs, of which this is the first:

| Conversion | Filter | Scalar reference | Four sums | Channel pairs | Nibble lookups |
|---|---|---|---|---|---|
| DSD64 to 88.2 kHz | 30 kHz | 98.89 ns (0.872 %) | 40.80 ns (0.360 %) | 39.01 ns (0.344 %) | 42.04 ns (0.371 %) |
| DSD64 to 176.4 kHz | 30 kHz | 41.33 ns (0.729 %) | 19.04 ns (0.336 %) | 18.01 ns (0.318 %) | 19.29 ns (0.340 %) |
| DSD64 to 176.4 kHz | 50 kHz | 50.06 ns (0.883 %) | 19.91 ns (0.351 %) | 20.80 ns (0.367 %) | 22.77 ns (0.402 %) |
| DSD64 to 176.4 kHz | wide | 75.23 ns (1.327 %) | 25.37 ns (0.447 %) | 25.08 ns (0.442 %) | 26.79 ns (0.473 %) |
| DSD64 to 352.8 kHz | 30 kHz | 42.23 ns (1.490 %) | 15.38 ns (0.543 %) | 14.80 ns (0.522 %) | 15.60 ns (0.550 %) |
| DSD128 to 176.4 kHz | 50 kHz | 72.70 ns (1.282 %) | 32.04 ns (0.565 %) | 32.50 ns (0.573 %) | 34.38 ns (0.606 %) |
| DSD128 to 352.8 kHz | 50 kHz | 54.18 ns (1.912 %) | 20.73 ns (0.731 %) | 20.82 ns (0.735 %) | 22.09 ns (0.779 %) |
| DSD256 to 352.8 kHz | 30 kHz | 68.57 ns (2.419 %) | 33.66 ns (1.188 %) | 29.31 ns (1.034 %) | 32.59 ns (1.150 %) |
| DSD256 to 352.8 kHz | 50 kHz | 73.87 ns (2.606 %) | 36.35 ns (1.283 %) | 32.31 ns (1.140 %) | 35.99 ns (1.270 %) |
| DSD512 to 352.8 kHz | 50 kHz | 115.38 ns (4.071 %) | 62.45 ns (2.203 %) | 55.52 ns (1.959 %) | 57.85 ns (2.041 %) |
| DSD1024 to 352.8 kHz | 50 kHz | 179.51 ns (6.333 %) | 112.54 ns (3.970 %) | 97.10 ns (3.426 %) | 106.80 ns (3.768 %) |

The first two runs agree within 4 % up to DSD256 and within 11 % above. The
third ran 9–47 % slower in every column at once from DSD64 to 176.4 kHz wide
to DSD256: that is the clock under `powersave`, so compare columns within a
run, not runs. Channel pairs were fastest in 24 of the 33 rows and four sums
in the rest, by at most 5 %.

The Kaiser-window filters of the first commit (stage 1 at 170 dB, half-bands
at 162 dB) on the same kernels, one run:

| Conversion | Filter | Scalar reference | Four sums | Channel pairs | Nibble lookups |
|---|---|---|---|---|---|
| DSD64 to 88.2 kHz | 30 kHz | 106.64 ns (0.941 %) | 62.79 ns (0.554 %) | 55.47 ns (0.489 %) | 55.91 ns (0.493 %) |
| DSD64 to 176.4 kHz | 30 kHz | 41.51 ns (0.732 %) | 26.59 ns (0.469 %) | 23.24 ns (0.410 %) | 22.90 ns (0.404 %) |
| DSD64 to 176.4 kHz | 50 kHz | 51.06 ns (0.901 %) | 30.81 ns (0.543 %) | 27.09 ns (0.478 %) | 26.63 ns (0.470 %) |
| DSD64 to 176.4 kHz | wide | 74.13 ns (1.308 %) | 39.94 ns (0.705 %) | 37.24 ns (0.657 %) | 37.71 ns (0.665 %) |
| DSD64 to 352.8 kHz | 30 kHz | 42.27 ns (1.491 %) | 25.21 ns (0.889 %) | 23.50 ns (0.829 %) | 23.97 ns (0.846 %) |
| DSD128 to 176.4 kHz | 50 kHz | 75.60 ns (1.334 %) | 45.21 ns (0.797 %) | 41.85 ns (0.738 %) | 40.86 ns (0.721 %) |
| DSD128 to 352.8 kHz | 50 kHz | 56.37 ns (1.989 %) | 33.04 ns (1.166 %) | 31.45 ns (1.110 %) | 30.78 ns (1.086 %) |
| DSD256 to 352.8 kHz | 30 kHz | 73.36 ns (2.588 %) | 48.92 ns (1.726 %) | 44.59 ns (1.573 %) | 42.76 ns (1.508 %) |
| DSD256 to 352.8 kHz | 50 kHz | 79.55 ns (2.806 %) | 49.09 ns (1.732 %) | 45.29 ns (1.598 %) | 44.97 ns (1.587 %) |
| DSD512 to 352.8 kHz | 50 kHz | 115.60 ns (4.078 %) | 80.82 ns (2.851 %) | 69.16 ns (2.440 %) | 67.60 ns (2.385 %) |
| DSD1024 to 352.8 kHz | 50 kHz | 193.70 ns (6.834 %) | 144.88 ns (5.111 %) | 123.29 ns (4.350 %) | 117.82 ns (4.157 %) |

After stage 1's kernels became classes, three more runs on cpu 2 matched
these within the `powersave` spread: channel pairs 0.31–0.35 % for DSD64 to
176.4 kHz and 1.13–1.24 % for DSD256 to 352.8 kHz, fastest in 29 of the 33
rows and within 5 % in the rest.

## x86_64: production PEQ, 20 November 2026

Three Release `kalinka-dsp-bench kernels PEQ` runs pinned to logical CPU 1
after a warm-up. The 20-band row includes copying the input block to the two
working planes. The output-stage row reads stereo S32, applies the same
filters and a −6 dB preamp, dithers at 24 bits and packs S32.

At 192 kHz, the cascade took 13.89–14.35 ns per stereo frame, or
0.267–0.276% of one core. The output stage took 16.73–19.04 ns, or
0.321–0.366%. Three earlier runs of the full kernel table on the same laptop
gave 0.309–0.318% and 0.364–0.391%, respectively. The governor was
`powersave` for both sets.

The first isolated run produced these figures.

- CPU: Intel(R) Core(TM) Ultra 9 185H
- Kernel: Linux 7.3.4-300.fc45.x86_64 x86_64
- Compiler: 16.2.1 20261029 (Red Hat 16.2.1-3), flags: -O3 -DNDEBUG
- cpu0 governor: powersave, clock now: 1892964 kHz, zone0: 26.8 C

| Kernel | 44.1 kHz | 96.0 kHz | 192.0 kHz | 352.8 kHz |
|---|---|---|---|---|
| 20 PEQ bands, planar float64 stereo | 13.81 ns (0.061 %) | 14.13 ns (0.136 %) | 14.35 ns (0.276 %) | 14.56 ns (0.514 %) |
| OutputStage with 20 PEQ bands, S32 stereo, dithered at 24 bits | 16.45 ns (0.073 %) | 16.47 ns (0.158 %) | 16.73 ns (0.321 %) | 16.72 ns (0.590 %) |

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

### DSD to PCM (`dsd`, 16 November 2026)

Same board, the converter as committed, pinned to core 3, three runs, of
which this is the first. Core 3 read 1500 MHz as the first run started and
2400 MHz at every later sample; the board ran at 58–68 C.

| Conversion | Filter | Scalar reference | Four sums | Channel pairs | Nibble lookups |
|---|---|---|---|---|---|
| DSD64 to 88.2 kHz | 30 kHz | 291.75 ns (2.573 %) | 154.79 ns (1.365 %) | 134.66 ns (1.188 %) | 141.80 ns (1.251 %) |
| DSD64 to 176.4 kHz | 30 kHz | 132.47 ns (2.337 %) | 71.64 ns (1.264 %) | 61.36 ns (1.082 %) | 64.97 ns (1.146 %) |
| DSD64 to 176.4 kHz | 50 kHz | 156.43 ns (2.759 %) | 77.95 ns (1.375 %) | 69.29 ns (1.222 %) | 74.12 ns (1.307 %) |
| DSD64 to 176.4 kHz | wide | 191.95 ns (3.386 %) | 96.74 ns (1.706 %) | 83.60 ns (1.475 %) | 88.51 ns (1.561 %) |
| DSD64 to 352.8 kHz | 30 kHz | 140.21 ns (4.947 %) | 57.42 ns (2.026 %) | 51.90 ns (1.831 %) | 54.16 ns (1.911 %) |
| DSD128 to 176.4 kHz | 50 kHz | 224.09 ns (3.953 %) | 122.65 ns (2.164 %) | 107.30 ns (1.893 %) | 117.61 ns (2.075 %) |
| DSD128 to 352.8 kHz | 50 kHz | 177.19 ns (6.251 %) | 79.69 ns (2.812 %) | 72.10 ns (2.544 %) | 77.26 ns (2.726 %) |
| DSD256 to 352.8 kHz | 30 kHz | 222.87 ns (7.863 %) | 125.71 ns (4.435 %) | 103.18 ns (3.640 %) | 112.18 ns (3.958 %) |
| DSD256 to 352.8 kHz | 50 kHz | 238.05 ns (8.399 %) | 132.10 ns (4.660 %) | 109.14 ns (3.851 %) | 119.29 ns (4.209 %) |
| DSD512 to 352.8 kHz | 50 kHz | 331.86 ns (11.708 %) | 207.98 ns (7.337 %) | 168.56 ns (5.947 %) | 189.19 ns (6.675 %) |
| DSD1024 to 352.8 kHz | 50 kHz | 533.49 ns (18.821 %) | 377.25 ns (13.309 %) | 305.53 ns (10.779 %) | 339.26 ns (11.969 %) |

The three runs agree within 4 % in every cell. Channel pairs were fastest in
every row of every run, 2–14 % ahead of the next kernel.

The Kaiser-window filters of the first commit, on the same kernels, one run:

| Conversion | Filter | Scalar reference | Four sums | Channel pairs | Nibble lookups |
|---|---|---|---|---|---|
| DSD64 to 88.2 kHz | 30 kHz | 268.52 ns (2.368 %) | 217.37 ns (1.917 %) | 184.04 ns (1.623 %) | 189.49 ns (1.671 %) |
| DSD64 to 176.4 kHz | 30 kHz | 131.96 ns (2.328 %) | 99.84 ns (1.761 %) | 83.48 ns (1.473 %) | 86.25 ns (1.521 %) |
| DSD64 to 176.4 kHz | 50 kHz | 152.04 ns (2.682 %) | 115.50 ns (2.037 %) | 96.32 ns (1.699 %) | 98.79 ns (1.743 %) |
| DSD64 to 176.4 kHz | wide | 171.23 ns (3.021 %) | 130.64 ns (2.304 %) | 114.98 ns (2.028 %) | 117.04 ns (2.065 %) |
| DSD64 to 352.8 kHz | 30 kHz | 141.00 ns (4.974 %) | 85.28 ns (3.009 %) | 77.20 ns (2.723 %) | 78.59 ns (2.773 %) |
| DSD128 to 176.4 kHz | 50 kHz | 219.84 ns (3.878 %) | 170.13 ns (3.001 %) | 148.00 ns (2.611 %) | 154.89 ns (2.732 %) |
| DSD128 to 352.8 kHz | 50 kHz | 177.70 ns (6.269 %) | 115.53 ns (4.076 %) | 104.59 ns (3.690 %) | 107.99 ns (3.810 %) |
| DSD256 to 352.8 kHz | 30 kHz | 228.46 ns (8.060 %) | 172.73 ns (6.094 %) | 146.04 ns (5.152 %) | 153.20 ns (5.405 %) |
| DSD256 to 352.8 kHz | 50 kHz | 241.90 ns (8.534 %) | 173.35 ns (6.116 %) | 154.12 ns (5.437 %) | 160.69 ns (5.669 %) |
| DSD512 to 352.8 kHz | 50 kHz | 343.12 ns (12.105 %) | 287.75 ns (10.152 %) | 238.36 ns (8.409 %) | 251.94 ns (8.888 %) |
| DSD1024 to 352.8 kHz | 50 kHz | 559.60 ns (19.743 %) | 514.71 ns (18.159 %) | 417.13 ns (14.716 %) | 444.43 ns (15.679 %) |

After stage 1's kernels became classes, three more runs, the first as
printed. Every cell's median is 1–11 % below the runs above; the loops are
the same, so the gain is in what surrounds them. Channel pairs lead every row,
by 3–16 %, and the runs agree within 4 %.

| Conversion | Filter | Scalar reference | Four sums | Channel pairs | Nibble lookups |
|---|---|---|---|---|---|
| DSD64 to 88.2 kHz | 30 kHz | 283.66 ns (2.502 %) | 147.78 ns (1.303 %) | 124.79 ns (1.101 %) | 134.16 ns (1.183 %) |
| DSD64 to 176.4 kHz | 30 kHz | 128.51 ns (2.267 %) | 68.79 ns (1.213 %) | 56.42 ns (0.995 %) | 60.91 ns (1.074 %) |
| DSD64 to 176.4 kHz | 50 kHz | 152.53 ns (2.691 %) | 74.29 ns (1.310 %) | 66.89 ns (1.180 %) | 70.30 ns (1.240 %) |
| DSD64 to 176.4 kHz | wide | 188.02 ns (3.317 %) | 92.51 ns (1.632 %) | 79.38 ns (1.400 %) | 84.42 ns (1.489 %) |
| DSD64 to 352.8 kHz | 30 kHz | 138.29 ns (4.879 %) | 55.67 ns (1.964 %) | 50.44 ns (1.780 %) | 52.29 ns (1.845 %) |
| DSD128 to 176.4 kHz | 50 kHz | 215.97 ns (3.810 %) | 114.90 ns (2.027 %) | 99.42 ns (1.754 %) | 109.57 ns (1.933 %) |
| DSD128 to 352.8 kHz | 50 kHz | 173.01 ns (6.104 %) | 75.81 ns (2.675 %) | 68.21 ns (2.407 %) | 73.22 ns (2.583 %) |
| DSD256 to 352.8 kHz | 30 kHz | 215.08 ns (7.588 %) | 117.32 ns (4.139 %) | 94.62 ns (3.338 %) | 104.24 ns (3.677 %) |
| DSD256 to 352.8 kHz | 50 kHz | 230.09 ns (8.118 %) | 123.83 ns (4.369 %) | 101.99 ns (3.598 %) | 111.23 ns (3.924 %) |
| DSD512 to 352.8 kHz | 50 kHz | 315.95 ns (11.147 %) | 190.04 ns (6.704 %) | 152.39 ns (5.376 %) | 173.70 ns (6.128 %) |
| DSD1024 to 352.8 kHz | 50 kHz | 502.07 ns (17.713 %) | 344.74 ns (12.162 %) | 265.63 ns (9.372 %) | 307.47 ns (10.848 %) |

## arm64: Raspberry Pi 4 production PEQ, 20 November 2026

Three Release runs of `taskset -c 3 kalinka-dsp-bench kernels PEQ 192000`
after a warm-up. The Core was paused for the runs and resumed afterward.
The CPU reached 1.8 GHz under load, with the governor at `ondemand`.
The temperature sampled from warm-up through the last run was 76.4–80.3 °C.
The firmware reported `0xe0000` throughout, with all current throttling flags
clear. Earlier runs with the Core busy reached the thermal limit and are
excluded from this table.

| 20 active bands per channel, 192 kHz stereo | Run 1 | Run 2 | Run 3 |
|---|---:|---:|---:|
| Planar float64 cascade, ns/frame (% of one core) | 142.04 (2.727%) | 142.37 (2.733%) | 141.00 (2.707%) |
| Output stage with −6 dB preamp, S32 input and 24-bit dithered S32 output | 163.57 (3.141%) | 163.49 (3.139%) | 163.62 (3.142%) |

The median cascade time was 142.04 ns per stereo frame. The 1% stage
budget allows 52.08 ns at this rate. This measures the DSP output path and
excludes decoding and ALSA.

- CPU: Arm Cortex-A72
- Board: Raspberry Pi 4 Model B Rev 1.5
- Kernel: Linux 6.18.60+rpt-rpi-v8 aarch64
- Compiler: 14.2.0, flags: -O3 -DNDEBUG
- cpu0 governor: ondemand, clock now: 1800000 kHz, zone0: 77.906 C

### Optimized float64 PEQ

The same production benchmark ran on the Pi 4 after a warm-up, pinned to
core 3. The executable was cross-compiled on x86 with Debian 13's AArch64
GCC 14.2 and `-O3 -DNDEBUG`. The Core was running. The three runs started at
1.8 GHz and 63.8–64.3 °C. The firmware reported `0xe0000` after the runs,
with all current throttle flags clear.

| 20 active bands per channel, 192 kHz stereo | Run 1 | Run 2 | Run 3 | Median |
|---|---:|---:|---:|---:|
| Planar float64 PEQ, ns/frame | 50.96 | 49.92 | 49.47 | 49.92 (0.958% of one core) |
| Output stage with −6 dB preamp, S32 input and 24-bit dithered S32 output, ns/frame | 71.58 | 71.55 | 71.38 | 71.55 (1.374%) |

The AArch64 kernel prepares a partial-fraction expansion per channel and
processes five independent pole pairs at a time. Profiles with repeated or
poorly conditioned poles use the original cascade. The benchmark curve
uses the parallel kernel. Its median dropped from 142.04 to 49.92 ns per
stereo frame (2.85× faster).

On the same Pi, a separate 17,003-frame regression with independent 20-band
left and 17-band right curves had maximum absolute differences of
3.96 × 10⁻¹² (−228.0 dBFS) and 1.90 × 10⁻¹² (−234.4 dBFS) against the
scalar cascade. Splitting the input across short and long reads produced
bit-identical samples to processing it continuously.

## Production PEQ after review, 21 November 2026

At 192 kHz, the 1% EQ budget is 52.08 ns per stereo frame. These are three
warmed runs of `kalinka-dsp-bench kernels PEQ 192000` with 20 active bands
per channel. Each timing is the median of seven repeats of 196 blocks of
1,024 frames. This version divides elapsed time by all 200,704 frames
processed. The earlier PEQ results used 200,000 as the divisor.

| Machine | Processing | Run 1 (ns/frame) | Run 2 | Run 3 | Median (ns/frame) | One core |
|---|---|---:|---:|---:|---:|---:|
| Intel Core Ultra 9 185H | Planar float64 EQ | 14.57 | 14.70 | 14.86 | 14.70 | 0.282% |
| Intel Core Ultra 9 185H | Full output stage | 16.42 | 16.76 | 16.79 | 16.76 | 0.322% |
| Raspberry Pi 4, Cortex-A72 | Planar float64 EQ | 49.72 | 49.31 | 50.03 | 49.72 | 0.955% |
| Raspberry Pi 4, Cortex-A72 | Full output stage | 71.81 | 72.94 | 72.34 | 72.34 | 1.389% |
| Raspberry Pi 5, Cortex-A76 | Planar float64 EQ | 36.32 | 36.33 | 36.33 | 36.33 | 0.698% |
| Raspberry Pi 5, Cortex-A76 | Full output stage | 46.10 | 46.11 | 46.09 | 46.10 | 0.885% |

The full stage reads S32 stereo, applies EQ and −6 dB preamp, and dithers
to 24 bits in S32. Both timings exclude the decoder and ALSA. This curve
qualifies for the AArch64 parallel kernel. Repeated or poorly conditioned
poles use the cascade fallback.

Both Pis ran the same Release binary cross-compiled with Debian 13 GCC
14.2 and `-O3 -DNDEBUG`, pinned to core 3. The installed Core and renderer
services stayed active. At each run the Pi 4 reported 1.8 GHz and
63.8–64.8 °C with all current throttling flags clear. The Pi 5 reported
2.4 GHz and 54.6–55.1 °C. Its firmware throttling flags were unavailable
to the benchmark user. The laptop used GCC 16.2.1 and logical CPU 1.
Both boards passed all 39 EQ, output-stage, profile and config tests
selected for the hardware run.

## Production PEQ on digital silence, 22 November 2026

The 20-band stereo benchmark now measures an impulse followed by 16 seconds
of digital silence before timing the silent tail. This lets the 30 Hz section
decay into the subnormal range. The tone run uses the same filters and block
copies. Run both with `kalinka-dsp-bench kernels '20 PEQ bands' 192000`.

Median nanoseconds per stereo frame across three alternating Release runs
with the previous and updated kernels, pinned to logical CPU 1 on the laptop
and core 3 on each Pi. The Pis ran the same cross-compiled binary, with their
installed services running.

| Machine | Tone before | Tone after | Silent tail before | Silent tail after | Silent tail after, % of one core |
|---|---:|---:|---:|---:|---:|
| Intel Core Ultra 9 185H | 14.58 | 14.58 | 2,590.86 | 14.80 | 0.284% |
| Raspberry Pi 4, Cortex-A72 | 50.54 | 49.79 | 50.52 | 49.78 | 0.956% |
| Raspberry Pi 5, Cortex-A76 | 36.33 | 36.34 | 36.32 | 36.34 | 0.698% |

Flushing subnormal values during EQ reduced x86-64 CPU use on the silent tail
from 49.74% of one core to 0.284%. The full output stage on the tone, including
preamp and 24-bit dither, took 16.67 ns/frame on x86-64, 71.49 ns/frame on
the Pi 4, and 46.06 ns/frame on the Pi 5.

## Raspberry Pi PEQ across preset sizes and live edits, 22 November 2026

Three alternating runs on core 3 of each Pi used the same Release benchmark
driver linked against each renderer revision. The tables give medians. The
kernel run used 1,024-frame blocks of stereo PCM at 192 kHz. The nine-band
fixture is the original KEF R3 Spinorama AutoEQ preset.

| EQ alone, ns/stereo frame | Pi 4 before | Pi 4 after | Pi 5 before | Pi 5 after |
|---|---:|---:|---:|---:|
| 5 bands | 41.64 | 13.89 | 16.65 | 9.68 |
| 9 bands, KEF R3 | 94.75 | 25.87 | 50.34 | 18.46 |
| 19 bands | 119.70 | 49.58 | 67.97 | 36.09 |
| 20 bands | 50.89 | 49.61 | 36.33 | 36.10 |
| 20 bands, silent tail | 50.88 | 49.58 | 36.33 | 36.09 |

The original R3 now uses 0.50% of a Pi 4 core and 0.35% of a Pi 5 core at
192 kHz. The full 20-band S32 output stage, including preamp and 24-bit dither,
took 71.65 ns/frame on the Pi 4 and 45.85 ns/frame on the Pi 5.

| Renderer update, µs/edit | Pi 4 before | Pi 4 after | Pi 5 before | Pi 5 after |
|---|---:|---:|---:|---:|
| Select a different 9-band profile | 720.864 | 363.012 | 380.686 | 191.692 |
| Adjust preamp with the 9-band profile active | 717.910 | 0.059 | 376.294 | 0.030 |
| Select a different 20-band profile | 3392.265 | 1701.073 | 1891.646 | 947.239 |
| Adjust preamp with the 20-band profile active | 3386.773 | 0.059 | 1889.980 | 0.030 |

Reproduce with `kalinka-dsp-bench kernels PEQ 192000` and
`kalinka-dsp-bench peq-settings`. The settings benchmark alternates two
in-memory profiles or two preamp values through `OutputStage::setRoomEq`. It
times renderer preparation and publication. Audio rendering, the gain ramp,
network requests and settings persistence are outside that timer.

## arm64: Raspberry Pi 4, 16 November 2026

- CPU: Arm Cortex-A72, 4 cores at up to 1.8 GHz, L1 data cache 32 KB and
  two-way; Raspberry Pi 4 Model B Rev 1.5, 4 GB
- Kernel: Linux 6.18.50+rpt-rpi-v8 aarch64 (Debian 13)
- Compiler: Debian 13's GCC 14.2, `-O3 -DNDEBUG`, built on the Pi against
  Debian 13's own libraries, as the packages are
- Every run pinned to core 3, with the governor left at `ondemand`, without
  root. Core 3 read 600–1800 MHz as a run started and 1800 MHz as it ended;
  the board ran at 67–78 C. `vcgencmd get_throttled` read 0x80000 throughout:
  the soft temperature limit had been reached since boot, but was not active.
- The installed server, renderer and supervisor were running, idle.

The November 16 runs measured the DSD converter.

### DSD to PCM (`dsd`, 16 November 2026)

The converter as committed, ten runs: two in the session that chose the
filters, on the same kernels, three in a row after a build, and five 30 s
apart after a 90 s rest. The first of those five, as printed:

| Conversion | Filter | Scalar reference | Four sums | Channel pairs | Nibble lookups |
|---|---|---|---|---|---|
| DSD64 to 88.2 kHz | 30 kHz | 624.36 ns (5.507 %) | 455.57 ns (4.018 %) | 426.96 ns (3.766 %) | 395.01 ns (3.484 %) |
| DSD64 to 176.4 kHz | 30 kHz | 275.17 ns (4.854 %) | 204.52 ns (3.608 %) | 192.19 ns (3.390 %) | 177.75 ns (3.135 %) |
| DSD64 to 176.4 kHz | 50 kHz | 323.10 ns (5.700 %) | 233.47 ns (4.118 %) | 227.28 ns (4.009 %) | 206.54 ns (3.643 %) |
| DSD64 to 176.4 kHz | wide | 433.36 ns (7.644 %) | 322.79 ns (5.694 %) | 296.48 ns (5.230 %) | 251.11 ns (4.430 %) |
| DSD64 to 352.8 kHz | 30 kHz | 287.23 ns (10.134 %) | 177.36 ns (6.257 %) | 171.41 ns (6.047 %) | 164.77 ns (5.813 %) |
| DSD128 to 176.4 kHz | 50 kHz | 471.58 ns (8.319 %) | 367.51 ns (6.483 %) | 343.75 ns (6.064 %) | 313.77 ns (5.535 %) |
| DSD128 to 352.8 kHz | 50 kHz | 367.81 ns (12.976 %) | 245.29 ns (8.654 %) | 235.87 ns (8.322 %) | 222.08 ns (7.835 %) |
| DSD256 to 352.8 kHz | 30 kHz | 452.92 ns (15.979 %) | 315.11 ns (11.117 %) | 286.24 ns (10.099 %) | 307.65 ns (10.854 %) |
| DSD256 to 352.8 kHz | 50 kHz | 500.56 ns (17.660 %) | 371.70 ns (13.114 %) | 340.23 ns (12.003 %) | 327.74 ns (11.563 %) |
| DSD512 to 352.8 kHz | 50 kHz | 691.84 ns (24.408 %) | 503.78 ns (17.773 %) | 455.72 ns (16.078 %) | 498.95 ns (17.603 %) |
| DSD1024 to 352.8 kHz | 50 kHz | 1131.35 ns (39.914 %) | 884.87 ns (31.218 %) | 794.86 ns (28.043 %) | 887.46 ns (31.310 %) |

Of one core, the median of the ten runs and their range:

| Conversion | Filter | Scalar reference | Four sums | Channel pairs | Nibble lookups |
|---|---|---|---|---|---|
| DSD64 to 88.2 kHz | 30 kHz | 5.51 % (4.97–5.83) | 4.05 % (3.51–4.50) | 3.80 % (3.25–4.30) | 3.49 % (3.42–3.52) |
| DSD64 to 176.4 kHz | 30 kHz | 4.85 % (4.32–5.14) | 3.66 % (3.16–4.08) | 3.43 % (2.84–3.87) | 3.12 % (3.10–3.15) |
| DSD64 to 176.4 kHz | 50 kHz | 5.73 % (5.02–6.36) | 4.11 % (3.48–5.00) | 4.08 % (3.35–4.57) | 3.62 % (3.60–3.65) |
| DSD64 to 176.4 kHz | wide | 7.54 % (6.56–8.28) | 5.45 % (4.50–6.26) | 5.15 % (4.30–5.75) | 4.43 % (4.40–4.45) |
| DSD64 to 352.8 kHz | 30 kHz | 10.13 % (9.58–10.42) | 6.29 % (5.80–6.75) | 6.09 % (5.49–6.55) | 5.80 % (5.77–5.84) |
| DSD128 to 176.4 kHz | 50 kHz | 7.96 % (7.36–8.64) | 6.09 % (5.33–6.87) | 5.73 % (5.01–6.51) | 5.54 % (5.44–5.58) |
| DSD128 to 352.8 kHz | 50 kHz | 12.71 % (12.10–13.38) | 8.33 % (7.69–9.16) | 8.02 % (7.29–8.81) | 7.83 % (7.79–7.92) |
| DSD256 to 352.8 kHz | 30 kHz | 15.93 % (15.53–17.72) | 11.11 % (10.79–13.54) | 10.00 % (9.73–12.28) | 10.86 % (10.78–10.95) |
| DSD256 to 352.8 kHz | 50 kHz | 17.09 % (16.37–18.58) | 12.43 % (11.44–14.18) | 11.47 % (10.70–12.86) | 11.56 % (11.41–11.65) |
| DSD512 to 352.8 kHz | 50 kHz | 24.38 % (23.55–25.42) | 17.62 % (16.98–20.30) | 16.04 % (15.40–18.38) | 17.54 % (17.28–17.70) |
| DSD1024 to 352.8 kHz | 50 kHz | 40.01 % (38.69–42.00) | 30.50 % (29.21–35.37) | 27.46 % (25.98–31.38) | 30.72 % (29.65–31.31) |

Nibble lookups repeat within 3 %, 6 % at DSD1024. Every kernel that reads
2 KB byte tables, the reference among them, moves by 8–36 % between runs.
The likeliest cause is the cache: DSD64's stage 1 alone has 20 KB of
tables, and in a two-way 32 KB L1 whether they collide with the streams
depends on where they land, which changes from run to run. The nibble tables
are 160 bytes a group. The Pi 5's L1 is 64 KB and four-way, and its runs
agree within 4 %.

Below DSD256 nibble lookups are the fastest on medians as well, by 2–16 %.
At DSD256 channel pairs lead by 9 % with the 30 kHz filter and draw with the
50 kHz one, and from DSD512 up they lead by 9–11 %. The converter uses
channel pairs on every machine
([docs/dsp-design.md](../../docs/dsp-design.md), section 14).

The Kaiser-window filters of the first commit, on the same kernels, the first
of two runs alternating with the equiripple ones:

| Conversion | Filter | Scalar reference | Four sums | Channel pairs | Nibble lookups |
|---|---|---|---|---|---|
| DSD64 to 88.2 kHz | 30 kHz | 918.37 ns (8.100 %) | 671.89 ns (5.926 %) | 603.97 ns (5.327 %) | 533.58 ns (4.706 %) |
| DSD64 to 176.4 kHz | 30 kHz | 446.32 ns (7.873 %) | 321.30 ns (5.668 %) | 287.18 ns (5.066 %) | 251.45 ns (4.436 %) |
| DSD64 to 176.4 kHz | 50 kHz | 504.47 ns (8.899 %) | 362.58 ns (6.396 %) | 323.56 ns (5.708 %) | 288.10 ns (5.082 %) |
| DSD64 to 176.4 kHz | wide | 549.04 ns (9.685 %) | 385.56 ns (6.801 %) | 358.96 ns (6.332 %) | 308.16 ns (5.436 %) |
| DSD64 to 352.8 kHz | 30 kHz | 381.34 ns (13.454 %) | 246.36 ns (8.692 %) | 229.45 ns (8.095 %) | 211.96 ns (7.478 %) |
| DSD128 to 176.4 kHz | 50 kHz | 791.79 ns (13.967 %) | 584.25 ns (10.306 %) | 558.14 ns (9.846 %) | 460.86 ns (8.130 %) |
| DSD128 to 352.8 kHz | 50 kHz | 525.44 ns (18.538 %) | 362.05 ns (12.773 %) | 351.08 ns (12.386 %) | 304.09 ns (10.728 %) |
| DSD256 to 352.8 kHz | 30 kHz | 775.40 ns (27.356 %) | 574.04 ns (20.252 %) | 548.57 ns (19.354 %) | 453.14 ns (15.987 %) |
| DSD256 to 352.8 kHz | 50 kHz | 810.94 ns (28.610 %) | 596.13 ns (21.032 %) | 570.68 ns (20.134 %) | 474.72 ns (16.748 %) |
| DSD512 to 352.8 kHz | 50 kHz | 1354.18 ns (47.775 %) | 1048.16 ns (36.979 %) | 993.71 ns (35.058 %) | 799.33 ns (28.200 %) |
| DSD1024 to 352.8 kHz | 50 kHz | 2092.53 ns (73.824 %) | 1721.42 ns (60.732 %) | 1615.41 ns (56.991 %) | 1327.70 ns (46.841 %) |

### After stage 1's kernels became classes (16 November 2026)

Ten runs 30 s apart on core 3, of the bench built on the Pi 5 and copied
over, the board at 62–65 C. Of one core, the median and the range:

| Conversion | Filter | Scalar reference | Four sums | Channel pairs | Nibble lookups |
|---|---|---|---|---|---|
| DSD64 to 88.2 kHz | 30 kHz | 5.38 % (4.85–5.79) | 3.88 % (3.42–4.46) | 3.64 % (3.22–4.20) | 3.37 % (3.35–3.40) |
| DSD64 to 176.4 kHz | 30 kHz | 4.73 % (4.21–5.14) | 3.48 % (3.04–4.07) | 3.24 % (2.82–3.81) | 3.00 % (2.98–3.03) |
| DSD64 to 176.4 kHz | 50 kHz | 5.54 % (4.95–6.13) | 3.88 % (3.45–4.76) | 3.79 % (3.32–4.44) | 3.50 % (3.49–3.53) |
| DSD64 to 176.4 kHz | wide | 7.24 % (6.50–8.33) | 5.04 % (4.36–5.43) | 4.72 % (4.17–5.05) | 4.30 % (4.28–4.33) |
| DSD64 to 352.8 kHz | 30 kHz | 9.99 % (9.49–10.40) | 6.15 % (5.71–6.72) | 5.91 % (5.50–6.51) | 5.67 % (5.66–5.69) |
| DSD128 to 176.4 kHz | 50 kHz | 7.69 % (7.25–8.44) | 5.82 % (5.36–6.75) | 5.40 % (4.85–6.43) | 5.34 % (5.30–5.39) |
| DSD128 to 352.8 kHz | 50 kHz | 12.40 % (11.97–13.21) | 8.09 % (7.67–8.97) | 7.66 % (7.12–8.66) | 7.62 % (7.59–7.66) |
| DSD256 to 352.8 kHz | 30 kHz | 15.52 % (15.43–16.32) | 10.61 % (10.54–12.15) | 9.47 % (9.44–10.87) | 10.42 % (10.35–10.52) |
| DSD256 to 352.8 kHz | 50 kHz | 16.80 % (16.30–18.03) | 12.00 % (11.23–13.66) | 11.01 % (10.30–12.41) | 11.09 % (11.01–11.25) |
| DSD512 to 352.8 kHz | 50 kHz | 23.48 % (23.37–24.65) | 16.80 % (16.63–19.13) | 15.11 % (15.05–17.16) | 16.82 % (16.60–16.98) |
| DSD1024 to 352.8 kHz | 50 kHz | 38.23 % (38.06–40.49) | 28.72 % (28.29–33.81) | 25.82 % (25.23–30.01) | 29.05 % (28.85–29.24) |

Every median is 1–8 % below the runs before. The ranking stands: nibble
lookups lead below DSD256, by up to 9 %, and hold within 2 % between runs;
the byte-table kernels move by 5–38 %; channel pairs lead at DSD256 with the
30 kHz filter and from DSD512 up, by 10–11 %.
