#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

/**
 * @brief Triangular dither, 2 LSB peak to peak, from seedable xoshiro256+
 * generators: each value is the difference of two uniforms on [0, 1), so it
 * lies in (-1, 1) LSB, averages zero and has a variance of 1/6 LSB².
 *
 * Four generators are taken in turn, so one draw does not wait for the last.
 * Every value comes from its own draw, so channels filled one after another
 * are independent.
 * @note Not thread-safe.
 */
class TpdfDither {
public:
  explicit TpdfDither(uint64_t seed);

  /// Fills @p out with @p count values, in LSBs.
  void fill(double *out, size_t count);

  /// Applies gain and dither to one channel and counts clamped samples.
  /// Uses the same sequence as fill(). Scratch holds count doubles.
  size_t quantise(const double *in, size_t count, double from, double step,
                  double scale, int32_t low, int32_t high, double *scratch,
                  int32_t *out);

private:
  using Generator = std::array<uint64_t, 4>;
  std::array<Generator, 4> generators;
};
