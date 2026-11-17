#include "dsp/output/PlanarPcm.h"

#include <cstring>
#include <stdexcept>
#include <string>

namespace {
template <class Value>
void deinterleave(const uint8_t *in, unsigned channels, size_t frames,
                  size_t width, double scale, Value value, double *const *out) {
  for (unsigned c = 0; c < channels; ++c) {
    const uint8_t *at = in + c * width;
    double *to = out[c];
    for (size_t i = 0; i < frames; ++i, at += channels * width)
      to[i] = double(value(at)) * scale;
  }
}

template <class T> T load(const uint8_t *at) {
  T value;
  std::memcpy(&value, at, sizeof(T));
  return value;
}
} // namespace

void readPlanar(const uint8_t *in, AudioSampleFormat format, unsigned channels,
                size_t frames, double *const *out) {
  const size_t width = sampleSize(format);
  switch (format) {
  case PCM16_LE:
    return deinterleave(in, channels, frames, width, 1.0 / 32768.0,
                        load<int16_t>, out);
  case PCM24_LE:
    // 24 bits in the low three bytes of a word, the sign in bit 23.
    return deinterleave(
        in, channels, frames, width, 1.0 / 8388608.0,
        [](const uint8_t *at) {
          return int32_t(uint32_t(load<int32_t>(at)) << 8) >> 8;
        },
        out);
  case PCM24_3LE:
    return deinterleave(
        in, channels, frames, width, 1.0 / 8388608.0,
        [](const uint8_t *at) {
          const uint32_t word = uint32_t(at[0]) << 8 | uint32_t(at[1]) << 16 |
                                uint32_t(at[2]) << 24;
          return int32_t(word) >> 8;
        },
        out);
  case PCM32_LE:
    return deinterleave(in, channels, frames, width, 1.0 / 2147483648.0,
                        load<int32_t>, out);
  case PCM_FLOAT32_LE:
    return deinterleave(in, channels, frames, width, 1.0, load<float>, out);
  case PCM_FLOAT64_LE:
    return deinterleave(in, channels, frames, width, 1.0, load<double>, out);
  default:
    throw std::invalid_argument(std::string("No planar PCM from ") +
                                sampleFormatToString(format));
  }
}
