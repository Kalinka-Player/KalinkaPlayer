#include "dsp/spatial/StereoExpansion.h"
#include "dsp/FlushFilterTails.h"

#include <algorithm>
#include <cmath>
#include <limits>

namespace {
// The digital placement example in US 5,774,556, figs. 3, 5–7.
// Each numerator is [1, 0, b2]. Normalize each section to unity at DC.
struct ReferenceSection {
  double a1, a2, b2;
};
constexpr std::array<ReferenceSection, 3> reference{{
    {-1.64451184525604, 0.73799030853044, 0},
    {-0.99807001285503, 0.61059291835028, 0},
    {-0.6107968716533, 0.811801, 0.1},
}};

// Substitute z_ref^-1 = (k + z^-1) / (1 + k*z^-1) in each polynomial.
// At the reference rate this substitution leaves each polynomial unchanged.
std::array<double, 3> atRate(double p0, double p1, double p2, double k) {
  return {p0 + k * (p1 + k * p2), 2 * k * (p0 + p2) + p1 * (1 + k * k),
          p2 + k * (p1 + k * p0)};
}
} // namespace

StereoExpansion::StereoExpansion() { mix.settle(0); }

void StereoExpansion::prepare(unsigned newRate) {
  constexpr double referenceRate = 22050;
  const double k = (referenceRate - newRate) / (referenceRate + newRate);
  for (size_t i = 0; i < sections.size(); ++i) {
    const auto [a1, a2, b2] = reference[i];
    const double dc = (1 + a1 + a2) / (1 + b2);
    const auto a = atRate(1, a1, a2, k);
    const auto b = atRate(dc, 0, dc * b2, k);
    sections[i] = {b[0] / a[0], b[1] / a[0], b[2] / a[0], a[1] / a[0],
                   a[2] / a[0]};
  }
}

void StereoExpansion::clear() {
  for (auto &s : sections)
    s.z1 = s.z2 = 0;
}

void StereoExpansion::follow(unsigned newRate, ExpansionLevel level,
                             bool reset) {
  double target = 0;
  if (newRate && level != ExpansionLevel::Off)
    target = level == ExpansionLevel::Normal ? 0.15 : 0.30;
  if (rate != newRate) {
    if (newRate)
      prepare(newRate);
    rate = newRate;
    reset = true;
  }
  if (reset) {
    clear();
    mix.settle(target);
  } else {
    if (target != 0 && !active())
      clear();
    mix.aim(target, rate);
  }
}

size_t StereoExpansion::span(size_t frames) const {
  return mix.left() ? std::min(frames, mix.left()) : frames;
}

void StereoExpansion::process(double *left, double *right, size_t frames) {
  [[maybe_unused]] dsp::FlushFilterTails flush;
  const auto gain = mix.take(frames);
  const double step = (gain.to - gain.from) / double(frames);
  for (size_t i = 0; i < frames; ++i) {
    // Half-difference keeps opposite-polarity finite inputs representable.
    double x = 0.5 * left[i] - 0.5 * right[i];
    for (auto &s : sections) {
      const double y = s.b0 * x + s.z1;
      s.z1 = s.b1 * x - s.a1 * y + s.z2;
      s.z2 = s.b2 * x - s.a2 * y;
#if !defined(__aarch64__) && !defined(__SSE2__)
      if (std::abs(s.z1) < std::numeric_limits<double>::min())
        s.z1 = 0;
      if (std::abs(s.z2) < std::numeric_limits<double>::min())
        s.z2 = 0;
#endif
      x = y;
    }
    const double delta = (2 * (gain.from + step * double(i + 1))) * x;
    const double l = left[i] + delta, r = right[i] - delta;
    if (!std::isfinite(l) || !std::isfinite(r)) [[unlikely]] {
      clear();
      continue;
    }
    left[i] = l;
    right[i] = r;
  }
}
