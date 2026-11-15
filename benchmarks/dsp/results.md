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

Highway 1.3.0, Fedora's package.

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

## arm64: Raspberry Pi 4 and Pi 5

Not measured yet; see [RUNBOOK.md](RUNBOOK.md).
