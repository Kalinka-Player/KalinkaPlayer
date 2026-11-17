#pragma once

#include "dsp/fir/FirPath.h"

#include <cstddef>
#include <vector>

/**
 * @brief A half-band FIR that halves one channel's rate, with the history and
 * the phase it keeps between blocks; the history starts at zero.
 *
 * Write a block of up to capacity() samples into input(), then decimate() it.
 * An output follows every second input, so an odd block leaves one waiting
 * for the next. The taps are given by those at odd offsets 1, 3, 5... from the
 * centre, whose own tap is 0.5, and copied.
 *
 * The history is kept as the input's two phases, apart, so a block only
 * splits its own samples, whatever the length of the filter.
 * @note Not thread-safe. Allocates only when constructed.
 */
class HalfBandDecimator {
public:
  HalfBandDecimator(const double *side, size_t sides, size_t capacity,
                    FirPath path);

  /// Where the next block's samples go.
  double *input() { return buffer.data() + kLead; }
  size_t capacity() const { return capacity_; }
  /// Input samples the output lags the input by, output m standing at input 2m.
  size_t delay() const { return 2 * side.size() - 2; }
  /// The most outputs decimate() makes from @p count inputs.
  static size_t maxOutputs(size_t count) { return count / 2 + 1; }
  /**
   * @brief Decimates the @p count samples written to input() into @p out.
   * @return The outputs written.
   */
  size_t decimate(size_t count, double *out);
  /// Zeroes the history and forgets the waiting input.
  void reset();

private:
  /// Samples of buffer before input(): the waiting input's slot, after one
  /// of padding that keeps input() aligned for the vectors written to it.
  static constexpr size_t kLead = 2;

  std::vector<double> side;
  size_t capacity_;
  FirPath path;
  /// Each phase's samples of history, rounded up to even so the block's are
  /// split to an aligned place: sides - 1 of the centre phase, 2 sides - 1 of
  /// the other.
  size_t evenLead, oddLead;
  /// Whether one input already waits for its pair, just before input().
  bool pending = false;
  std::vector<double> buffer;
  /// The centre phase: evenLead samples of history, then the block's.
  std::vector<double> evens;
  /// The other phase: oddLead samples of history, then the block's.
  std::vector<double> odds;
};
