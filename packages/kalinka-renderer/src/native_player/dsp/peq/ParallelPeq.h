#pragma once

#include "dsp/peq/Profile.h"
#include <array>

namespace peq {
struct Cascade;
struct State;

struct ParallelPlan {
  struct Mode {
    double b, a, c, d;
  };
  struct Channel {
    alignas(64) std::array<Mode, kBands> modes{};
    size_t count = 0;
    double direct = 1;
  };
  std::array<Channel, 2> channels;
};

/// An inline plan keeps allocation and reclamation off the playback thread.
/// An empty result selects the original second-order sections.
std::optional<ParallelPlan> prepareParallel(const Cascade &cascade);
void filterParallel(const ParallelPlan &plan, State *history, double *left,
                    double *right, size_t frames);
} // namespace peq
