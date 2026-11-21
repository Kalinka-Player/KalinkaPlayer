#pragma once

#include "dsp/output/GainRamp.h"
#include "dsp/output/PlanarPcm.h"

#include "dsp/peq/Profile.h"
#if defined(__aarch64__)
#include "dsp/peq/ParallelPeq.h"
#endif
#include <array>

namespace peq {
using Stereo = std::array<double, 2>;
struct Section {
  Stereo b0{1, 1}, b1{}, b2{}, a1{}, a2{};
  bool operator==(const Section &) const = default;
};
struct Cascade {
  std::array<Section, kBands> sections{};
  size_t count = 0;
#if defined(__aarch64__)
  std::optional<ParallelPlan> parallel;
#endif
  bool operator==(const Cascade &other) const {
    return count == other.count && sections == other.sections;
  }
};

/// Coefficients prepared for the stream's rate, before the audio thread reads
/// them.
struct Plan {
  std::array<Cascade, kPlanarChannels / 2> pairs{};
  double preampDb = 0;
  bool active = false;
  bool applicable = true;
};

Plan prepare(const Settings &settings, unsigned rate, unsigned channels);

struct State {
  Stereo z1{}, z2{};
};
void filter(const Cascade &cascade, State *state, double *left, double *right,
            size_t frames);

/// Keeps both filter histories during a profile change. Owned by the pulling
/// thread.
class RoomEq {
public:
  void follow(const Plan &plan, unsigned rate, bool reset);
  bool filtering() const { return current.active || transitioning(); }
  bool bypassed() const { return !filtering() && preamp.holding(1); }
  GainRamp::Span takePreamp(size_t frames);
  bool transitioning() const { return remaining != 0; }
  size_t span(size_t frames) const;
  void process(double *const *planes, unsigned channels, size_t frames);

private:
  using History = std::array<std::array<State, kBands>, kPlanarChannels / 2>;
  Plan current, previous;
  GainRamp preamp{std::chrono::milliseconds(25)};
  double oldGain = 1, newGain = 1;
  History history{}, oldHistory{};
  PlanarScratch scratch;
  size_t remaining = 0, length = 0;
};

} // namespace peq
