#include "dsp/output/TruePeak.h"

#include <algorithm>
#include <cmath>
#include <numbers>
#include <span>
#include <stdexcept>

namespace {
constexpr size_t kFactor = 4;
constexpr size_t kLength = kFactor * 2 * (TruePeakMeter::kTaps / 2) + 1;
constexpr double kBeta = 8.0;

double besselI0(double x) {
  double sum = 1, term = 1;
  for (int k = 1; k < 50; ++k) {
    term *= (x / (2 * k)) * (x / (2 * k));
    sum += term;
  }
  return sum;
}
} // namespace

const std::array<TruePeakMeter::Phase, 3> &TruePeakMeter::phases() {
  static const auto designed = [] {
    std::array<Phase, 3> out{};
    const double centre = double(kLength - 1) / 2;
    for (size_t p = 1; p <= 3; ++p) {
      for (size_t i = 0; i < kTaps; ++i) {
        const double n = double(kFactor * i + p);
        const double t = (n - centre) / double(kFactor);
        const double sinc =
            std::sin(std::numbers::pi * t) / (std::numbers::pi * t);
        const double r = (n - centre) / centre;
        out[p - 1][i] =
            sinc * besselI0(kBeta * std::sqrt(1 - r * r)) / besselI0(kBeta);
      }
      double sum = 0;
      for (const double tap : out[p - 1])
        sum += tap;
      for (double &tap : out[p - 1])
        tap /= sum;
    }
    return out;
  }();
  return designed;
}

TruePeakMeter::TruePeakMeter(size_t maxFrames, unsigned maxChannels)
    : maxFrames(maxFrames),
      lines(maxChannels, std::vector<double>(kTaps - 1 + maxFrames)) {}

void TruePeakMeter::feed(const double *const *planes, unsigned channels,
                         size_t frames, unsigned rate, double gain) {
  if (frames > maxFrames || channels > lines.size())
    throw std::invalid_argument("More than a true-peak meter was made for");
  if (rate != this->rate || channels != this->channels) {
    for (auto &line : lines)
      std::fill(line.begin(), line.end(), 0.0);
    this->rate = rate;
    this->channels = channels;
  }
  // Interpolated far enough that the samples lie at least 176.4 kHz apart.
  const auto &all = phases();
  const std::span<const Phase> interpolated =
      rate <= 48000   ? std::span(all)
      : rate <= 96000 ? std::span(all).subspan(1, 1)
                      : std::span<const Phase>();
  double highest = 0;
  for (unsigned c = 0; c < channels; ++c) {
    double *line = lines[c].data();
    std::copy(planes[c], planes[c] + frames, line + kTaps - 1);
    for (size_t k = 0; k < frames; ++k) {
      const double *newest = line + kTaps - 1 + k;
      highest = std::max(highest, std::abs(*newest));
      for (const Phase &phase : interpolated) {
        double sum = 0;
        for (size_t i = 0; i < kTaps; ++i)
          sum += phase[i] * newest[-std::ptrdiff_t(i)];
        highest = std::max(highest, std::abs(sum));
      }
    }
    std::copy(line + frames, line + frames + kTaps - 1, line);
  }
  peak_ = std::max(peak_, highest * std::abs(gain));
}

