# Real-input FFT libraries: measured results

> What `kalinka-fft-bench` measured on one x86-64 laptop on 2026-11-15, as
> input for choosing the FFT under the renderer's FIR convolver (uniformly
> partitioned overlap-save, 512 to 8192 points per transform). How to build and
> rerun it, on x86-64 and on a Raspberry Pi, is in [README.md](README.md).
> Nothing here was measured on arm64.

## Machine and builds

- **CPU**: Intel Core Ultra 9 185H (Meteor Lake: AVX2 and FMA, no AVX-512),
  the process pinned to CPU 2, a performance core rated at 5.1 GHz. On mains
  power; intel_pstate governor `powersave` with energy preference
  `balance_performance`, turbo on, platform profile `balanced`. Other work on
  the machine had finished before each run started (1-minute load average
  0.9–1.7 at the starts).
- **Compiler**: GCC 16.2.1, CMake Release (`-O3 -DNDEBUG`), `-std=gnu++20`.
- **Generic build**: no `-march`, i.e. the x86-64 baseline (SSE2) that
  distribution packages are built for. This is how each library would ship.
- **AVX2/FMA build**: `-march=x86-64-v3` for C and C++, for the libraries that
  only use what the compiler is allowed (PFFFT, pocketfft). KFR was left out of
  it: it picks its code at run time, so it would run the same code, and its
  build is the heaviest by far (see [Building](#building)). FFTW and zldsp_fft
  also dispatch at run time; they stayed in as a control.
- **Libraries**: FFTW 3.3.10 from Fedora 45 (`fftw-libs` 3.3.10-18, built with
  SSE2, AVX and AVX2 codelets); PFFFT v1.3.0 (marton78 fork); pocketfft `cpp`
  branch at 4d865cb; KFR 7.2.0, multi-architecture build (SSE2, SSE4.1, AVX,
  AVX2, AVX-512 with run-time dispatch); zldsp_fft 8e9b4c1 on Google Highway
  1.4.0, with Highway's dynamic dispatch over SSE2, SSE4 and AVX2.
- **Method**: 21 rounds of one batch of at least 5 ms per library, interleaved
  so that a change of clock speed lands on every library alike; the median is
  reported. Three runs of the generic build and two of the AVX2 build,
  alternating. The summary tables take the median over runs; the full tables
  below are single runs.

Reproduce with (paths as in the README):

```
cmake -S benchmarks/dsp/fft -B build/fft-generic -G Ninja
cmake --build build/fft-generic -j2
cmake -S benchmarks/dsp/fft -B build/fft-avx2 -G Ninja -DFFT_BENCH_WITH_KFR=OFF \
  -DCMAKE_C_FLAGS=-march=x86-64-v3 -DCMAKE_CXX_FLAGS=-march=x86-64-v3
cmake --build build/fft-avx2 -j4
for run in 1 2 3; do
  taskset -c 2 build/fft-generic/kalinka-fft-bench > generic-$run.md
  [ $run = 3 ] || taskset -c 2 build/fft-avx2/kalinka-fft-bench > avx2-$run.md
done
```

## Rows

- **FFTW**: planned `FFTW_MEASURE`, run through the new-array execute calls.
  FFTW's complex-to-real transform overwrites its input, so the adapter copies
  the n + 2 spectrum values into scratch it owns before each inverse. A
  convolver could let FFTW overwrite its accumulator and skip that copy, so
  FFTW's inverse here is a little slower than it needs to be.
- **FFTW preserve-input**: the inverse planned `FFTW_PRESERVE_INPUT` instead.
  FFTW then wraps the transform in its `rdft2-buffered` solver, which copies
  the input into a buffer that it **allocates and frees on every call**.
- **PFFFT**: `pffft_transform_ordered`, interleaved output.
- **PFFFT unordered**: `pffft_transform`, PFFFT's internal order, which is the
  layout `pffft_zconvolve_accumulate()` multiplies in. A convolver built on
  PFFFT would use this one.
- **pocketfft**: `pocketfft::detail::pocketfft_r`, the plan its public `r2c()`
  and `c2r()` build on every call. It transforms in place, so each call copies
  its input first, as `r2c()` and `c2r()` do.
- **KFR**: `dft_plan_real` with the CCs layout and the scratch buffer it asks
  for supplied by the caller.
- **zldsp_fft**: `RFFT`, interleaved output. Its inverse gains n/2 rather than
  n, so the round trip is scaled by 2/n for it; a convolver would fold that
  into the filter.

## Summary at the convolver's sizes

### Speed

Median over runs of the per-run median, forward plus inverse, in ns. Lower is
better.

#### float: forward + inverse, ns per pair (median of runs)

| library | build | n = 512 | n = 1024 | n = 2048 | n = 4096 | n = 8192 |
|---|---|---:|---:|---:|---:|---:|
| FFTW | generic | 447 | 893 | 1962 | 4236 | 9236 |
| FFTW preserve-input | generic | 475 | 928 | 1967 | 4310 | 9295 |
| PFFFT | generic | 629 | 1246 | 2784 | 5774 | 15917 |
| PFFFT unordered | generic | 567 | 1118 | 2499 | 5223 | 14237 |
| pocketfft | generic | 1921 | 3970 | 8566 | 18302 | 43705 |
| KFR | generic | 506 | 1042 | 1942 | 3852 | 8013 |
| zldsp_fft | generic | 341 | 745 | 1548 | 3613 | 8876 |
| FFTW | AVX2/FMA build | 446 | 889 | 1906 | 4301 | 9358 |
| FFTW preserve-input | AVX2/FMA build | 475 | 932 | 1973 | 4434 | 9342 |
| PFFFT | AVX2/FMA build | 562 | 1096 | 2409 | 5060 | 15344 |
| PFFFT unordered | AVX2/FMA build | 501 | 976 | 2148 | 4612 | 13473 |
| pocketfft | AVX2/FMA build | 1428 | 2846 | 6019 | 12732 | 31851 |
| zldsp_fft | AVX2/FMA build | 344 | 752 | 1546 | 3674 | 8917 |

#### double: forward + inverse, ns per pair (median of runs)

| library | build | n = 512 | n = 1024 | n = 2048 | n = 4096 | n = 8192 |
|---|---|---:|---:|---:|---:|---:|
| FFTW | generic | 647 | 1405 | 3226 | 7112 | 17577 |
| FFTW preserve-input | generic | 693 | 1463 | 3302 | 7253 | 17531 |
| PFFFT | generic | 1096 | 2268 | 5250 | 12838 | 30517 |
| PFFFT unordered | generic | 984 | 2018 | 4814 | 11539 | 27456 |
| pocketfft | generic | 1956 | 3813 | 8626 | 21033 | 49759 |
| KFR | generic | 937 | 1945 | 4193 | 9349 | 19708 |
| zldsp_fft | generic | 659 | 1468 | 3223 | 8002 | 18855 |
| FFTW | AVX2/FMA build | 640 | 1402 | 3244 | 7153 | 17786 |
| FFTW preserve-input | AVX2/FMA build | 690 | 1495 | 3369 | 7332 | 17674 |
| PFFFT | AVX2/FMA build | 754 | 1478 | 3502 | 11299 | 29036 |
| PFFFT unordered | AVX2/FMA build | 554 | 1076 | 2949 | 9551 | 25155 |
| pocketfft | AVX2/FMA build | 1813 | 3644 | 8153 | 20671 | 50209 |
| zldsp_fft | AVX2/FMA build | 666 | 1472 | 3226 | 8132 | 19022 |

How to read it:

- **Float, generic build** (how every library would ship in a package):
  zldsp_fft is the fastest from 512 to 4096 points, KFR at 8192. FFTW is
  1.15–1.31× the fastest at every size. PFFFT unordered is 1.27× FFTW at 512
  and 1.54× at 8192. pocketfft is 4.3–4.7× FFTW.
- **The AVX2/FMA build changes little in float.** PFFFT's float code is 4-wide
  SSE in every x86 build; `-march=x86-64-v3` only adds FMA and takes it to
  0.86–0.95× of its generic time. pocketfft gains the most (0.74× at 512), but
  it is still 3.0–3.4× FFTW.
- **Double, generic build**: FFTW and zldsp_fft are the fastest, within 5% of
  each other up to 2048 points. FFTW leads at 4096 and 8192, where zldsp_fft
  is 1.13× and 1.07× it. KFR is 1.12–1.45× FFTW, and PFFFT unordered
  1.44–1.62×. PFFFT's double is 2-wide SSE2 unless compiled with AVX.
- **Double, AVX2/FMA build**: PFFFT switches to 4-wide AVX and becomes the
  fastest of all up to 2048 points (554 ns per pair at 512, 0.87× FFTW). Past
  that it falls back to 1.34–1.41× FFTW.
- **Control**: FFTW and zldsp_fft choose their code at run time. They agree
  between the two builds to within 3%, which is the noise between builds.
- **Double over float** costs 1.45–1.9× for FFTW and 1.7–2.2× for PFFFT
  unordered in the generic build. In the AVX2 build PFFFT pays only 1.11× at
  512.
- **Absolute cost on this machine is small at these sizes.** PFFFT
  unordered's float pair at 8192 points takes 14 µs, and runs once per
  4096-sample hop: 21 ms at 192 kHz. The FFTs of a partitioned convolver are a
  small part of its cost next to the spectrum multiply-accumulate over all
  partitions, which this benchmark does not time. The differences will matter
  on a Raspberry Pi, which was not measured.

### Accuracy

The worst value over n = 512 … 8192 and over all runs, in dB: forward-spectrum
error / round-trip error, each relative to the RMS of the signal. Lower is
better.

Generic build:

| library | float | double |
|---|---:|---:|
| FFTW | -137.7 / -134.4 | -312.6 / -309.5 |
| FFTW preserve-input | -137.7 / -134.4 | -312.6 / -309.5 |
| PFFFT | -137.0 / -134.9 | -311.9 / -309.8 |
| PFFFT unordered | -137.0 / -134.9 | -311.9 / -309.8 |
| pocketfft | -137.8 / -134.8 | -312.2 / -309.0 |
| KFR | -135.7 / -134.7 | -310.5 / -309.4 |
| zldsp_fft | -137.7 / -134.6 | -312.5 / -309.3 |

AVX2/FMA build (only the libraries whose code changes):

| library | float | double |
|---|---:|---:|
| PFFFT | -137.6 / -135.4 | -312.2 / -310.3 |
| PFFFT unordered | -137.6 / -135.4 | -312.2 / -310.3 |
| pocketfft | -138.3 / -135.1 | -312.8 / -309.9 |

- Every library is within about 2 dB of the others.
- **Float**: −134 to −138 dB.
- **Double**: −309 to −313 dB.
- KFR's float spectrum is 1–2 dB worse than the rest.
- The error grows by roughly 0.4 dB per doubling of n; the full tables have
  every size.
- Only FFTW varies between runs, by up to 0.6 dB, because `FFTW_MEASURE`
  picks a different plan each time. Every other library is bit-identical run
  to run.
- The reference is a long-double radix-2 FFT. Against direct summation it
  differs by −365 dB, so it adds nothing visible to these figures.

### Heap use in the audio thread's calls

Counted over the first call and then over 8 further calls in each direction,
at every size, in both precisions, in all five runs. The results were the same
every time.

| Library | forward | inverse |
|---|---|---|
| FFTW (default flags, adapter's own scratch for the inverse) | 0 | 0 |
| FFTW preserve-input | 0 | **1 per call**, n values (8 KiB at n = 2048, float) |
| PFFFT, ordered and unordered (work buffer passed in) | 0 | 0 |
| pocketfft | **1 per call**, n values plus 64 B for alignment | **1 per call**, the same |
| KFR (scratch passed in) | 0 | 0 |
| zldsp_fft | 0 | 0 |

FFTW's zero is a property of the plans its planner chose on this machine, not
a promise. Its buffered solvers allocate in every execute, and the
preserve-input row shows FFTW choosing one. A convolver built on FFTW would
have to plan with default flags, let the inverse overwrite its input, and
still check the chosen plan on each machine. PFFFT, KFR and zldsp_fft are
allocation-free by construction once their buffers are supplied.

### Planning

The plan is made once per filter length, off the audio thread.

#### float: planning time, and heap the plan holds (median of runs)

| library | n = 512 | n = 2048 | n = 8192 | n = 131072 |
|---|---:|---:|---:|---:|
| FFTW | 152 ms, 11.0 KiB | 298 ms, 33.2 KiB | 476 ms, 87.2 KiB | 2795 ms, 1290.8 KiB |
| FFTW preserve-input | 152 ms, 5.1 KiB | 298 ms, 21.9 KiB | 475 ms, 8.7 KiB | 2811 ms, 330.9 KiB |
| PFFFT | 4 µs, 4.3 KiB | 7 µs, 16.3 KiB | 16 µs, 64.3 KiB | 182 µs, 1024.3 KiB |
| PFFFT unordered | 1 µs, 4.3 KiB | 3 µs, 16.3 KiB | 13 µs, 64.3 KiB | 193 µs, 1024.3 KiB |
| pocketfft | 5 µs, 2.3 KiB | 6 µs, 8.3 KiB | 11 µs, 32.3 KiB | 84 µs, 512.4 KiB |
| KFR | 12 µs, 4.7 KiB | 21 µs, 17.0 KiB | 52 µs, 65.4 KiB | 547 µs, 1040.6 KiB |
| zldsp_fft | 10 µs, 12.5 KiB | 13 µs, 32.8 KiB | 27 µs, 117.2 KiB | 150 µs, 1866.6 KiB |

#### double: planning time, and heap the plan holds (median of runs)

| library | n = 512 | n = 2048 | n = 8192 | n = 131072 |
|---|---:|---:|---:|---:|
| FFTW | 176 ms, 21.7 KiB | 337 ms, 90.8 KiB | 572 ms, 292.6 KiB | 4283 ms, 3302.6 KiB |
| FFTW preserve-input | 177 ms, 6.1 KiB | 336 ms, 22.2 KiB | 573 ms, 70.9 KiB | 4264 ms, 1069.1 KiB |
| PFFFT | 5 µs, 8.3 KiB | 10 µs, 32.3 KiB | 30 µs, 128.3 KiB | 443 µs, 2048.3 KiB |
| PFFFT unordered | 3 µs, 8.3 KiB | 7 µs, 32.3 KiB | 30 µs, 128.3 KiB | 411 µs, 2048.3 KiB |
| pocketfft | 4 µs, 4.2 KiB | 5 µs, 16.2 KiB | 8 µs, 64.2 KiB | 80 µs, 1024.3 KiB |
| KFR | 15 µs, 9.4 KiB | 31 µs, 33.6 KiB | 82 µs, 132.5 KiB | 1.1 ms, 2112.5 KiB |
| zldsp_fft | 11 µs, 19.2 KiB | 16 µs, 60.6 KiB | 34 µs, 229.9 KiB | 330 µs, 2940.7 KiB |

- The planning times are for the forward and inverse plans together.
- FFTW's `FFTW_MEASURE` plans take 0.15–0.6 s per size at the convolver's
  sizes, and 2.8 s (float) and 4.3 s (double) at 131072, on this machine. Every
  plan is measured cold, because the adapter forgets FFTW's wisdom after
  planning.
- Debian's arm64 FFTW is configured `--with-slow-timer`, so its
  `FFTW_MEASURE` times with no cycle counter. Planning there was not measured.
- Every other library plans in microseconds.
- The FFTW row's heap includes the adapter's n + 2-value scratch. It also
  depends on the plan chosen, so it differs between runs.
- At n = 256, the first FFTW plan of each precision also creates that
  precision's planner (about 190 KiB); see the full tables.

## Licence, packaging and SIMD

Licences are from each project's licence file. Packages were checked on
packages.debian.org (bookworm) and with `dnf repoquery` (Fedora 45), and
after these runs for the renderer's actual targets: Debian 13 has
`libfftw3-dev` 3.3.10-2 and `libhwy-dev` 1.2.0, Ubuntu 24.04 has 3.3.10 and
1.0.7, Fedora 44 has 3.3.10 and 1.3.0, and none of the three packages PFFFT,
KFR or zldsp_fft. Debian 13's and Ubuntu 24.04's `debian/rules` enable NEON
for single precision only, as bookworm's does. The SIMD
columns come from the documentation and source; only the x86-64 behaviour was
observed, through the code path each library reported in these runs.

| | FFTW 3.3.10 | PFFFT v1.3.0 | pocketfft (cpp) | KFR 7.2.0 | zldsp_fft |
|---|---|---|---|---|---|
| Licence | GPL-2.0-or-later | FFTPACK/NCAR BSD-style (attribution and no-endorsement clauses) | BSD-3-Clause | GPL-2.0-or-later, or commercial | Apache-2.0 (Highway: Apache-2.0 or BSD-3) |
| GPL-3 compatible | yes | yes | yes | yes | yes |
| Debian bookworm | `libfftw3-dev` 3.3.10-1, arm64 built | not packaged | not packaged | not packaged | not packaged; `libhwy-dev` is 1.0.3 |
| Fedora 45 | `fftw-devel` 3.3.10-18 | not packaged | `pocketfft-devel`, a 2023-02-14 snapshot (076cb3d) | not packaged | not packaged; `highway-devel` 1.3.0 |
| x86-64, generic package | run-time dispatch. Fedora: SSE2, AVX, AVX2 codelets. Debian: SSE/SSE2 and AVX, no AVX2 | compile time only: float SSE, double SSE2; AVX for double and FMA need `-mavx`/`-mfma` | none for a single 1-D transform (its vectors only batch several transforms); auto-vectorisation | run-time dispatch over SSE2, SSE4.1, AVX, AVX2, AVX-512 | run-time dispatch over SSE2, SSE4, AVX2 (Highway); no AVX-512 |
| Code path seen here (generic build) | `avx2`, `avx2_128` and `avx` codelets | float `SSE1`, double `SSE2` | scalar | `avx2` | `AVX2` |
| arm64 NEON | float only in Debian (`--enable-neon` on single precision, double is scalar); both in Fedora | float and double on AArch64; the double NEON path was rewritten in v1.2.0 (2026-11-03) | none for a 1-D transform | float and double (`neon64`), chosen at build time; GCC on arm64 supported since 7.2 | 128-bit NEON, no SVE |
| Size limits | any | n = 2^a·3^b·5^c, a multiple of 32 for real transforms | any | any | powers of two |
| Inverse gain | n | n | n | n | n/2 |

The Debian FFTW configure flags are from `debian/rules` of fftw3 3.3.10-1.
Fedora's are from the koji build logs of fftw 3.3.10-18.fc45; the installed
library's version string confirms `sse2-avx-avx2-avx2_128`.

## Recommendation

**PFFFT (marton78 fork, pinned release), fetched at build time and built into
the renderer**, with the
spectra kept in its unordered layout, multiplied with
`pffft_zconvolve_accumulate()` (`pffftd_zconvolve_accumulate()` in double),
and float as the default precision. The
reasons, against the criteria:

- **No allocation on execute, by construction.** It takes its work buffer from
  the caller. FFTW matched it here only because of the plans its planner happened
  to choose, and FFTW allocates per call as soon as it picks a buffered solver.
  pocketfft allocates on every call and is out for the audio thread.
- **NEON in both precisions on arm64**, the platform that matters most, and
  the fastest library there in both (see [Raspberry Pi 5](#raspberry-pi-5)).
  On Debian, FFTW's double is scalar code.
- **Built for this job.** The unordered layout and the z-domain
  multiply-accumulate exist for partitioned convolution. The unordered layout
  takes 0.89–0.92× the time of the ordered one in the generic build. Planning
  takes microseconds, so a filter change has no planning step to hide.
- **Accuracy equal to the rest**: within 1 dB of FFTW at the convolver's sizes
  (worst forward error −137.0 dB in float, −311.9 dB in double).
- **Licence**: BSD-style, GPL-3 compatible. It must keep its NCAR notice in
  the sources and the documentation of binary packages.
- **Packaging**: it is in none of Debian 13, Ubuntu 24.04 or Fedora, but it
  is a handful of
  C99 files with no dependencies, about as cheap to fetch and build as a library gets.
  KFR, the other strong option, is 55 heavy translation units on x86 (see
  [Building](#building)) and is not packaged either.
- **What it costs, x86-64**: a generic package gets SSE only. It is 1.27–1.54×
  FFTW in float and 1.44–1.62× in double at 512–8192 points, which is 14 µs per
  8192-point float pair on this machine. Building its double code with AVX makes
  it the fastest of all up to 2048 points. A package would need two builds and a
  dispatcher for that, and these timings do not justify one.

The recommendation is conditional on a Raspberry Pi run, because nothing here
was timed on arm64:

- **If PFFFT's NEON double turns out slow** (the path is less than two weeks
  old), FFTW is a fallback for float only.
- **If KFR's float lead at 8192 holds on the Pi**, it is the faster option. It
  dispatches at run time on x86, has NEON in both precisions and never
  allocated here. What counts against it is the build: a 7457 MiB peak at
  `-j2` for the x86 multi-architecture build, and no packages.
- **zldsp_fft was the fastest in float up to 4096 points.** It is about six
  months old, has no release, and needs a newer Highway than Ubuntu 24.04
  ships.
  Worth rechecking later, not depending on now.

Keep the convolver's FFT behind an interface like `RealFft` here, owning its
buffers and exposing forward, inverse and the multiply-accumulate in its own
layout, so that the Pi results can change the choice without touching the
convolver.

## Variance

- **Within a run**: the interquartile range over the 21 rounds is typically
  0.7–1.1% of the median. The worst cells, up to 11.6%, are mostly at
  65536–131072 points.
- **Between runs**: over the three generic runs, max − min over the median is
  2.4% for the median cell and 8.6% at the 90th percentile.
- **The two AVX2 runs**: 0.8% for the median cell and 3.7% at the 90th
  percentile.
- **One disturbed size**: the third generic run's last size, double
  n = 131072, came out 1.6–2.3× slower for every library at once, most likely
  because other work started on the machine: the load average had risen to 2.5
  by the end. The median over three runs discards it.
- Apart from that, a few single cells differ by 10–25% between runs, among
  them FFTW preserve-input's double forward at 1024 and FFTW's float inverse at
  16384.
- Treat differences below about 5% as noise.

## Building

- The project builds with CMake ≥ 3.25 and a C++20 compiler, and fetches its
  pinned dependencies at configure time.
- Without KFR, a `-j4` build peaked at about 1.3 GB.
- KFR's x86 multi-architecture DFT is 55 translation units: 11 sources ×
  5 instruction sets.
- **KFR, built once at `-j2` with GCC 16 under an 8 GiB memory cap**: 270 s,
  peak memory of the whole build 7457 MiB. That is past the cap's 7 GiB reclaim
  threshold and 735 MiB short of the kill limit.
- The largest single compiler process reached 6.9 GB while the AVX-512
  variants were compiling. Even at `-j1`, that much has to be free. Setting
  `KFR_ARCHS` without `avx512` should lower it, but that was not tried.

## Raspberry Pi 5

Two runs on 15 November 2026:

- **Machine:** a Raspberry Pi 5 (Cortex-A76, 4 GB, Debian 13), built on the
  board with its own GCC 14.2 at `-O3`.
- **Libraries:** FFTW is Debian 13's 3.3.10-2. The others are the same pinned
  sources as on x86, KFR included; it built at `-j2` in under two minutes
  alongside the rest.
- **Conditions:** pinned to core 3, governor `ondemand`, since changing it
  needs root. The installed renderer was idle. The board reached 84 C in both
  runs, near where a Pi 5 begins to throttle, and its throttle state could
  not be read without root.

The libraries are timed in interleaved rounds, so a change of clock affects
them all alike; the comparison holds, but the larger absolute times may be
slightly throttled. Forward plus inverse, ns per pair, as the range over the
two runs where they differ by more than 3 %:

| float | 512 | 1024 | 2048 | 4096 | 8192 |
|---|---:|---:|---:|---:|---:|
| FFTW | 2301–2536 | 4809–5275 | 10107–11108 | 22340–25071 | 50020–52906 |
| PFFFT unordered | 1407–1553 | 2869–3120 | 6340–6824 | 13431–14861 | 32795–34356 |
| zldsp_fft | 1661–1833 | 3647–3975 | 7693–8295 | 17247–19081 | 38876–40293 |
| KFR | 12475–13833 | 26776–29230 | 57120–61512 | 134008–146654 | 280035–291104 |
| pocketfft | 5021–5536 | 10361–11301 | 23108–24919 | 48644–53718 | 118388–123105 |

| double | 512 | 1024 | 2048 | 4096 | 8192 |
|---|---:|---:|---:|---:|---:|
| FFTW | 4212 | 9148 | 20222 | 45996 | 106921 |
| PFFFT unordered | 2758 | 5647 | 13059 | 29275 | 74154 |
| zldsp_fft | 3365 | 7093 | 16006 | 35672 | 82898 |
| KFR | 12865 | 27074 | 59856 | 121376 | 245948 |
| pocketfft | 5716 | 11574 | 25713 | 55816 | 143850 |

- **PFFFT's unordered layout is the fastest in both precisions at every
  convolver size.** FFTW takes 1.5–1.7× its time in float and 1.4–1.6× in
  double, zldsp_fft 1.2–1.3× and 1.1–1.3×.
- **Debian 13's FFTW runs scalar code in double on arm64**, as its own report
  of the plan says ("scalar / scalar"), which confirms the packaging finding.
  In float it runs NEON.
- **FFTW's `FFTW_MEASURE` planning took 20 to 190 s per size in float.** In
  double it took 11 to 59 ms. Either way, FFTW on the Pi would need wisdom
  saved ahead of time, or `FFTW_ESTIMATE`.
- **FFTW allocates on every call at 131072 points in double**, both
  directions, even without `FFTW_PRESERVE_INPUT`. PFFFT, KFR and zldsp_fft
  never allocated. pocketfft allocated on every call, as on x86.
- **KFR, built with GCC 14, took 8.5–9.9× PFFFT's time on the A76 in float
  and 3.3–4.8× in double**, unlike on x86. Whether Clang would change that was not tested.
- **Accuracy** matched x86 and the libraries agreed within about 2 dB:
  -134 to -141 dB in float, -308 to -317 dB in double.

## Not measured

- **A Raspberry Pi 4 (Cortex-A72)**: only the Pi 5 was measured on arm64.
- **A Clang build**: every number is from GCC 16. KFR's documentation no
  longer prefers Clang since 7.1, but whether Clang is faster here was not
  tested.
- **KFR in the AVX2/FMA build.** It dispatches at run time, so it would run
  the same code.
- **AVX-512 code paths**: this CPU has no AVX-512.
- **Debian's FFTW** (no AVX2 codelets): only Fedora's build was timed.
- **Fedora's packaged pocketfft snapshot**: the `cpp` branch head was timed
  instead.
- **FFTW with `FFTW_ESTIMATE`, `FFTW_PATIENT` or saved wisdom**: only cold
  `FFTW_MEASURE`.
- **The convolver itself**: the spectrum multiply-accumulate, block
  bookkeeping, and the cost of FFTW overwriting an accumulator instead of
  copying it first.
- **Lengths that are not powers of two.**
- **Thermal state**: no check for throttling on this laptop beyond the spread
  figures above.
- **Timing of the other heap paths**: PFFFT with a null work buffer (stack)
  and KFR with a null scratch buffer (heap per call, per its documentation).

## Full tables

Single runs, as the program prints them. The other runs differ as described under [Variance](#variance).

<details>
<summary>Generic build, run 2 of 3</summary>

### Run

- CPU: Intel(R) Core(TM) Ultra 9 185H; running on CPU 2, allowed 1 CPU(s)
- Compiler: GNU 16.2.1
- Flags: `C: -O3 -DNDEBUG, C++: -O3 -DNDEBUG`; target macros: SSE2
- Libraries: FFTW fftw-3.3.10-sse2-avx-avx2-avx2_128; FFTW preserve-input fftw-3.3.10-sse2-avx-avx2-avx2_128; PFFFT v1.3.0; PFFFT unordered v1.3.0; pocketfft cpp@4d865cb; KFR KFR 7.2.0 optimized sse2 64-bit (gcc-16.2.1/linux) +in; zldsp_fft 8e9b4c1;
- Timing: 21 rounds of one batch (at least 5.0 ms) per library, median shown
- Reference: long double radix-2, 64-bit mantissa; against direct summation at n = 16..1024 it differs by at most -365.2 dB

#### float: forward, median ns per transform (ns per sample)

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | KFR | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|---:|
| 256 | 118 (0.461) | 119 (0.464) | 146 (0.571) | 132 (0.514) | 449 (1.752) | 195 (0.761) | 91 (0.354) |
| 512 | 222 (0.434) | 222 (0.433) | 315 (0.614) | 286 (0.559) | 976 (1.906) | 255 (0.498) | 176 (0.344) |
| 1024 | 439 (0.429) | 441 (0.431) | 620 (0.605) | 560 (0.547) | 1981 (1.934) | 589 (0.576) | 399 (0.389) |
| 2048 | 939 (0.459) | 942 (0.460) | 1386 (0.677) | 1245 (0.608) | 4284 (2.092) | 1102 (0.538) | 797 (0.389) |
| 4096 | 2056 (0.502) | 2033 (0.496) | 2947 (0.719) | 2692 (0.657) | 9045 (2.208) | 1962 (0.479) | 1890 (0.461) |
| 8192 | 4438 (0.542) | 4438 (0.542) | 7876 (0.961) | 6933 (0.846) | 21323 (2.603) | 4089 (0.499) | 4573 (0.558) |
| 16384 | 11506 (0.702) | 11665 (0.712) | 16944 (1.034) | 14785 (0.902) | 45377 (2.770) | 9559 (0.583) | 10434 (0.637) |
| 32768 | 24438 (0.746) | 24312 (0.742) | 37423 (1.142) | 32932 (1.005) | 96182 (2.935) | 21124 (0.645) | 23012 (0.702) |
| 65536 | 55036 (0.840) | 53651 (0.819) | 75266 (1.148) | 67789 (1.034) | 201477 (3.074) | 44764 (0.683) | 47403 (0.723) |
| 131072 | 129581 (0.989) | 125096 (0.954) | 168807 (1.288) | 151929 (1.159) | 450427 (3.436) | 95780 (0.731) | 107453 (0.820) |

Interquartile range over the median, across 21 rounds: typically 0.8%, at worst 3.5% (FFTW, n = 131072).

#### float: inverse, median ns per transform (ns per sample)

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | KFR | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|---:|
| 256 | 119 (0.464) | 130 (0.508) | 146 (0.572) | 128 (0.501) | 445 (1.738) | 164 (0.639) | 84 (0.326) |
| 512 | 225 (0.440) | 251 (0.491) | 318 (0.621) | 281 (0.549) | 949 (1.854) | 252 (0.493) | 165 (0.322) |
| 1024 | 446 (0.435) | 482 (0.470) | 626 (0.612) | 557 (0.544) | 1989 (1.943) | 453 (0.443) | 346 (0.337) |
| 2048 | 978 (0.477) | 1025 (0.501) | 1390 (0.679) | 1247 (0.609) | 4282 (2.091) | 840 (0.410) | 751 (0.367) |
| 4096 | 2182 (0.533) | 2277 (0.556) | 2834 (0.692) | 2531 (0.618) | 9257 (2.260) | 1916 (0.468) | 1730 (0.422) |
| 8192 | 4798 (0.586) | 4857 (0.593) | 8132 (0.993) | 7330 (0.895) | 22646 (2.764) | 4041 (0.493) | 4325 (0.528) |
| 16384 | 12348 (0.754) | 12331 (0.753) | 18269 (1.115) | 15676 (0.957) | 48377 (2.953) | 8816 (0.538) | 9682 (0.591) |
| 32768 | 25652 (0.783) | 26719 (0.815) | 39180 (1.196) | 35745 (1.091) | 102726 (3.135) | 20172 (0.616) | 22871 (0.698) |
| 65536 | 56483 (0.862) | 56030 (0.855) | 79041 (1.206) | 69168 (1.055) | 218176 (3.329) | 42372 (0.647) | 46608 (0.711) |
| 131072 | 142931 (1.090) | 146718 (1.119) | 186351 (1.422) | 169078 (1.290) | 499765 (3.813) | 94347 (0.720) | 114941 (0.877) |

Interquartile range over the median, across 21 rounds: typically 0.9%, at worst 8.0% (PFFFT unordered, n = 131072).

#### float: error in dB relative to the signal, forward spectrum / round trip

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | KFR | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|---:|
| 256 | -140.3 / -137.1 | -140.3 / -137.1 | -139.7 / -137.5 | -139.7 / -137.5 | -141.8 / -138.2 | -138.9 / -137.4 | -139.8 / -136.1 |
| 512 | -139.8 / -137.1 | -139.8 / -137.1 | -138.8 / -136.6 | -138.8 / -136.6 | -140.1 / -136.9 | -137.3 / -136.4 | -139.2 / -136.1 |
| 1024 | -139.4 / -136.1 | -139.4 / -136.1 | -138.9 / -136.5 | -138.9 / -136.5 | -139.1 / -136.1 | -137.2 / -135.9 | -138.6 / -135.5 |
| 2048 | -138.7 / -135.5 | -138.5 / -135.4 | -138.1 / -136.0 | -138.1 / -136.0 | -138.9 / -135.8 | -136.6 / -134.9 | -138.3 / -135.4 |
| 4096 | -138.1 / -134.8 | -138.2 / -134.7 | -137.5 / -135.3 | -137.5 / -135.3 | -138.2 / -135.1 | -136.1 / -135.0 | -137.9 / -134.8 |
| 8192 | -137.7 / -134.4 | -137.7 / -134.4 | -137.0 / -134.9 | -137.0 / -134.9 | -137.8 / -134.8 | -135.7 / -134.7 | -137.7 / -134.6 |
| 16384 | -137.3 / -134.2 | -137.4 / -134.2 | -136.8 / -134.6 | -136.8 / -134.6 | -137.5 / -134.5 | -135.3 / -134.0 | -137.2 / -134.1 |
| 32768 | -137.1 / -133.8 | -137.0 / -133.8 | -136.4 / -134.3 | -136.4 / -134.3 | -137.2 / -134.1 | -134.9 / -133.6 | -136.7 / -133.5 |
| 65536 | -136.7 / -133.5 | -136.8 / -133.6 | -136.1 / -134.0 | -136.1 / -134.0 | -136.9 / -133.9 | -134.5 / -133.4 | -136.6 / -133.4 |
| 131072 | -136.6 / -133.3 | -136.3 / -133.1 | -135.8 / -133.6 | -135.8 / -133.6 | -136.6 / -133.5 | -134.1 / -133.0 | -136.1 / -132.9 |

#### float: planning time, and heap held by the plan

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | KFR | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|---:|
| 256 | 99966 µs, 192.5 KiB | 99727 µs, 4.6 KiB | 26 µs, 2.3 KiB | 1 µs, 2.3 KiB | 7 µs, 1.2 KiB | 32 µs, 1.5 KiB | 20 µs, 7.9 KiB |
| 512 | 152211 µs, 11.0 KiB | 152164 µs, 5.1 KiB | 4 µs, 4.3 KiB | 1 µs, 4.3 KiB | 5 µs, 2.3 KiB | 13 µs, 4.7 KiB | 10 µs, 12.5 KiB |
| 1024 | 229498 µs, 19.0 KiB | 229232 µs, 6.1 KiB | 5 µs, 8.3 KiB | 2 µs, 8.3 KiB | 5 µs, 4.3 KiB | 17 µs, 8.7 KiB | 10 µs, 19.6 KiB |
| 2048 | 298350 µs, 33.2 KiB | 298345 µs, 21.9 KiB | 7 µs, 16.3 KiB | 3 µs, 16.3 KiB | 6 µs, 8.3 KiB | 22 µs, 17.0 KiB | 16 µs, 32.8 KiB |
| 4096 | 376123 µs, 46.0 KiB | 378613 µs, 23.0 KiB | 9 µs, 32.3 KiB | 6 µs, 32.3 KiB | 8 µs, 16.3 KiB | 35 µs, 33.2 KiB | 22 µs, 61.8 KiB |
| 8192 | 477597 µs, 87.2 KiB | 474525 µs, 8.7 KiB | 16 µs, 64.3 KiB | 12 µs, 64.3 KiB | 11 µs, 32.3 KiB | 52 µs, 65.4 KiB | 31 µs, 117.2 KiB |
| 16384 | 643244 µs, 231.2 KiB | 642618 µs, 57.4 KiB | 27 µs, 128.3 KiB | 24 µs, 128.3 KiB | 15 µs, 64.3 KiB | 85 µs, 130.7 KiB | 46 µs, 229.0 KiB |
| 32768 | 949426 µs, 440.2 KiB | 947882 µs, 201.6 KiB | 49 µs, 256.3 KiB | 48 µs, 256.3 KiB | 24 µs, 128.3 KiB | 146 µs, 260.6 KiB | 45 µs, 511.8 KiB |
| 65536 | 1471953 µs, 649.8 KiB | 1468932 µs, 42.3 KiB | 89 µs, 512.3 KiB | 97 µs, 512.3 KiB | 45 µs, 256.3 KiB | 282 µs, 520.6 KiB | 97 µs, 992.0 KiB |
| 131072 | 2795111 µs, 1867.1 KiB | 2810743 µs, 330.9 KiB | 182 µs, 1024.3 KiB | 196 µs, 1024.3 KiB | 88 µs, 512.4 KiB | 548 µs, 1040.6 KiB | 757 µs, 1866.6 KiB |

#### float: heap allocations per call, forward (first call, then each) / inverse (first call, then each), with KiB per call

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | KFR | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|---:|
| 256 | 0, 0 / 0, 0 | 0, 0 / 1 (1.0 KiB), 1 (1.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (1.1 KiB), 1 (1.1 KiB) / 1 (1.1 KiB), 1 (1.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 512 | 0, 0 / 0, 0 | 0, 0 / 1 (2.0 KiB), 1 (2.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (2.1 KiB), 1 (2.1 KiB) / 1 (2.1 KiB), 1 (2.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 1024 | 0, 0 / 0, 0 | 0, 0 / 1 (4.0 KiB), 1 (4.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (4.1 KiB), 1 (4.1 KiB) / 1 (4.1 KiB), 1 (4.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 2048 | 0, 0 / 0, 0 | 0, 0 / 1 (8.0 KiB), 1 (8.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (8.1 KiB), 1 (8.1 KiB) / 1 (8.1 KiB), 1 (8.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 4096 | 0, 0 / 0, 0 | 0, 0 / 1 (16.0 KiB), 1 (16.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (16.1 KiB), 1 (16.1 KiB) / 1 (16.1 KiB), 1 (16.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 8192 | 0, 0 / 0, 0 | 0, 0 / 1 (32.0 KiB), 1 (32.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (32.1 KiB), 1 (32.1 KiB) / 1 (32.1 KiB), 1 (32.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 16384 | 0, 0 / 0, 0 | 0, 0 / 1 (64.0 KiB), 1 (64.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (64.1 KiB), 1 (64.1 KiB) / 1 (64.1 KiB), 1 (64.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 32768 | 0, 0 / 0, 0 | 0, 0 / 1 (128.0 KiB), 1 (128.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (128.1 KiB), 1 (128.1 KiB) / 1 (128.1 KiB), 1 (128.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 65536 | 0, 0 / 0, 0 | 0, 0 / 1 (256.0 KiB), 1 (256.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (256.1 KiB), 1 (256.1 KiB) / 1 (256.1 KiB), 1 (256.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 131072 | 0, 0 / 0, 0 | 0, 0 / 1 (512.0 KiB), 1 (512.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (512.1 KiB), 1 (512.1 KiB) / 1 (512.1 KiB), 1 (512.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |

#### float: instruction set in use

| library | SIMD path reported |
|---|---:|
| FFTW | avx2+avx2_128 / avx2+avx2_128; avx2+avx2_128 / avx+avx2+avx2_128; avx+avx2+avx2_128 / avx2+avx2_128 |
| FFTW preserve-input | avx2+avx2_128 / avx2+avx2_128; avx+avx2+avx2_128 / avx+avx2+avx2_128 |
| PFFFT | SSE1 |
| PFFFT unordered | SSE1 |
| pocketfft | none (scalar 1-D code) |
| KFR | avx2 |
| zldsp_fft | AVX2 |

#### double: forward, median ns per transform (ns per sample)

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | KFR | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|---:|
| 256 | 148 (0.580) | 150 (0.584) | 252 (0.986) | 224 (0.874) | 456 (1.783) | 205 (0.800) | 171 (0.667) |
| 512 | 305 (0.595) | 306 (0.597) | 551 (1.077) | 495 (0.967) | 988 (1.929) | 439 (0.858) | 348 (0.680) |
| 1024 | 698 (0.682) | 677 (0.661) | 1143 (1.117) | 1023 (0.999) | 1934 (1.889) | 879 (0.858) | 771 (0.753) |
| 2048 | 1509 (0.737) | 1541 (0.753) | 2703 (1.320) | 2500 (1.220) | 4349 (2.124) | 2005 (0.979) | 1710 (0.835) |
| 4096 | 3366 (0.822) | 3427 (0.837) | 6560 (1.601) | 5817 (1.420) | 10520 (2.568) | 4564 (1.114) | 4279 (1.045) |
| 8192 | 7933 (0.968) | 7945 (0.970) | 15093 (1.842) | 13561 (1.655) | 24869 (3.036) | 9631 (1.176) | 9791 (1.195) |
| 16384 | 17348 (1.059) | 17301 (1.056) | 30714 (1.875) | 27885 (1.702) | 51602 (3.150) | 21131 (1.290) | 22367 (1.365) |
| 32768 | 40463 (1.235) | 40458 (1.235) | 67405 (2.057) | 62038 (1.893) | 106854 (3.261) | 45570 (1.391) | 46563 (1.421) |
| 65536 | 86899 (1.326) | 87546 (1.336) | 140969 (2.151) | 126666 (1.933) | 224283 (3.422) | 93734 (1.430) | 104158 (1.589) |
| 131072 | 220878 (1.685) | 226020 (1.724) | 372049 (2.839) | 327862 (2.501) | 560772 (4.278) | 211048 (1.610) | 235931 (1.800) |

Interquartile range over the median, across 21 rounds: typically 0.8%, at worst 6.3% (FFTW preserve-input, n = 131072).

#### double: inverse, median ns per transform (ns per sample)

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | KFR | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|---:|
| 256 | 170 (0.665) | 215 (0.838) | 245 (0.957) | 218 (0.853) | 440 (1.718) | 245 (0.956) | 155 (0.605) |
| 512 | 342 (0.669) | 383 (0.749) | 545 (1.064) | 489 (0.954) | 968 (1.890) | 498 (0.972) | 311 (0.608) |
| 1024 | 729 (0.712) | 767 (0.749) | 1125 (1.099) | 995 (0.972) | 1876 (1.832) | 1066 (1.041) | 690 (0.674) |
| 2048 | 1698 (0.829) | 1761 (0.860) | 2547 (1.244) | 2314 (1.130) | 4277 (2.088) | 2188 (1.069) | 1529 (0.746) |
| 4096 | 3843 (0.938) | 3884 (0.948) | 6278 (1.533) | 5722 (1.397) | 10513 (2.567) | 4785 (1.168) | 3723 (0.909) |
| 8192 | 9782 (1.194) | 9749 (1.190) | 15395 (1.879) | 14434 (1.762) | 24885 (3.038) | 10077 (1.230) | 9064 (1.106) |
| 16384 | 20273 (1.237) | 20608 (1.258) | 31225 (1.906) | 28131 (1.717) | 51115 (3.120) | 22149 (1.352) | 20588 (1.257) |
| 32768 | 44466 (1.357) | 44551 (1.360) | 69422 (2.119) | 62624 (1.911) | 106123 (3.239) | 47416 (1.447) | 43746 (1.335) |
| 65536 | 103272 (1.576) | 106171 (1.620) | 143659 (2.192) | 129396 (1.974) | 222666 (3.398) | 97905 (1.494) | 101503 (1.549) |
| 131072 | 254527 (1.942) | 252246 (1.924) | 380986 (2.907) | 326151 (2.488) | 552774 (4.217) | 211775 (1.616) | 234785 (1.791) |

Interquartile range over the median, across 21 rounds: typically 0.9%, at worst 4.1% (FFTW preserve-input, n = 65536).

#### double: error in dB relative to the signal, forward spectrum / round trip

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | KFR | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|---:|
| 256 | -315.8 / -312.0 | -315.8 / -312.0 | -315.0 / -313.3 | -315.0 / -313.3 | -315.5 / -312.1 | -313.4 / -312.1 | -315.0 / -311.4 |
| 512 | -315.2 / -311.9 | -315.2 / -311.9 | -314.1 / -312.3 | -314.1 / -312.3 | -314.6 / -311.7 | -312.2 / -311.4 | -313.8 / -310.7 |
| 1024 | -314.1 / -311.5 | -314.1 / -311.5 | -313.0 / -311.2 | -313.0 / -311.2 | -313.3 / -310.1 | -312.0 / -310.5 | -313.6 / -310.2 |
| 2048 | -313.6 / -310.2 | -313.6 / -310.6 | -312.9 / -311.1 | -312.9 / -311.1 | -313.1 / -310.1 | -311.7 / -310.5 | -313.4 / -310.2 |
| 4096 | -313.0 / -310.0 | -313.2 / -310.1 | -312.5 / -310.0 | -312.5 / -310.0 | -312.7 / -309.7 | -311.0 / -310.2 | -312.8 / -309.7 |
| 8192 | -312.6 / -309.6 | -312.7 / -309.5 | -311.9 / -309.8 | -311.9 / -309.8 | -312.2 / -309.0 | -310.5 / -309.4 | -312.5 / -309.3 |
| 16384 | -312.2 / -309.1 | -312.1 / -309.1 | -311.6 / -309.5 | -311.6 / -309.5 | -311.8 / -308.7 | -310.2 / -308.9 | -311.9 / -308.8 |
| 32768 | -311.9 / -308.7 | -311.9 / -308.6 | -311.1 / -309.1 | -311.1 / -309.1 | -311.3 / -308.2 | -309.7 / -308.8 | -311.7 / -308.6 |
| 65536 | -311.5 / -308.2 | -311.4 / -308.2 | -310.8 / -308.8 | -310.8 / -308.8 | -311.0 / -308.0 | -309.6 / -308.4 | -311.2 / -308.0 |
| 131072 | -311.2 / -307.8 | -311.2 / -308.0 | -310.5 / -308.4 | -310.5 / -308.4 | -310.6 / -307.5 | -309.0 / -308.2 | -311.0 / -307.8 |

#### double: planning time, and heap held by the plan

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | KFR | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|---:|
| 256 | 116353 µs, 198.2 KiB | 116331 µs, 5.5 KiB | 3 µs, 4.3 KiB | 1 µs, 4.3 KiB | 10 µs, 2.2 KiB | 9 µs, 4.7 KiB | 7 µs, 12.2 KiB |
| 512 | 177559 µs, 21.7 KiB | 176424 µs, 6.1 KiB | 6 µs, 8.3 KiB | 3 µs, 8.3 KiB | 5 µs, 4.2 KiB | 15 µs, 9.4 KiB | 12 µs, 19.2 KiB |
| 1024 | 257442 µs, 32.0 KiB | 259046 µs, 7.0 KiB | 6 µs, 16.3 KiB | 4 µs, 16.3 KiB | 3 µs, 8.2 KiB | 16 µs, 17.4 KiB | 9 µs, 34.3 KiB |
| 2048 | 337892 µs, 90.8 KiB | 336220 µs, 53.9 KiB | 10 µs, 32.3 KiB | 7 µs, 32.3 KiB | 5 µs, 16.2 KiB | 31 µs, 33.6 KiB | 16 µs, 60.6 KiB |
| 4096 | 433575 µs, 148.8 KiB | 432084 µs, 7.2 KiB | 17 µs, 64.3 KiB | 14 µs, 64.3 KiB | 6 µs, 32.2 KiB | 49 µs, 65.8 KiB | 21 µs, 117.6 KiB |
| 8192 | 573201 µs, 277.6 KiB | 572686 µs, 7.8 KiB | 31 µs, 128.3 KiB | 29 µs, 128.3 KiB | 8 µs, 64.2 KiB | 90 µs, 132.5 KiB | 34 µs, 229.9 KiB |
| 16384 | 825558 µs, 438.7 KiB | 829192 µs, 71.1 KiB | 57 µs, 256.3 KiB | 53 µs, 256.3 KiB | 13 µs, 128.2 KiB | 145 µs, 264.5 KiB | 66 µs, 523.8 KiB |
| 32768 | 1294158 µs, 678.3 KiB | 1295032 µs, 251.5 KiB | 104 µs, 512.3 KiB | 103 µs, 512.3 KiB | 22 µs, 256.2 KiB | 281 µs, 528.5 KiB | 111 µs, 1003.2 KiB |
| 65536 | 2133220 µs, 1884.9 KiB | 2135630 µs, 346.2 KiB | 201 µs, 1024.3 KiB | 205 µs, 1024.3 KiB | 41 µs, 512.2 KiB | 552 µs, 1056.4 KiB | 176 µs, 1884.8 KiB |
| 131072 | 4283357 µs, 3763.4 KiB | 4264059 µs, 2271.8 KiB | 432 µs, 2048.3 KiB | 410 µs, 2048.3 KiB | 80 µs, 1024.3 KiB | 1093 µs, 2112.5 KiB | 330 µs, 2940.7 KiB |

#### double: heap allocations per call, forward (first call, then each) / inverse (first call, then each), with KiB per call

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | KFR | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|---:|
| 256 | 0, 0 / 0, 0 | 0, 0 / 1 (2.0 KiB), 1 (2.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (2.1 KiB), 1 (2.1 KiB) / 1 (2.1 KiB), 1 (2.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 512 | 0, 0 / 0, 0 | 0, 0 / 1 (4.0 KiB), 1 (4.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (4.1 KiB), 1 (4.1 KiB) / 1 (4.1 KiB), 1 (4.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 1024 | 0, 0 / 0, 0 | 0, 0 / 1 (8.0 KiB), 1 (8.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (8.1 KiB), 1 (8.1 KiB) / 1 (8.1 KiB), 1 (8.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 2048 | 0, 0 / 0, 0 | 0, 0 / 1 (16.0 KiB), 1 (16.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (16.1 KiB), 1 (16.1 KiB) / 1 (16.1 KiB), 1 (16.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 4096 | 0, 0 / 0, 0 | 0, 0 / 1 (32.0 KiB), 1 (32.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (32.1 KiB), 1 (32.1 KiB) / 1 (32.1 KiB), 1 (32.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 8192 | 0, 0 / 0, 0 | 0, 0 / 1 (64.0 KiB), 1 (64.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (64.1 KiB), 1 (64.1 KiB) / 1 (64.1 KiB), 1 (64.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 16384 | 0, 0 / 0, 0 | 0, 0 / 1 (128.0 KiB), 1 (128.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (128.1 KiB), 1 (128.1 KiB) / 1 (128.1 KiB), 1 (128.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 32768 | 0, 0 / 0, 0 | 0, 0 / 1 (256.0 KiB), 1 (256.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (256.1 KiB), 1 (256.1 KiB) / 1 (256.1 KiB), 1 (256.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 65536 | 0, 0 / 0, 0 | 0, 0 / 1 (512.0 KiB), 1 (512.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (512.1 KiB), 1 (512.1 KiB) / 1 (512.1 KiB), 1 (512.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |
| 131072 | 0, 0 / 0, 0 | 0, 0 / 1 (1024.0 KiB), 1 (1024.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (1024.1 KiB), 1 (1024.1 KiB) / 1 (1024.1 KiB), 1 (1024.1 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 |

#### double: instruction set in use

| library | SIMD path reported |
|---|---:|
| FFTW | avx2 / avx2; avx+avx2 / avx+avx2; avx2 / avx+avx2; avx2+avx2_128 / avx2 |
| FFTW preserve-input | avx2 / avx2; avx2 / avx+avx2; avx2+avx2_128 / avx+avx2 |
| PFFFT | SSE2 |
| PFFFT unordered | SSE2 |
| pocketfft | none (scalar 1-D code) |
| KFR | avx2 |
| zldsp_fft | AVX2 |

</details>

<details>
<summary>AVX2/FMA build, run 1 of 2</summary>

### Run

- CPU: Intel(R) Core(TM) Ultra 9 185H; running on CPU 2, allowed 1 CPU(s)
- Compiler: GNU 16.2.1
- Flags: `C: -march=x86-64-v3 -O3 -DNDEBUG, C++: -march=x86-64-v3 -O3 -DNDEBUG`; target macros: SSE2 SSE4.2 AVX AVX2 FMA
- Libraries: FFTW fftw-3.3.10-sse2-avx-avx2-avx2_128; FFTW preserve-input fftw-3.3.10-sse2-avx-avx2-avx2_128; PFFFT v1.3.0; PFFFT unordered v1.3.0; pocketfft cpp@4d865cb; zldsp_fft 8e9b4c1;
- Timing: 21 rounds of one batch (at least 5.0 ms) per library, median shown
- Reference: long double radix-2, 64-bit mantissa; against direct summation at n = 16..1024 it differs by at most -365.2 dB

#### float: forward, median ns per transform (ns per sample)

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|
| 256 | 117 (0.455) | 117 (0.456) | 133 (0.518) | 117 (0.458) | 350 (1.368) | 92 (0.359) |
| 512 | 224 (0.438) | 222 (0.433) | 280 (0.547) | 252 (0.492) | 754 (1.473) | 178 (0.348) |
| 1024 | 441 (0.431) | 442 (0.431) | 540 (0.528) | 483 (0.471) | 1494 (1.459) | 403 (0.393) |
| 2048 | 926 (0.452) | 931 (0.455) | 1202 (0.587) | 1071 (0.523) | 3200 (1.563) | 801 (0.391) |
| 4096 | 2047 (0.500) | 2062 (0.503) | 2589 (0.632) | 2361 (0.576) | 6728 (1.642) | 1945 (0.475) |
| 8192 | 4499 (0.549) | 4488 (0.548) | 7294 (0.890) | 6612 (0.807) | 16451 (2.008) | 4570 (0.558) |
| 16384 | 11497 (0.702) | 11614 (0.709) | 16251 (0.992) | 14227 (0.868) | 34458 (2.103) | 10435 (0.637) |
| 32768 | 27162 (0.829) | 24798 (0.757) | 36537 (1.115) | 31585 (0.964) | 71899 (2.194) | 23261 (0.710) |
| 65536 | 53005 (0.809) | 54226 (0.827) | 72871 (1.112) | 63817 (0.974) | 147889 (2.257) | 47180 (0.720) |
| 131072 | 130777 (0.998) | 122337 (0.933) | 158687 (1.211) | 143102 (1.092) | 330401 (2.521) | 106817 (0.815) |

Interquartile range over the median, across 21 rounds: typically 1.1%, at worst 2.6% (FFTW preserve-input, n = 512).

#### float: inverse, median ns per transform (ns per sample)

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|
| 256 | 119 (0.463) | 130 (0.509) | 133 (0.521) | 114 (0.447) | 327 (1.279) | 85 (0.333) |
| 512 | 224 (0.437) | 253 (0.494) | 280 (0.547) | 247 (0.482) | 674 (1.317) | 165 (0.322) |
| 1024 | 449 (0.439) | 499 (0.487) | 567 (0.554) | 481 (0.470) | 1360 (1.328) | 349 (0.341) |
| 2048 | 999 (0.488) | 1056 (0.516) | 1207 (0.589) | 1082 (0.528) | 2810 (1.372) | 749 (0.366) |
| 4096 | 2315 (0.565) | 2365 (0.577) | 2559 (0.625) | 2326 (0.568) | 6231 (1.521) | 1805 (0.441) |
| 8192 | 4855 (0.593) | 4915 (0.600) | 8145 (0.994) | 7061 (0.862) | 15168 (1.852) | 4311 (0.526) |
| 16384 | 12421 (0.758) | 12709 (0.776) | 17781 (1.085) | 15510 (0.947) | 31442 (1.919) | 9670 (0.590) |
| 32768 | 25798 (0.787) | 25977 (0.793) | 39075 (1.192) | 34522 (1.054) | 64071 (1.955) | 22721 (0.693) |
| 65536 | 55273 (0.843) | 56146 (0.857) | 80030 (1.221) | 70632 (1.078) | 129975 (1.983) | 46692 (0.712) |
| 131072 | 140368 (1.071) | 136824 (1.044) | 180543 (1.377) | 162531 (1.240) | 285880 (2.181) | 107594 (0.821) |

Interquartile range over the median, across 21 rounds: typically 0.8%, at worst 6.7% (zldsp_fft, n = 4096).

#### float: error in dB relative to the signal, forward spectrum / round trip

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|
| 256 | -140.3 / -137.1 | -140.3 / -137.1 | -140.3 / -138.1 | -140.3 / -138.1 | -141.0 / -137.7 | -139.8 / -136.1 |
| 512 | -139.8 / -137.1 | -139.8 / -137.1 | -139.5 / -137.1 | -139.5 / -137.1 | -140.4 / -137.2 | -139.2 / -136.1 |
| 1024 | -139.4 / -136.1 | -139.4 / -136.0 | -138.7 / -136.7 | -138.7 / -136.7 | -139.3 / -136.5 | -138.6 / -135.5 |
| 2048 | -138.5 / -135.4 | -138.5 / -135.5 | -138.5 / -136.4 | -138.5 / -136.4 | -139.6 / -136.3 | -138.3 / -135.4 |
| 4096 | -138.3 / -134.9 | -138.1 / -134.8 | -138.0 / -135.8 | -138.0 / -135.8 | -138.8 / -135.6 | -137.9 / -134.8 |
| 8192 | -137.7 / -134.4 | -137.7 / -134.4 | -137.6 / -135.4 | -137.6 / -135.4 | -138.3 / -135.1 | -137.7 / -134.6 |
| 16384 | -137.5 / -134.2 | -137.4 / -134.1 | -137.2 / -135.1 | -137.2 / -135.1 | -138.0 / -134.8 | -137.2 / -134.1 |
| 32768 | -137.1 / -133.9 | -137.0 / -133.9 | -136.7 / -134.6 | -136.7 / -134.6 | -137.5 / -134.5 | -136.7 / -133.5 |
| 65536 | -136.8 / -133.6 | -136.5 / -133.4 | -136.5 / -134.3 | -136.5 / -134.3 | -137.3 / -134.2 | -136.6 / -133.4 |
| 131072 | -136.2 / -133.0 | -136.2 / -133.1 | -136.1 / -134.0 | -136.1 / -134.0 | -137.0 / -133.9 | -136.1 / -132.9 |

#### float: planning time, and heap held by the plan

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|
| 256 | 100713 µs, 192.5 KiB | 100212 µs, 4.6 KiB | 13 µs, 2.3 KiB | 1 µs, 2.3 KiB | 7 µs, 1.2 KiB | 13 µs, 7.9 KiB |
| 512 | 151524 µs, 11.0 KiB | 152265 µs, 5.1 KiB | 3 µs, 4.3 KiB | 1 µs, 4.3 KiB | 4 µs, 2.3 KiB | 6 µs, 12.5 KiB |
| 1024 | 230434 µs, 19.0 KiB | 228168 µs, 9.7 KiB | 5 µs, 8.3 KiB | 2 µs, 8.3 KiB | 4 µs, 4.3 KiB | 10 µs, 19.6 KiB |
| 2048 | 297745 µs, 48.7 KiB | 297815 µs, 7.2 KiB | 6 µs, 16.3 KiB | 3 µs, 16.3 KiB | 6 µs, 8.3 KiB | 11 µs, 32.8 KiB |
| 4096 | 376570 µs, 77.3 KiB | 376910 µs, 23.0 KiB | 8 µs, 32.3 KiB | 7 µs, 32.3 KiB | 5 µs, 16.3 KiB | 13 µs, 61.8 KiB |
| 8192 | 475415 µs, 87.2 KiB | 475387 µs, 8.7 KiB | 14 µs, 64.3 KiB | 12 µs, 64.3 KiB | 7 µs, 32.3 KiB | 21 µs, 117.2 KiB |
| 16384 | 644598 µs, 216.0 KiB | 640970 µs, 72.5 KiB | 27 µs, 128.3 KiB | 24 µs, 128.3 KiB | 13 µs, 64.3 KiB | 48 µs, 229.0 KiB |
| 32768 | 951428 µs, 551.8 KiB | 951935 µs, 138.2 KiB | 49 µs, 256.3 KiB | 48 µs, 256.3 KiB | 22 µs, 128.3 KiB | 54 µs, 511.8 KiB |
| 65536 | 1476260 µs, 680.4 KiB | 1467394 µs, 11.7 KiB | 89 µs, 512.3 KiB | 92 µs, 512.3 KiB | 39 µs, 256.3 KiB | 95 µs, 992.0 KiB |
| 131072 | 2806185 µs, 1865.1 KiB | 2801063 µs, 715.9 KiB | 184 µs, 1024.3 KiB | 188 µs, 1024.3 KiB | 74 µs, 512.4 KiB | 147 µs, 1866.6 KiB |

#### float: heap allocations per call, forward (first call, then each) / inverse (first call, then each), with KiB per call

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|
| 256 | 0, 0 / 0, 0 | 0, 0 / 1 (1.0 KiB), 1 (1.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (1.1 KiB), 1 (1.1 KiB) / 1 (1.1 KiB), 1 (1.1 KiB) | 0, 0 / 0, 0 |
| 512 | 0, 0 / 0, 0 | 0, 0 / 1 (2.0 KiB), 1 (2.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (2.1 KiB), 1 (2.1 KiB) / 1 (2.1 KiB), 1 (2.1 KiB) | 0, 0 / 0, 0 |
| 1024 | 0, 0 / 0, 0 | 0, 0 / 1 (4.0 KiB), 1 (4.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (4.1 KiB), 1 (4.1 KiB) / 1 (4.1 KiB), 1 (4.1 KiB) | 0, 0 / 0, 0 |
| 2048 | 0, 0 / 0, 0 | 0, 0 / 1 (8.0 KiB), 1 (8.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (8.1 KiB), 1 (8.1 KiB) / 1 (8.1 KiB), 1 (8.1 KiB) | 0, 0 / 0, 0 |
| 4096 | 0, 0 / 0, 0 | 0, 0 / 1 (16.0 KiB), 1 (16.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (16.1 KiB), 1 (16.1 KiB) / 1 (16.1 KiB), 1 (16.1 KiB) | 0, 0 / 0, 0 |
| 8192 | 0, 0 / 0, 0 | 0, 0 / 1 (32.0 KiB), 1 (32.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (32.1 KiB), 1 (32.1 KiB) / 1 (32.1 KiB), 1 (32.1 KiB) | 0, 0 / 0, 0 |
| 16384 | 0, 0 / 0, 0 | 0, 0 / 1 (64.0 KiB), 1 (64.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (64.1 KiB), 1 (64.1 KiB) / 1 (64.1 KiB), 1 (64.1 KiB) | 0, 0 / 0, 0 |
| 32768 | 0, 0 / 0, 0 | 0, 0 / 1 (128.0 KiB), 1 (128.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (128.1 KiB), 1 (128.1 KiB) / 1 (128.1 KiB), 1 (128.1 KiB) | 0, 0 / 0, 0 |
| 65536 | 0, 0 / 0, 0 | 0, 0 / 1 (256.0 KiB), 1 (256.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (256.1 KiB), 1 (256.1 KiB) / 1 (256.1 KiB), 1 (256.1 KiB) | 0, 0 / 0, 0 |
| 131072 | 0, 0 / 0, 0 | 0, 0 / 1 (512.0 KiB), 1 (512.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (512.1 KiB), 1 (512.1 KiB) / 1 (512.1 KiB), 1 (512.1 KiB) | 0, 0 / 0, 0 |

#### float: instruction set in use

| library | SIMD path reported |
|---|---:|
| FFTW | avx2+avx2_128 / avx2+avx2_128; avx2+avx2_128 / avx+avx2+avx2_128; avx+avx2_128 / avx+avx2+avx2_128 |
| FFTW preserve-input | avx2+avx2_128 / avx2+avx2_128; avx2+avx2_128 / avx+avx2+avx2_128; avx+avx2+avx2_128 / avx+avx2+avx2_128; avx+avx2+avx2_128 / avx2+avx2_128 |
| PFFFT | SSE1 |
| PFFFT unordered | SSE1 |
| pocketfft | none (scalar 1-D code) |
| zldsp_fft | AVX2 |

#### double: forward, median ns per transform (ns per sample)

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|
| 256 | 149 (0.581) | 153 (0.597) | 169 (0.659) | 130 (0.510) | 432 (1.686) | 171 (0.670) |
| 512 | 305 (0.596) | 307 (0.599) | 365 (0.713) | 275 (0.537) | 966 (1.887) | 349 (0.682) |
| 1024 | 708 (0.691) | 683 (0.667) | 716 (0.699) | 540 (0.528) | 1968 (1.922) | 776 (0.758) |
| 2048 | 1553 (0.758) | 1539 (0.751) | 1804 (0.881) | 1461 (0.713) | 4462 (2.178) | 1716 (0.838) |
| 4096 | 3419 (0.835) | 3377 (0.824) | 5393 (1.317) | 4519 (1.103) | 11097 (2.709) | 4373 (1.068) |
| 8192 | 8084 (0.987) | 8255 (1.008) | 13611 (1.662) | 11772 (1.437) | 26710 (3.261) | 9814 (1.198) |
| 16384 | 17618 (1.075) | 17498 (1.068) | 28175 (1.720) | 24588 (1.501) | 55557 (3.391) | 22399 (1.367) |
| 32768 | 41249 (1.259) | 41162 (1.256) | 60907 (1.859) | 54520 (1.664) | 117045 (3.572) | 47271 (1.443) |
| 65536 | 88639 (1.353) | 90938 (1.388) | 130319 (1.989) | 114942 (1.754) | 246138 (3.756) | 106169 (1.620) |
| 131072 | 221847 (1.693) | 221903 (1.693) | 356004 (2.716) | 312140 (2.381) | 595111 (4.540) | 235102 (1.794) |

Interquartile range over the median, across 21 rounds: typically 1.1%, at worst 4.5% (zldsp_fft, n = 131072).

#### double: inverse, median ns per transform (ns per sample)

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|
| 256 | 169 (0.658) | 198 (0.775) | 182 (0.711) | 136 (0.532) | 401 (1.568) | 157 (0.612) |
| 512 | 337 (0.659) | 373 (0.728) | 389 (0.760) | 278 (0.542) | 831 (1.623) | 316 (0.617) |
| 1024 | 709 (0.692) | 786 (0.768) | 762 (0.744) | 539 (0.526) | 1671 (1.632) | 696 (0.680) |
| 2048 | 1709 (0.835) | 1838 (0.897) | 1701 (0.831) | 1513 (0.739) | 3679 (1.796) | 1525 (0.745) |
| 4096 | 3767 (0.920) | 3905 (0.953) | 5975 (1.459) | 4977 (1.215) | 9695 (2.367) | 3814 (0.931) |
| 8192 | 9982 (1.218) | 9723 (1.187) | 15452 (1.886) | 13489 (1.647) | 23746 (2.899) | 9156 (1.118) |
| 16384 | 20573 (1.256) | 20695 (1.263) | 30223 (1.845) | 26728 (1.631) | 46042 (2.810) | 20940 (1.278) |
| 32768 | 44679 (1.363) | 45693 (1.394) | 70982 (2.166) | 61440 (1.875) | 98279 (2.999) | 44357 (1.354) |
| 65536 | 106291 (1.622) | 103750 (1.583) | 143507 (2.190) | 126606 (1.932) | 197326 (3.011) | 104575 (1.596) |
| 131072 | 256771 (1.959) | 245613 (1.874) | 392455 (2.994) | 343642 (2.622) | 526369 (4.016) | 233225 (1.779) |

Interquartile range over the median, across 21 rounds: typically 0.8%, at worst 4.4% (zldsp_fft, n = 4096).

#### double: error in dB relative to the signal, forward spectrum / round trip

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|
| 256 | -315.8 / -312.0 | -315.1 / -311.8 | -315.2 / -313.2 | -315.2 / -313.2 | -316.8 / -313.1 | -315.0 / -311.4 |
| 512 | -315.2 / -311.9 | -315.2 / -311.9 | -314.9 / -312.7 | -314.9 / -312.7 | -315.0 / -311.5 | -313.8 / -310.7 |
| 1024 | -314.0 / -311.0 | -313.8 / -311.0 | -313.5 / -311.8 | -313.5 / -311.8 | -314.3 / -311.3 | -313.6 / -310.2 |
| 2048 | -313.6 / -310.5 | -313.6 / -310.4 | -313.2 / -311.3 | -313.2 / -311.3 | -313.8 / -311.0 | -313.4 / -310.2 |
| 4096 | -313.0 / -310.0 | -313.2 / -310.1 | -312.8 / -310.8 | -312.8 / -310.8 | -313.3 / -310.1 | -312.8 / -309.7 |
| 8192 | -312.7 / -309.7 | -312.6 / -309.5 | -312.2 / -310.3 | -312.2 / -310.3 | -312.8 / -309.9 | -312.5 / -309.3 |
| 16384 | -312.2 / -309.0 | -312.2 / -309.1 | -312.0 / -310.0 | -312.0 / -310.0 | -312.3 / -309.2 | -311.9 / -308.8 |
| 32768 | -312.0 / -308.7 | -311.9 / -308.6 | -311.4 / -309.5 | -311.4 / -309.5 | -311.9 / -308.7 | -311.7 / -308.6 |
| 65536 | -311.5 / -308.1 | -311.5 / -308.2 | -311.2 / -309.2 | -311.2 / -309.2 | -311.7 / -308.7 | -311.2 / -308.0 |
| 131072 | -311.2 / -307.8 | -311.0 / -307.6 | -310.8 / -308.8 | -310.8 / -308.8 | -311.2 / -308.3 | -311.0 / -307.8 |

#### double: planning time, and heap held by the plan

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|
| 256 | 116242 µs, 198.2 KiB | 115440 µs, 6.8 KiB | 5 µs, 4.3 KiB | 1 µs, 4.3 KiB | 18 µs, 2.2 KiB | 11 µs, 12.2 KiB |
| 512 | 175565 µs, 21.7 KiB | 175464 µs, 6.1 KiB | 5 µs, 8.3 KiB | 2 µs, 8.3 KiB | 4 µs, 4.2 KiB | 16 µs, 19.2 KiB |
| 1024 | 257911 µs, 32.0 KiB | 257738 µs, 7.0 KiB | 6 µs, 16.3 KiB | 4 µs, 16.3 KiB | 3 µs, 8.2 KiB | 9 µs, 34.3 KiB |
| 2048 | 336699 µs, 76.8 KiB | 337050 µs, 6.5 KiB | 9 µs, 32.3 KiB | 7 µs, 32.3 KiB | 4 µs, 16.2 KiB | 15 µs, 60.6 KiB |
| 4096 | 433046 µs, 148.8 KiB | 431913 µs, 7.2 KiB | 16 µs, 64.3 KiB | 12 µs, 64.3 KiB | 6 µs, 32.2 KiB | 27 µs, 117.6 KiB |
| 8192 | 574357 µs, 309.3 KiB | 572040 µs, 142.3 KiB | 26 µs, 128.3 KiB | 23 µs, 128.3 KiB | 8 µs, 64.2 KiB | 33 µs, 229.9 KiB |
| 16384 | 830797 µs, 500.0 KiB | 829808 µs, 41.1 KiB | 51 µs, 256.3 KiB | 47 µs, 256.3 KiB | 10 µs, 128.2 KiB | 50 µs, 523.8 KiB |
| 32768 | 1302624 µs, 948.7 KiB | 1303090 µs, 11.5 KiB | 97 µs, 512.3 KiB | 90 µs, 512.3 KiB | 17 µs, 256.2 KiB | 105 µs, 1003.2 KiB |
| 65536 | 2140840 µs, 1925.2 KiB | 2175039 µs, 290.2 KiB | 200 µs, 1024.3 KiB | 185 µs, 1024.3 KiB | 32 µs, 512.2 KiB | 196 µs, 1884.8 KiB |
| 131072 | 4312193 µs, 2150.6 KiB | 4270905 µs, 1960.3 KiB | 367 µs, 2048.3 KiB | 339 µs, 2048.3 KiB | 85 µs, 1024.3 KiB | 219 µs, 2940.7 KiB |

#### double: heap allocations per call, forward (first call, then each) / inverse (first call, then each), with KiB per call

| n | FFTW | FFTW preserve-input | PFFFT | PFFFT unordered | pocketfft | zldsp_fft |
|---|---:|---:|---:|---:|---:|---:|
| 256 | 0, 0 / 0, 0 | 0, 0 / 1 (2.0 KiB), 1 (2.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (2.1 KiB), 1 (2.1 KiB) / 1 (2.1 KiB), 1 (2.1 KiB) | 0, 0 / 0, 0 |
| 512 | 0, 0 / 0, 0 | 0, 0 / 1 (4.0 KiB), 1 (4.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (4.1 KiB), 1 (4.1 KiB) / 1 (4.1 KiB), 1 (4.1 KiB) | 0, 0 / 0, 0 |
| 1024 | 0, 0 / 0, 0 | 0, 0 / 1 (8.0 KiB), 1 (8.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (8.1 KiB), 1 (8.1 KiB) / 1 (8.1 KiB), 1 (8.1 KiB) | 0, 0 / 0, 0 |
| 2048 | 0, 0 / 0, 0 | 0, 0 / 1 (16.0 KiB), 1 (16.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (16.1 KiB), 1 (16.1 KiB) / 1 (16.1 KiB), 1 (16.1 KiB) | 0, 0 / 0, 0 |
| 4096 | 0, 0 / 0, 0 | 0, 0 / 1 (32.0 KiB), 1 (32.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (32.1 KiB), 1 (32.1 KiB) / 1 (32.1 KiB), 1 (32.1 KiB) | 0, 0 / 0, 0 |
| 8192 | 0, 0 / 0, 0 | 0, 0 / 1 (64.0 KiB), 1 (64.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (64.1 KiB), 1 (64.1 KiB) / 1 (64.1 KiB), 1 (64.1 KiB) | 0, 0 / 0, 0 |
| 16384 | 0, 0 / 0, 0 | 0, 0 / 1 (128.0 KiB), 1 (128.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (128.1 KiB), 1 (128.1 KiB) / 1 (128.1 KiB), 1 (128.1 KiB) | 0, 0 / 0, 0 |
| 32768 | 0, 0 / 0, 0 | 0, 0 / 1 (256.0 KiB), 1 (256.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (256.1 KiB), 1 (256.1 KiB) / 1 (256.1 KiB), 1 (256.1 KiB) | 0, 0 / 0, 0 |
| 65536 | 0, 0 / 0, 0 | 0, 0 / 1 (512.0 KiB), 1 (512.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (512.1 KiB), 1 (512.1 KiB) / 1 (512.1 KiB), 1 (512.1 KiB) | 0, 0 / 0, 0 |
| 131072 | 0, 0 / 0, 0 | 0, 0 / 1 (1024.0 KiB), 1 (1024.0 KiB) | 0, 0 / 0, 0 | 0, 0 / 0, 0 | 1 (1024.1 KiB), 1 (1024.1 KiB) / 1 (1024.1 KiB), 1 (1024.1 KiB) | 0, 0 / 0, 0 |

#### double: instruction set in use

| library | SIMD path reported |
|---|---:|
| FFTW | avx2 / avx+avx2; avx2 / avx2; avx+avx2 / avx2; avx2+avx2_128 / avx+avx2 |
| FFTW preserve-input | avx2 / avx2; avx+avx2 / avx2; avx2 / avx+avx2; avx2+avx2_128 / avx2 |
| PFFFT | AVX |
| PFFFT unordered | AVX |
| pocketfft | none (scalar 1-D code) |
| zldsp_fft | AVX2 |

</details>
