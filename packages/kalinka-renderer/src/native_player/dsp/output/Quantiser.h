#pragma once

#include "AudioSampleFormat.h"
#include "dsp/output/GainRamp.h"
#include "dsp/output/TpdfDither.h"

#include <cstddef>
#include <cstdint>
#include <vector>

/**
 * @brief The last step to the device: scales planar float64 by a gain, adds
 * TPDF dither at the significant bits the device keeps, rounds, clamps and
 * packs the result interleaved into the output format, counting what it
 * clamps.
 *
 * Integer output carries its significant bits in the top of the container.
 * Float output is only scaled. A gain held at zero writes digital silence,
 * not dither.
 * @note Not thread-safe. Allocates only when constructed.
 */
class Quantiser {
public:
  /**
   * @param capacity The most frames one pack() takes.
   * @param channels The most channels one pack() takes.
   */
  Quantiser(size_t capacity, unsigned channels, uint64_t seed);

  /**
   * @brief Packs @p frames frames of @p in, one array per channel, into
   * @p out, with the gain moving across them as @p gain says.
   * @param bits The significant bits to dither at; at most the container's.
   */
  void pack(const double *const *in, unsigned channels, size_t frames,
            GainRamp::Span gain, AudioSampleFormat format, unsigned bits,
            void *out);

  /// Samples clamped to the output's range so far.
  uint64_t clipped() const { return clipped_; }

private:
  size_t capacity;
  TpdfDither dither;
  std::vector<double> noise;
  std::vector<std::vector<int32_t>> rounded;
  uint64_t clipped_ = 0;
};
