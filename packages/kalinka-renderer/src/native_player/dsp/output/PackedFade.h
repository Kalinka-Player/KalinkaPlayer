#pragma once

#include "AudioSampleFormat.h"
#include "dsp/output/GainRamp.h"
#include "dsp/output/Quantiser.h"

#include <cstddef>
#include <cstdint>
#include <random>
#include <vector>

/**
 * @brief Fades PCM already packed for the device to silence, in place: what a
 * device holds unplayed at a seek, which no stage upstream can reach any more.
 *
 * The fade is the OutputStage's: the same GainRamp over the same length,
 * dithered at the significant bits given. Past its end, frames are silenced.
 * @note Not thread-safe. Allocates only when constructed.
 */
class PackedFade {
public:
  explicit PackedFade(uint64_t seed = std::random_device{}());

  /// Starts a fade from full level at @p rate.
  void begin(unsigned rate);

  /**
   * @brief Fades the next @p frames frames of it, interleaved in @p data.
   * @param bits The significant bits to dither at.
   * @throws std::invalid_argument for DSD, DoP, or more channels than it holds.
   */
  void out(uint8_t *data, AudioSampleFormat format, unsigned channels,
           unsigned bits, size_t frames);

private:
  GainRamp ramp;
  Quantiser quantiser;
  std::vector<std::vector<double>> planar;
  std::vector<double *> planes;
};
