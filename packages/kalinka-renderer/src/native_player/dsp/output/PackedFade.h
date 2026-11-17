#pragma once

#include "AudioSampleFormat.h"
#include "dsp/output/Quantiser.h"

#include <cstddef>
#include <cstdint>
#include <random>
#include <vector>

/**
 * @brief Fades PCM already packed for the device to silence, in place: what a
 * device holds unplayed at a seek, which no stage upstream can reach any more.
 *
 * The gain falls as the OutputStage's does over the same frames, and the
 * result is dithered at the significant bits given.
 * @note Not thread-safe. Allocates only when constructed.
 */
class PackedFade {
public:
  explicit PackedFade(uint64_t seed = std::random_device{}());

  /**
   * @brief Fades frames [@p at, @p at + @p frames) of a fade @p length frames
   * long, interleaved in @p data.
   * @param bits The significant bits to dither at.
   * @throws std::invalid_argument for DSD, DoP, or more channels than it holds.
   */
  void out(uint8_t *data, AudioSampleFormat format, unsigned channels,
           unsigned bits, size_t at, size_t frames, size_t length);

private:
  Quantiser quantiser;
  std::vector<std::vector<double>> planar;
  std::vector<double *> planes;
};
