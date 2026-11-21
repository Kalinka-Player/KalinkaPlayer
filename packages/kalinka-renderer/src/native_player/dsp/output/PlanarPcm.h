#pragma once

#include "AudioSampleFormat.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

/// The most channels the output side handles in float, and the frames it
/// takes at a time.
inline constexpr unsigned kPlanarChannels = 8;
inline constexpr size_t kPlanarFrames = 1024;

/**
 * @brief Planar float64 scratch, one array of kPlanarFrames per channel for
 * up to kPlanarChannels, allocated once.
 * @note Not thread-safe.
 */
class PlanarScratch {
public:
  PlanarScratch();
  /// The arrays, as readPlanar() fills them and the Quantiser packs them.
  double *const *planes() { return pointers.data(); }

private:
  struct alignas(64) Plane {
    std::array<double, kPlanarFrames> samples;
  };
  std::vector<Plane> channels;
  std::array<double *, kPlanarChannels> pointers;
  PlanarScratch(const PlanarScratch &) = delete;
  PlanarScratch &operator=(const PlanarScratch &) = delete;
};

/**
 * @brief Reads @p frames frames of interleaved PCM in @p format into planar
 * float64, one array per channel, full scale ±1.0. An integer sample of an
 * N-bit container is divided by 2^(N - 1), so the conversion is exact.
 * @throws std::invalid_argument for DSD and DoP.
 */
void readPlanar(const uint8_t *in, AudioSampleFormat format, unsigned channels,
                size_t frames, double *const *out);
