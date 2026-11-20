# Reproduce the DSD-to-PCM measurements

From a full KalinkaPlayer Git clone, run the following command on Linux. It builds the actual release converter, renders the committed source file with Kalinka and FFmpeg, runs the release’s measurement routines on the local CPU, and writes CSVs, SVGs and PNGs to the directory you choose.

```sh
python3 benchmarks/dsp/decoder-comparison/reproduce.py --output ./dsd-results
```

Install Git, a C++23-capable g++ (GCC 14 or later), the Highway and GoogleTest development packages, FFmpeg, rsvg-convert for the pipeline PNG, and Python 3.12+ with NumPy, SciPy and Matplotlib. On Debian 13 the packages include `g++ libhwy-dev libgtest-dev ffmpeg librsvg2-bin python3-numpy python3-scipy python3-matplotlib`. If the measurement scripts came from the article ZIP, pass `--repo /path/to/KalinkaPlayer`. Set `CPPFLAGS` and `LDFLAGS` if Highway is installed in a nonstandard location.

## Sources and outputs

| What is measured | Source | Output |
| --- | --- | --- |
| One input shared by Kalinka and FFmpeg | [dsd64.dsf](dsd64.dsf), mono 2.8224 MHz DSD, SHA-256 `e21d0b464515f7a1a3b73ad3051900896e88a783d079cdc4c83e6d43cff1a5f0` | Four 176.4 kHz float64 WAVs and `fixture-results.csv` |
| Filter stages, frequency and impulse response | [Release filter coefficients](https://github.com/Kalinka-Player/KalinkaPlayer/blob/a043989071d8bbe391ba9ae3372dbedf91a8a705/packages/kalinka-renderer/src/native_player/dsp/dsd/DsdFilterTaps.h) and [release impulse capture](https://github.com/Kalinka-Player/KalinkaPlayer/blob/a043989071d8bbe391ba9ae3372dbedf91a8a705/packages/kalinka-renderer/tests/native_player/dsp/dsd/DsdToPcm_test.cpp#L62-L81) | `chains.csv`, `impulse.csv`, `filter-results.csv` and figures |
| This machine’s DSD64/128/256 input and output THD+N | [Release tone test](https://github.com/Kalinka-Player/KalinkaPlayer/blob/a043989071d8bbe391ba9ae3372dbedf91a8a705/packages/kalinka-renderer/tests/native_player/dsp/dsd/DsdToPcm_test.cpp#L290-L359) | `platform-tone.csv` |
| Stereo CPU load, optionally | [Original timing implementation](https://github.com/Kalinka-Player/KalinkaPlayer/blob/57f1ae92ce45c0e5da90ca5a5ece9b26ae1b4447/packages/kalinka-renderer/bench/DsdToPcm.cpp) | Raw tables per run and `timing.csv` |
| Level-matched programme spectra, optionally | User-supplied BitPerfect DSD original and optional historical Saracon render | `music-alignment.txt`, `music-spectrum.csv`, `music-bands.csv` and a figure |

The runner extracts the tagged renderer source from Git at `a043989071d8bbe391ba9ae3372dbedf91a8a705` (Kalinka 5.20.0, renderer 0.16.0). The local checkout stays in place. The operating system, CPU, compiler flags, renderer revision, FFmpeg build and input hashes are recorded in `provenance.json`. The published common-input comparison used an Intel Core Ultra 9 185H, FFmpeg 9.0.2, GCC 16.2.1, Highway 1.3.0, NumPy 2.5.4 and SciPy 1.18.1.

## Signal and analysis

The fixed synthetic DSF contains 16 isolated bit changes at successive decimation phases on an idle background, followed by a half-second, 1 kHz tone at half modulation. [make_fixture.cpp](make_fixture.cpp) uses the release’s seventh-order modulator and DSF writer. Add `--regenerate-fixture` to write a second copy made on your CPU. ARM and Intel can generate slightly different DSD bits. All decoder comparisons always read the committed bytes.

[render.cpp](render.cpp) converts with the release’s 30 kHz, 50 kHz and Wide presets at 0 dB. The equivalent FFmpeg invocation is:

```sh
ffmpeg -i dsd64.dsf -af aresample=176400:resampler=swr:osf=dbl:dither_method=none -c:a pcm_f64le ffmpeg.wav
```

FFmpeg first decodes to float PCM at 352.8 kHz. Its standard swr filter then produces 176.4 kHz. Decoder internal precision and filter design are implementation choices. The four final files use float64 storage and no added dither. For each file [measure.py](measure.py) aligns to the first bit marker, combines all phases, and interpolates the first downward −3 dB crossing from a DC-normalised 262,144-point FFT. It fits a 1 kHz sine plus DC in the central 250 ms and integrates the unweighted 20 Hz–20 kHz residual with the same power-normalised seven-term Blackman–Harris window. THD+N is relative to the fitted tone.

The per-platform release test uses a separate, locally generated quarter-second tone. [release_probe.cpp](release_probe.cpp) calls the test’s own impulse and tone helpers. [figures.py](figures.py) computes passband deviation and folded-band rejection from the captured samples and draws the article figures directly from the CSVs. Replot without rebuilding using `python3 benchmarks/dsp/decoder-comparison/figures.py ./dsd-results` and add `--music` for an existing music capture.

## Matched CPU and tone comparison on Raspberry Pi

On the target Pi, install the dependencies above plus Debian’s `gcc pkg-config libavcodec-dev libavutil-dev libswresample-dev libsamplerate0-dev libsoxr-dev`. The driver builds Kalinka 5.20.0, MPD 0.24.4 and Squeezelite’s 2.0.0-1517 copy of dsd2pcm from pinned source revisions. It fetches the two upstream repositories if needed. To use existing clones, pass `--mpd-repo /path/to/MPD --squeezelite-repo /path/to/squeezelite`.

```sh
python3 benchmarks/dsp/decoder-comparison/compare_pi.py --output ./pi-results
```

The [timing harness](decoder_cpu.cpp) duplicates the mono DSF to stereo and pre-packs the same MSB-first bytes for all five paths. Each call supplies 2,048 DSD bytes per channel. All paths target 176.4 kHz at unity gain and deliver interleaved float64 for analysis. The timed work includes the following operations.

| Path | Conversion from DSD64 |
| --- | --- |
| Kalinka 30 kHz and Wide | Each preset runs the release converter and interleaves its float64 output. |
| FFmpeg 7.1.5 | libavcodec decodes to planar float32 at 352.8 kHz. The standard swr resampler produces float64 at 176.4 kHz. |
| MPD 0.24.4 | MPD’s floating-point Dsd2Pcm engine feeds libsamplerate 0.2.2 at ratio 0.5 using SRC_SINC_FASTEST (MPD’s default type 2 for this library). The harness widens the resulting float32 PCM. |
| Squeezelite 2.0.0-1517 | Upstream dsd2pcm runs once per channel. The harness follows Squeezelite’s signed 32-bit clipping and packing, then calls libsoxr 0.1.3 using interleaved signed 32-bit input and output. The HQ linear-phase recipe has flags 0 and unity gain, equivalent to `-r 176400 -u hL:0:0` in PCM mode on a resampling-enabled build. The final integers are normalised to float64. Squeezelite normally applies 1 dB of attenuation when resampling. |

The three upstream DSD engines specify the same 48 coefficients mirrored into a 96-tap filter. Squeezelite and MPD build float32 lookup tables. FFmpeg builds double-precision tables. The driver’s separate `native` pass captures the first channel immediately after 8:1 DSD decimation. It compares every float32 word from the first sample onward at 352.8 kHz. `native.f32` holds little-endian triples in Squeezelite, FFmpeg, MPD order. A second pass captures each complete 176.4 kHz path and checks that both copies of the channel agree. The shared [analyser](measure.py) measures cutoff and the central 250 ms of the tone, clear of start and end transients. [Native-rate comparisons](pi-results/native-comparison.csv) · [Squeezelite’s dsd2pcm](https://github.com/ralph-irving/squeezelite/blob/262994a989dc29ce3be4390c57c6a43373dfdca2/dsd2pcm/dsd2pcm.c) · [Integer packing](https://github.com/ralph-irving/squeezelite/blob/262994a989dc29ce3be4390c57c6a43373dfdca2/dsd.c) · [Squeezelite resampling](https://github.com/ralph-irving/squeezelite/blob/262994a989dc29ce3be4390c57c6a43373dfdca2/resample.c) · [FFmpeg’s DSD filter](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/libavcodec/dsd.c) · [MPD’s DSD filter](https://github.com/MusicPlayerDaemon/MPD/blob/72077d76f11ffba21f6705fe912f47b0b5dc1d6e/src/pcm/Dsd2Pcm.cxx) · [MPD resampler settings](https://github.com/MusicPlayerDaemon/MPD/blob/72077d76f11ffba21f6705fe912f47b0b5dc1d6e/src/pcm/LibsamplerateResampler.cxx)

The driver pins the process to logical core 2 (change with `--cpu`) and selects one FFmpeg decoder thread. Each trial warms up 512 blocks, then times 4,096 blocks, equivalent to 23.777 seconds of stereo input. Ten balanced rounds put every path in each position twice, and every ordered pair of paths occurs twice as neighbours within a round. CPU use is 100 × process CPU seconds / seconds of PCM actually emitted. The frame count is recorded for every trial because libsoxr buffers batches of output. File parsing, file I/O, playback services, FLAC encoding and the DAC sit outside the timer. [Individual trials](pi-results/cpu-runs.csv) · [Medians, ranges and tone measurements](pi-results/comparison.csv) · [Board conditions and library versions](pi-results/environment.json)

To repeat the analysis of a local run, use `compare_pi.py --analyse-only --output ./pi-results` with its saved `cpu-runs.csv`, `native.f32` and five stereo `.f64` captures. The mono WAVs also work directly with `python3 measure.py ./pi-results/*.wav`. The driver plots Kalinka 30 kHz, Kalinka Wide, MPD, FFmpeg and Squeezelite from the five measured WAVs. The overview covers 20–88.2 kHz. The detail panel enlarges the upper band around the −3 dB crossings. Both panels use the same gain traces, exported in `resampler-response.csv`. To redraw it from the downloadable WAVs and `comparison.csv`, run `python3 figures.py ./pi-results --pi`.

## Historical Kalinka CPU timings on Pi 4, Pi 5 or Intel

Add `--timing-runs 10` on Pi 4 or `--timing-runs 3` on Pi 5 and Intel. Run on the target machine with playback stopped and a stable clock. The runner builds the channel-pair benchmark from the revision that recorded the original table, runs all its kernels and reports the median of the channel-pair column across launches. A run on a different machine or at another clock gives a new timing. [Original measurements](https://github.com/Kalinka-Player/KalinkaPlayer/blob/57f1ae92ce45c0e5da90ca5a5ece9b26ae1b4447/benchmarks/dsp/results.md) · [Clock, thermal and run conditions](../RUNBOOK.md)

## Compare the recorded music

Download *Looking For A Home* from the two Internet Archive captures linked below. Extract the named audio files in the roots of the ZIPs. The music is by Keith Greeninger and Dayan Kai, courtesy of Blue Coast Records. BitPerfect identifies the AIFF as a Weiss Saracon conversion. [Publisher’s sample notes](https://dsdmaster.blogspot.com/p/blog-page.html) · [Original DSD64](https://web.archive.org/web/20161224072819id_/http://www.bitperfectsound.com/public_files/Looking%20For%20A%20Home%20-%202.8MHz-DSD.dsf.zip) · [Saracon 24/176.4 AIFF](https://web.archive.org/web/20161224125910id_/http://www.bitperfectsound.com/public_files/Looking%20For%20A%20Home%20-%2024-176.4%20PCM%20(Saracon).aiff.zip)

```sh
python3 benchmarks/dsp/decoder-comparison/reproduce.py --output ./music-results \
  --music-dsf 'Looking For A Home - 2.8MHz-DSD.dsf' \
  --saracon-aiff 'Looking For A Home - 24-176.4 PCM (Saracon).aiff'
```

Omit `--saracon-aiff` for a two-decoder music comparison. Allow about 2 GB of working space with the recordings. The runner checks SHA-256 of each uncompressed source:

- DSD64 DSF — `b28e53003e8d8aaad8eb7ddb027a9aa051c963840a076b8330094e7e9a7881bb`
- Saracon AIFF — `940dbd1fa692664f0d42231202bea2b04271d16afc9372a1f8f7a5545ed7b008`

[music_measure.py](music_measure.py) correlates both channels at 10, 30 and 50 s, selects the nearest common frame offset and measures 30–40 s in the Kalinka render. The two new renders are rounded to 24 bits without dither, then one stereo-linked gain per render matches 200 Hz–2 kHz RMS. The spectral estimate uses a 16,384-sample Hann window, 50% overlap and a power average across channels. Bands are lower-inclusive and upper-exclusive. The plotting bins average power density in 1 kHz intervals. The dBFS reference is the mean-square power of a full-scale sine. This measures all the signal in the music excerpt. The historical Saracon filter, gain setting and dither configuration are undocumented.
