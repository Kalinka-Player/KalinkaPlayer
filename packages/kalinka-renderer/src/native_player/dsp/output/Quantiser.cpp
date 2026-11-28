#include "dsp/output/Quantiser.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <string>

namespace {
template <class T> void store(uint8_t *at, T value) {
  std::memcpy(at, &value, sizeof(T));
}

/// Interleaves each channel's integers into containers of @p Width bytes,
/// which @p put writes from the MSB-aligned word.
template <size_t Width, class Put>
void interleave(const std::vector<std::vector<int32_t>> &rounded,
                unsigned channels, size_t frames, unsigned shift, Put put,
                uint8_t *out) {
  if (channels == 2) {
    const int32_t *left = rounded[0].data(), *right = rounded[1].data();
    for (size_t i = 0; i < frames; ++i, out += 2 * Width) {
      put(out, uint32_t(left[i]) << shift);
      put(out + Width, uint32_t(right[i]) << shift);
    }
    return;
  }
  for (unsigned c = 0; c < channels; ++c) {
    const int32_t *from = rounded[c].data();
    uint8_t *at = out + c * Width;
    for (size_t i = 0; i < frames; ++i, at += channels * Width)
      put(at, uint32_t(from[i]) << shift);
  }
}

// Lambdas, not functions: each its own type, so every loop inlines its store.
constexpr auto put16 = [](uint8_t *at, uint32_t aligned) {
  store(at, int16_t(aligned));
};
constexpr auto put32 = [](uint8_t *at, uint32_t aligned) {
  store(at, int32_t(aligned));
};
constexpr auto put24 = [](uint8_t *at, uint32_t aligned) {
  at[0] = uint8_t(aligned);
  at[1] = uint8_t(aligned >> 8);
  at[2] = uint8_t(aligned >> 16);
};
} // namespace

Quantiser::Quantiser(size_t capacity, unsigned channels, uint64_t seed)
    : capacity(capacity), dither(seed), noise(capacity),
      rounded(channels, std::vector<int32_t>(capacity)) {}

void Quantiser::pack(const double *const *in, unsigned channels, size_t frames,
                     GainRamp::Span gain, AudioSampleFormat format,
                     unsigned bits, void *out, bool preserveExact) {
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
    // Bypassed integer PCM is exact in float64. Restore it without adding
    // noise when its original samples fit the output precision.
    if (preserveExact && gain.from == 1 && gain.to == 1) {
      size_t i = 0;
      for (; i < frames; ++i) {
        const double value = in[c][i] * scale;
        if (!(value >= low && value <= high && value == std::trunc(value)))
          break;
        rounded[c][i] = static_cast<int32_t>(value);
      }
      if (i == frames)
        continue;
      ++unrestored_;
    }
    clipped_ += dither.quantise(in[c], frames, gain.from, step, scale, low,
                                high, noise.data(), rounded[c].data());
  }
  const unsigned shift = container - bits;
  switch (format) {
  case PCM16_LE:
    return interleave<2>(rounded, channels, frames, shift, put16, bytes);
  case PCM24_LE:
  case PCM32_LE:
    return interleave<4>(rounded, channels, frames, shift, put32, bytes);
  case PCM24_3LE:
    return interleave<3>(rounded, channels, frames, shift, put24, bytes);
  default:
    throw std::invalid_argument(std::string("No integer output in ") +
                                sampleFormatToString(format));
  }
}
