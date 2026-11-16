#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

/**
 * @brief Triangular dither, 2 LSB peak to peak, from a seedable xoshiro256+
 * generator: each value is the difference of two uniforms on [0, 1), so it
 * lies in (-1, 1) LSB, averages zero and has a variance of 1/6 LSB².
 *
 * Every value comes from its own draw, so channels filled one after another
 * are independent.
 * @note Not thread-safe.
 */
class TpdfDither {
public:
  explicit TpdfDither(uint64_t seed);

  /// Fills @p out with @p count values, in LSBs.
  void fill(double *out, size_t count);

private:
  std::array<uint64_t, 4> state;

  uint64_t next();
};
