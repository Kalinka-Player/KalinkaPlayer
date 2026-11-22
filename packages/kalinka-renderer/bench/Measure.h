#ifndef BENCH_MEASURE_H
#define BENCH_MEASURE_H

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace bench {

/// The rates every kernel is reported at.
inline constexpr unsigned kRates[] = {44100, 96000, 192000, 352800};

/**
 * @brief Median nanoseconds per frame of calling @p process on @p block
 * frames at a time, at least @p frames per repeat, after one warm-up repeat.
 */
template <typename Process>
double nsPerFrame(Process &&process, size_t block, size_t frames,
                  int repeats = 7) {
  std::vector<double> samples;
  const size_t blocks = frames / block + (frames % block != 0);
  for (int r = 0; r <= repeats; ++r) {
    const auto start = std::chrono::steady_clock::now();
    for (size_t i = 0; i < blocks; ++i) {
      process(block);
    }
    const std::chrono::duration<double, std::nano> elapsed =
        std::chrono::steady_clock::now() - start;
    if (r > 0) {
      samples.push_back(elapsed.count() / double(blocks * block));
    }
  }
  std::ranges::sort(samples);
  return samples[samples.size() / 2];
}

/// What @p ns per frame costs of one core at @p rate in real time, in percent.
inline double percentOfCore(double ns, unsigned rate) {
  return ns * rate / 1e7;
}

/// The machine, build and clock state a result was measured with, as Markdown.
std::string machine();

/// "12.3 ns (0.54 %)": per frame, and of one core in real time at @p rate.
std::string cost(double ns, unsigned rate);

/// Cost at the selected rates for kernels whose names contain the filter.
void runKernels(std::string_view filter, std::span<const unsigned> rates);
/// Time renderer profile and preamp edits at a fixed playback rate.
void runPeqSettings();
/// CPU and callback times for stereo partitioned convolution.
void runFir();
/// The SIMD mechanisms side by side, each null-tested against scalar.
void runSimd();
/// DSD-to-PCM conversion's cost, per conversion and kernel.
void runDsd();
/// The harness modulator's own THD+N and idle noise at DSD64 to DSD256.
void runFixtures();
/// What per-stream processing leaves at a gapless join, against continuous.
void runJoin();
/// Plays each file through AudioPlayer into ALSA null and reports its CPU.
void runPlayback(const std::vector<std::string> &files, int delayMs);
/// Writes @p seconds of dense stereo test audio as a WAV file.
void writeWav(const std::string &path, unsigned rate, unsigned bits,
              unsigned seconds);

} // namespace bench

#endif
