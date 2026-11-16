#include "dsp/output/Quantiser.h"

#include "dsp/output/OutputKernels.h"

#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <string>

namespace {
template <class T> void store(uint8_t *at, T value) {
  std::memcpy(at, &value, sizeof(T));
}

/// Writes one significant-bit integer into its container, MSB-aligned.
void storeInteger(uint8_t *at, AudioSampleFormat format, int32_t value,
                  unsigned shift) {
  const int32_t aligned = int32_t(uint32_t(value) << shift);
  switch (format) {
  case PCM16_LE:
    return store(at, int16_t(aligned));
  case PCM24_LE:
    return store(at, aligned);
  case PCM24_3LE:
    at[0] = uint8_t(aligned);
    at[1] = uint8_t(aligned >> 8);
    at[2] = uint8_t(aligned >> 16);
    return;
  case PCM32_LE:
    return store(at, aligned);
  default:
    throw std::invalid_argument(std::string("No integer output in ") +
                                sampleFormatToString(format));
  }
}
} // namespace

Quantiser::Quantiser(size_t capacity, unsigned channels, uint64_t seed)
    : capacity(capacity), dither(seed), noise(capacity),
      rounded(channels, std::vector<int32_t>(capacity)) {}

void Quantiser::pack(const double *const *in, unsigned channels, size_t frames,
                     GainRamp::Span gain, AudioSampleFormat format,
                     unsigned bits, void *out) {
  if (frames > capacity || channels > rounded.size())
    throw std::invalid_argument("Quantiser asked for more than it holds");
  auto *bytes = static_cast<uint8_t *>(out);
  const size_t width = sampleSize(format);
  if (gain.from == 0.0 && gain.to == 0.0) {
    std::memset(bytes, 0, frames * channels * width);
    return;
  }
  const double step = (gain.to - gain.from) / double(frames);
  if (format == PCM_FLOAT32_LE || format == PCM_FLOAT64_LE) {
    for (size_t i = 0; i < frames; ++i) {
      const double g = gain.from + step * double(i + 1);
      for (unsigned c = 0; c < channels; ++c) {
        uint8_t *at = bytes + (i * channels + c) * width;
        if (format == PCM_FLOAT32_LE)
          store(at, float(in[c][i] * g));
        else
          store(at, in[c][i] * g);
      }
    }
    return;
  }
  const unsigned container = unsigned(sampleBits(format));
  bits = std::min(bits, container);
  const double scale = double(1ull << (bits - 1));
  const auto high = int32_t((1ll << (bits - 1)) - 1), low = -high - 1;
  for (unsigned c = 0; c < channels; ++c) {
    dither.fill(noise.data(), frames);
    clipped_ +=
        output_kernels::quantise(in[c], frames, gain.from, step, scale,
                                 noise.data(), low, high, rounded[c].data());
  }
  const unsigned shift = container - bits;
  for (size_t i = 0; i < frames; ++i)
    for (unsigned c = 0; c < channels; ++c)
      storeInteger(bytes + (i * channels + c) * width, format, rounded[c][i],
                   shift);
}
