#include "dsp/peq/RoomEq.h"

#include <algorithm>
#include <cmath>
#include <numbers>

namespace peq {
namespace {
using Coefficients = std::array<double, 5>;

Coefficients coefficients(const Band &band, unsigned rate) {
  const double w = 2 * std::numbers::pi * band.hz / rate;
  if (isFirstOrder(band.type)) {
    const bool low = band.type == Type::FirstOrderLowPass;
    const double k = std::tan(w / 2), b0 = (low ? k : 1) / (1 + k);
    return {b0, low ? b0 : -b0, 0, (k - 1) / (1 + k), 0};
  }
  const double c = std::cos(w), alpha = std::sin(w) / (2 * band.q);
  if (isPass(band.type)) {
    const double sign = band.type == Type::LowPass ? 1 : -1;
    const double a0 = 1 + alpha, b0 = (1 - sign * c) / (2 * a0);
    return {b0, sign * 2 * b0, b0, -2 * c / a0, (1 - alpha) / a0};
  }
  const double a = std::pow(10.0, band.db / 40);
  if (band.type == Type::Peak) {
    const double a0 = 1 + alpha / a;
    return {(1 + alpha * a) / a0, -2 * c / a0, (1 - alpha * a) / a0,
            -2 * c / a0, (1 - alpha / a) / a0};
  }
  const double sign = band.type == Type::LowShelf ? 1 : -1;
  const double p = a + 1, m = a - 1, b = 2 * std::sqrt(a) * alpha;
  const double num = p - sign * m * c, den = p + sign * m * c, a0 = den + b;
  return {a * (num + b) / a0, 2 * a * (sign * m - p * c) / a0,
          a * (num - b) / a0, -2 * (sign * m + p * c) / a0, (den - b) / a0};
}

/// Two first-order sections as the one second-order section they make.
Coefficients paired(const Coefficients &p, const Coefficients &q) {
  return {p[0] * q[0], p[0] * q[1] + p[1] * q[0], p[1] * q[1], p[3] + q[3],
          p[3] * q[3]};
}

// A bilinear section's extrema are those of its analog prototype, including
// resonant shelves and pass filters whose gain exceeds both endpoints.
double maxGainDb(const Band &band) {
  if (isFirstOrder(band.type))
    return 0;
  const bool resonant =
      band.type != Type::Peak && band.q > std::numbers::sqrt2 / 2;
  if (isPass(band.type)) {
    const double q2 = band.q * band.q;
    return resonant ? 10 * std::log10(4 * q2 * q2 / (4 * q2 - 1)) : 0;
  }
  double peak = std::max(0.0, band.db);
  if (!resonant)
    return peak;
  const double a = std::pow(10.0, band.db / 40);
  const double k = a * (1 / (band.q * band.q) - 2);
  // Squared magnitude is a² (x² + kx + a²) / (a²x² + kx + 1).
  const double sum = -(1 + a * a) / k;
  const double hi = sum + std::sqrt(std::max(0.0, sum * sum - 1));
  for (const double x : {hi, 1 / hi}) {
    const double damping = a * x / (band.q * band.q);
    const double n = (x - a) * (x - a) + damping;
    const double d = (a * x - 1) * (a * x - 1) + damping;
    peak = std::max(peak, 10 * std::log10(a * a * n / d));
  }
  return peak;
}
} // namespace

Plan prepare(const Profile *profile, unsigned rate, unsigned channels) {
  Plan plan;
  if (!profile || !rate || !channels || channels > kPlanarChannels)
    return plan;
  const bool shared = !profile->shared.empty();
  auto &front = plan.pairs.front();
  const unsigned frontChannels = std::min(channels, 2u);
  for (unsigned ch = 0; ch < (shared ? 1u : frontChannels); ++ch) {
    const auto &bands = shared    ? profile->shared
                        : ch == 0 ? profile->left
                                  : profile->right;
    size_t count = 0;
    std::optional<size_t> unpaired;
    double headroom = 0;
    for (const Band &band : bands) {
      if (isFlat(band))
        continue;
      // A second first-order band shares the section of the one before it.
      const bool pairs = isFirstOrder(band.type) && unpaired;
      if (band.hz >= rate * 0.5 || (!pairs && count == kBands))
        return {.applicable = false};
      Section &s = front.sections[pairs ? *unpaired : count];
      Coefficients section = coefficients(band, rate);
      if (pairs) {
        section = paired({s.b0[ch], s.b1[ch], 0, s.a1[ch], 0}, section);
        unpaired.reset();
      } else {
        if (isFirstOrder(band.type))
          unpaired = count;
        ++count;
      }
      const auto [b0, b1, b2, a1, a2] = section;
      if (!(std::abs(a2) < 1 && 1 + a1 + a2 > 0 && 1 - a1 + a2 > 0))
        return {.applicable = false};
      for (unsigned lane = ch; lane < (shared ? frontChannels : ch + 1);
           ++lane) {
        s.b0[lane] = b0;
        s.b1[lane] = b1;
        s.b2[lane] = b2;
        s.a1[lane] = a1;
        s.a2[lane] = a2;
      }
      headroom += maxGainDb(band);
    }
    front.count = std::max(front.count, count);
    plan.automaticPreampDb = std::min(plan.automaticPreampDb, -headroom);
  }
  plan.active = front.count != 0;
#if defined(__aarch64__)
  front.parallel = prepareParallel(front, shared && frontChannels == 2);
#endif
  if (shared)
    for (unsigned ch = 2; ch < channels; ch += 2)
      plan.pairs[ch / 2] = front;
  return plan;
}

void RoomEq::follow(const Plan &plan, double preampDb, unsigned rate,
                    bool reset) {
  const double gain = std::pow(10.0, preampDb / 20);
  if (reset) {
    preamp.settle(gain);
    current = plan;
    history = {};
    remaining = 0;
  } else if (!remaining && current.pairs != plan.pairs) {
    // Blend each branch at its own headroom so a newly boosted band cannot
    // outrun the attenuation ramp during the switch.
    oldGain = preamp.take(0).from;
    newGain = gain;
    preamp.settle(gain);
    previous = current;
    oldHistory = history;
    current = plan;
    history = {};
    length = std::max(1u, rate / (newGain == oldGain ? 100 : 40));
    remaining = length - 1;
  } else {
    preamp.aim(gain, rate);
  }
}

size_t RoomEq::span(size_t frames) const {
  if (remaining)
    return std::min(frames, remaining);
  return preamp.left() ? std::min(frames, preamp.left()) : frames;
}

GainRamp::Span RoomEq::takePreamp(size_t frames) {
  return transitioning() ? GainRamp::Span{1, 1, frames} : preamp.take(frames);
}

void RoomEq::process(double *const *planes, unsigned channels, size_t frames) {
  if (remaining)
    for (unsigned ch = 0; ch < channels; ++ch)
      std::copy_n(planes[ch], frames, scratch.planes()[ch]);
  for (unsigned ch = 0; ch < channels; ch += 2) {
    const unsigned pair = ch / 2;
    const auto run = [&](const Plan &plan, History &state,
                         double *const *data) {
      if (plan.pairs[pair].count)
        filter(plan.pairs[pair], state[pair].data(), data[ch],
               ch + 1 < channels ? data[ch + 1] : nullptr, frames);
    };
    run(current, history, planes);
    if (remaining)
      run(previous, oldHistory, scratch.planes());
  }
  if (remaining) {
    for (unsigned ch = 0; ch < channels; ++ch)
      for (size_t i = 0; i < frames; ++i) {
        const double mix = double(length - remaining + i) / double(length);
        planes[ch][i] = std::lerp(oldGain * scratch.planes()[ch][i],
                                  newGain * planes[ch][i], mix);
      }
    remaining -= frames;
  }
}
} // namespace peq
