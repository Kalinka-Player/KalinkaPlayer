#include "Signals.h"

#include <algorithm>
#include <cmath>
#include <numbers>

namespace harness {

std::vector<double> sine(double frequency, double rate, size_t frames,
                         double amplitude, double phase) {
  std::vector<double> out(frames);
  const double step = 2 * std::numbers::pi * frequency / rate;
  for (size_t n = 0; n < frames; ++n) {
    out[n] = amplitude * std::sin(step * double(n) + phase);
  }
  return out;
}

std::vector<double> multitone(std::span<const double> frequencies, double rate,
                              size_t frames, double peak) {
  std::vector<double> out(frames, 0.0);
  const double count = double(frequencies.size());
  for (size_t k = 0; k < frequencies.size(); ++k) {
    const double phase = -std::numbers::pi * double(k) * double(k + 1) / count;
    const auto tone = sine(frequencies[k], rate, frames, 1.0, phase);
    std::ranges::transform(out, tone, out.begin(), std::plus<>());
  }
  const double top = std::ranges::max(out, {}, [](double x) {
    return std::abs(x);
  });
  if (top != 0) {
    for (double &x : out) {
      x *= peak / std::abs(top);
    }
  }
  return out;
}

std::vector<double> impulse(size_t frames, size_t at, double amplitude) {
  std::vector<double> out(frames, 0.0);
  if (at < frames) {
    out[at] = amplitude;
  }
  return out;
}

std::vector<double> logSweep(double from, double to, double rate,
                             size_t frames, double amplitude) {
  std::vector<double> out(frames);
  const double duration = double(frames) / rate;
  const double rise = duration / std::log(to / from);
  for (size_t n = 0; n < frames; ++n) {
    const double t = double(n) / rate;
    out[n] = amplitude * std::sin(2 * std::numbers::pi * from * rise *
                                  (std::exp(t / rise) - 1));
  }
  return out;
}

} // namespace harness
