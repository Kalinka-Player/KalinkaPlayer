#pragma once

#include "dsp/fir/FirPath.h"

#include <cstddef>
#include <vector>

/**
 * @brief A linear-phase FIR of odd length for one channel, with the history it
 * keeps between blocks; the history starts at zero.
 *
 * Write a block of up to capacity() samples into input(), then filter() it.
 * The taps are given by their first half, the centre last, and copied.
 * @note Not thread-safe. Allocates only when constructed.
 */
class SymmetricFir {
public:
  SymmetricFir(const double *half, size_t halfSize, size_t capacity,
               FirPath path);

  /// Where the next block's samples go.
  double *input() { return buffer.data() + history; }
  size_t capacity() const { return capacity_; }
  /// Samples the output lags the input by.
  size_t delay() const { return delayFor(taps.size()); }
  /// The delay() of a filter given @p halfSize taps.
  static size_t delayFor(size_t halfSize) { return halfSize - 1; }
  /// Filters the @p count samples written to input() into @p out.
  void filter(size_t count, double *out);
  /// Zeroes the history.
  void reset();

private:
  std::vector<double> taps;
  size_t history;
  size_t capacity_;
  FirPath path;
  std::vector<double> buffer;
};
