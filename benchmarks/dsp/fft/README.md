# Real-input FFT comparison

`kalinka-fft-bench` compares the FFT libraries the renderer's future convolver
could be built on: uniformly partitioned overlap-save needs one real forward
and one real inverse transform per block, at 512 to 8192 points. It is a
standalone project, not part of the renderer build. The measured numbers are in
[results.md](results.md).

For every power of two from 256 to 131072 points, in float and in double, it
reports:

- **speed**: median ns per forward and per inverse transform, and ns per
  input sample. Plans are made once, outside the timing; each library is timed
  in batches of at least 5 ms, one batch per library per round over 21 rounds,
  on warm buffers and one thread.
- **accuracy**: the RMS error of the forward spectrum relative to the RMS of
  the reference spectrum, and of a forward-then-inverse round trip relative to
  the input, both in dB. The reference is a radix-2 FFT in long double
  (`src/ReferenceFft.cpp`), itself checked against direct summation at the
  start of every run. The input is uniform noise from a fixed seed, rounded to
  the precision under test before the reference sees it.
- **heap use**: what planning leaves allocated, and how many allocations the
  first and the following execute calls make. The program defines `malloc` and
  its relatives itself (`src/HeapProbe.cpp`, glibc only), so this covers the
  shared libraries and `operator new` as well.
- **the instruction set each library ended up using**, as the library reports
  it.

| Library | Fetched as | Adapter |
|---|---|---|
| FFTW 3 | the system library (`libfftw3-dev`, `fftw-devel`) | `src/FftwFft.cpp` |
| PFFFT (marton78 fork) | tag v1.3.0 | `src/PffftFft.cpp` |
| pocketfft, C++ header | `cpp` branch at 4d865cb | `src/PocketFft.cpp` |
| KFR | tag 7.2.0 | `src/KfrFft.cpp` |
| zldsp_fft, on Google Highway 1.4.0 | commit 8e9b4c1 | `src/ZldspFft.cpp` |

Every fetched archive is pinned by URL and SHA-256 in `CMakeLists.txt`.
`-DFFT_BENCH_WITH_FFTW=OFF`, `-DFFT_BENCH_WITH_KFR=OFF` and
`-DFFT_BENCH_WITH_ZLDSP=OFF` leave a library out. The configure step needs
network access.

## Running it

The output is Markdown on stdout; progress goes to stderr.

```
kalinka-fft-bench [--min N] [--max N] [--rounds R] [--batch-ms MS] [--accuracy-only]
```

`--accuracy-only` skips all timing, for emulators and sanitizer builds, where
times mean nothing.

Close everything else, keep the machine on mains power, and pin the process to
one core. On a hybrid Intel CPU that must be a performance core: `lscpu -e`
lists them as the CPUs with the highest MAXMHZ. Run it two or three times; the
tables state the spread between rounds, but not between runs.

### Raspberry Pi 4 / 5, Raspberry Pi OS or Debian 13 (trixie), arm64

```
sudo apt install build-essential cmake ninja-build libfftw3-dev ca-certificates
cmake -S benchmarks/dsp/fft -B build/fft-bench -G Ninja
cmake --build build/fft-bench -j2
taskset -c 3 build/fft-bench/kalinka-fft-bench > fft-pi.md
```

The project needs CMake 3.25 or newer and builds on Debian 13 (CMake 3.31,
GCC 14.2) and Ubuntu 24.04 (CMake 3.28, GCC 13.2), both newer than the GCC 11
KFR 7.2 asks for. It has not been built on either yet. KFR builds one DFT
library per instruction set only on x86, so on
arm64 it compiles 11 files rather than 55. On x86, single GCC processes
compiling them reached several GB (see below). A Pi with 4 GB may not build
KFR at all: try `-j1`, and if the compiler is killed, configure with
`-DFFT_BENCH_WITH_KFR=OFF`.

On the Pi, check that it did not throttle during the run:
`vcgencmd get_throttled` should print `throttled=0x0`. A Pi 4 without a heatsink
will throttle.

Debian's `libfftw3-double3` is built without NEON on arm64 (only the single
precision library has `--enable-neon`), so FFTW's double numbers there are
scalar code. Fedora's aarch64 build enables NEON for both.

### x86_64, Fedora or Debian

```
sudo dnf install gcc-c++ cmake ninja-build fftw-devel      # Fedora
sudo apt install build-essential cmake ninja-build libfftw3-dev   # Debian
```

Distribution packages are built for the baseline x86-64 instruction set, so the
build that shows what a package would ship uses no `-march` at all:

```
cmake -S benchmarks/dsp/fft -B build/fft-generic -G Ninja
cmake --build build/fft-generic -j2
taskset -c 2 build/fft-generic/kalinka-fft-bench > fft-generic.md
```

FFTW, KFR and zldsp_fft choose their code at run time, so they use AVX2 even
there. PFFFT and pocketfft use what the compiler is allowed, so a second build
shows what they would do with AVX2 and FMA. KFR would run the same code in it,
so it can be left out:

```
cmake -S benchmarks/dsp/fft -B build/fft-avx2 -G Ninja -DFFT_BENCH_WITH_KFR=OFF \
  -DCMAKE_C_FLAGS=-march=x86-64-v3 -DCMAKE_CXX_FLAGS=-march=x86-64-v3
cmake --build build/fft-avx2 -j4
taskset -c 2 build/fft-avx2/kalinka-fft-bench > fft-avx2.md
```

FFTW's x86 SIMD differs by distribution: Fedora builds it with SSE2, AVX and
AVX2 codelets, Debian with SSE/SSE2 and AVX but not AVX2.

KFR's multi-architecture build compiles its DFT five times (SSE2, SSE4.1, AVX,
AVX2, AVX-512) and is by far the heaviest part of the build. With GCC 16 at
`-j2` it took 270 s and peaked at 7457 MiB. One compiler process alone reached
6.9 GB while the AVX-512 variants were compiling. Everything else builds at
`-j4` in about 1.3 GB.

#### Without root, on Fedora

When FFTW's runtime libraries are installed but `fftw-devel` is not, the
headers can come from the package without installing it:

```
mkdir fftw-devel && cd fftw-devel
dnf download --arch=x86_64 fftw-devel
rpm2cpio fftw-devel-*.rpm | cpio -idm
ln -sf /usr/lib64/libfftw3f.so.3 usr/lib64/libfftw3f.so
ln -sf /usr/lib64/libfftw3.so.3 usr/lib64/libfftw3.so
cmake -S benchmarks/dsp/fft -B build/fft-generic -G Ninja -DCMAKE_PREFIX_PATH=$PWD/usr
```
