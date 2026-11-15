#ifndef BENCH_MEASURE_H
#define BENCH_MEASURE_H

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <string>
#include <vector>

namespace bench {

/// The rates every kernel is reported at.
inline constexpr unsigned kRates[] = {44100, 96000, 192000, 352800};

/**
 * @brief Median nanoseconds per frame of calling @p process on @p block
 * frames at a time, @p frames frames per repeat, after one warm-up repeat.
 */
template <typename Process>
double nsPerFrame(Process &&process, size_t block, size_t frames,
                  int repeats = 7) {
  std::vector<double> samples;
  for (int r = 0; r <= repeats; ++r) {
    const auto start = std::chrono::steady_clock::now();
    for (size_t done = 0; done < frames; done += block) {
      process(block);
    }
    const std::chrono::duration<double, std::nano> elapsed =
        std::chrono::steady_clock::now() - start;
    if (r > 0) {
      samples.push_back(elapsed.count() / double(frames));
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

/// Each audio kernel's cost at every rate in kRates, as a Markdown table.
void runKernels();
/// The SIMD mechanisms side by side, each null-tested against scalar.
void runSimd();
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
