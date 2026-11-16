#pragma once

#include "dsp/dsd/DsdOptions.h"

#include <cstddef>
#include <cstdint>
#include <memory>

/**
 * @brief Stage 1 of DSD to PCM: divides one-bit DSD by 8, a byte at a time,
 * from tables of each group of 8 taps, keeping each channel's history between
 * blocks. The history starts as the idle pattern, which converts to zero.
 *
 * Every implementation gives the same output, bit for bit: the tables hold
 * whole numbers, whose sums are exact in any order.
 * @note Not thread-safe. Allocates only when made.
 */
class Stage1 {
public:
  virtual ~Stage1() = default;

  /**
   * @brief Converts @p bytes bytes of each channel, interleaved one byte per
   * channel, into out[c], one sample per byte.
   * @param bytes At most the capacity it was made with.
   */
  virtual void process(const uint8_t *in, size_t bytes, double *const *out) = 0;
  /// Forgets the history, as if every earlier byte had been the idle pattern.
  virtual void reset() = 0;
};

/// What a stage 1 is made from.
struct Stage1Spec {
  /// Symmetric taps, whole numbers, a multiple of 8 of them; not kept.
  const int64_t *taps;
  size_t size;
  /// What one unit of a tap is worth.
  double scale;
  unsigned channels;
  DsdBitOrder order;
  /// The most bytes per channel one call converts.
  size_t capacity;
};

/// The stage 1 that runs @p kernel's loops.
std::unique_ptr<Stage1> makeStage1(DsdKernel kernel, const Stage1Spec &spec);
