#pragma once

#include "../output/GainRamp.h"

#include <array>
#include <cstddef>

enum class ExpansionLevel { Off, Normal, Strong };

/// A symmetric speaker-placement filter on the stereo difference signal.
/// Owned by the pulling thread. The direct speaker feeds have no delay.
class StereoExpansion {
public:
  StereoExpansion();
  /// A zero rate bypasses the stage for a non-stereo source.
  void follow(unsigned rate, ExpansionLevel level, bool reset);
  bool active() const { return !mix.holding(0); }
  size_t span(size_t frames) const;
  /// Process a nonempty active block of the length returned by span().
  void process(double *left, double *right, size_t frames);

private:
  struct Section {
    double b0, b1, b2, a1, a2;
    double z1 = 0, z2 = 0;
  };
  std::array<Section, 3> sections{};
  unsigned rate = 0;
  GainRamp mix{std::chrono::milliseconds(25)};
  void prepare(unsigned newRate);
  void clear();
};
