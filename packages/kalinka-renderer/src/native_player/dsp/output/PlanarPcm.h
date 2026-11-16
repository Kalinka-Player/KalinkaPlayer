#pragma once

#include "AudioSampleFormat.h"

#include <cstddef>
#include <cstdint>

/**
 * @brief Reads @p frames frames of interleaved PCM in @p format into planar
 * float64, one array per channel, full scale ±1.0. An integer sample of an
 * N-bit container is divided by 2^(N - 1), so the conversion is exact.
 * @throws std::invalid_argument for DSD and DoP.
 */
void readPlanar(const uint8_t *in, AudioSampleFormat format, unsigned channels,
                size_t frames, double *const *out);
