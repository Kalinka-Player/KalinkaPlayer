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
  /// A run of modes that share one input and sum into one output.
  struct Bank {
    size_t first = 0, count = 0;
    double direct = 1;
  };
  struct Channel {
    alignas(64) std::array<Mode, kBands> modes{};
    /// Applied one after the other. An unused bank has no modes.
    std::array<Bank, 2> banks{};
  };
  std::array<Channel, 2> channels;
  /// Leading sections of the cascade that the modes stand for. The rest
  /// follow them in series.
  size_t replaced = 0;
};

/**
 * @brief Divides @p cascade into banks of parallel modes and a serial
 * remainder.
 *
 * Sections whose poles repeat or crowd one another cannot expand accurately
 * in one bank. They fill a second bank where enough of them agree, and
 * otherwise stay second-order sections, moved behind the rest in each
 * channel. An inline plan keeps allocation and reclamation off the playback
 * thread.
 * @return The plan for the leading sections, or nothing, with @p cascade as
 * it was, where the original sections serve better.
 */
std::optional<ParallelPlan> prepareParallel(Cascade &cascade,
                                            bool sharedStereo);
void filterParallel(const ParallelPlan &plan, State *history, double *left,
                    double *right, size_t frames);
} // namespace peq
