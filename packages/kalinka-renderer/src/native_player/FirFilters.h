#pragma once

#include <cstddef>
#include <vector>

/// Whether a filter runs the plain loops it is checked against, or vectors.
enum class FirPath { Reference, Vector };

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
  size_t delay() const { return history / 2; }
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

/**
 * @brief A half-band FIR that halves one channel's rate, with the history and
 * the phase it keeps between blocks; the history starts at zero.
 *
 * Write a block of up to capacity() samples into input(), then decimate() it.
 * An output follows every second input, so an odd block leaves one waiting
 * for the next. The taps are given by those at odd offsets 1, 3, 5... from the
 * centre, whose own tap is 0.5, and copied.
 * @note Not thread-safe. Allocates only when constructed.
 */
class HalfBandDecimator {
public:
  HalfBandDecimator(const double *side, size_t sides, size_t capacity,
                    FirPath path);

  /// Where the next block's samples go.
  double *input() { return buffer.data() + history; }
  size_t capacity() const { return capacity_; }
  /// Input samples the output lags the input by, output m standing at input 2m.
  size_t delay() const { return history / 2 - 1; }
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
  std::vector<double> side;
  size_t history;
  size_t capacity_;
  FirPath path;
  /// Whether one input already waits for its pair.
  bool pending = false;
  std::vector<double> buffer;
  /// The input's two phases, apart, for the vector loop.
  std::vector<double> centres, others;
};
