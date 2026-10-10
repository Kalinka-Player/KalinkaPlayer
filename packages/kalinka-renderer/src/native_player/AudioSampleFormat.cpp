#include "AudioSampleFormat.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>

void convertToFormat(void *buffer, const int32_t *const samples[], size_t size,
                     AudioSampleFormat format) {
  if (isDsd(format))
    throw std::invalid_argument("PCM conversion cannot produce DSD");
  switch (format) {
  case AudioSampleFormat::PCM16_LE: {
    uint32_t *iBuffer = static_cast<uint32_t *>(buffer);
    for (size_t i = 0; i < size; ++i) {
      *iBuffer++ = packIntegers(samples[0][i], samples[1][i]);
    }
    break;
  }
  case AudioSampleFormat::PCM24_LE: {
    uint32_t *iBuffer = static_cast<uint32_t *>(buffer);
    for (size_t i = 0; i < size; ++i) {
      *iBuffer++ = samples[0][i] & 0xffffff;
      *iBuffer++ = samples[1][i] & 0xffffff;
    }
    break;
  }
  case AudioSampleFormat::PCM32_LE: {
    uint32_t *iBuffer = static_cast<uint32_t *>(buffer);
    for (size_t i = 0; i < size; ++i) {
      *iBuffer++ = (samples[0][i] & 0xffffff) << 8;
      *iBuffer++ = (samples[1][i] & 0xffffff) << 8;
    }
    break;
  }
  case AudioSampleFormat::PCM24_3LE:
  default:
    throw std::runtime_error("Unsupported format");
  }
}

namespace {
inline int32_t getSample(const uint8_t *source, AudioSampleFormat format) {
  // A full-width signed sample keeps the low eight bits of true S32 audio.
  uint32_t result = 0;
  for (size_t i = 0; i < sampleSize(format); ++i)
    result |= uint32_t(source[i]) << (8 * i);
  switch (format) {
  case PCM16_LE:
    return static_cast<int32_t>(result << 16);
  case PCM24_LE:
  case PCM24_3LE:
    return static_cast<int32_t>(result << 8);
  case PCM32_LE:
    return static_cast<int32_t>(result);
  default:
    break;
  }
  throw std::runtime_error("getSample: Unsupported format");
}

inline size_t putSample(uint8_t *dest, int32_t rawSample,
                        AudioSampleFormat format) {
  uint32_t value = static_cast<uint32_t>(rawSample);
  switch (format) {
  case PCM16_LE:
    value >>= 16;
    break;
  case PCM24_LE:
  case PCM24_3LE:
    value >>= 8;
    break;
  case PCM32_LE:
    break;
  default:
    throw std::runtime_error("putSample: Unsupported format");
  }
  for (size_t i = 0; i < sampleSize(format); ++i)
    dest[i] = value >> (8 * i);
  return sampleSize(format);
}
} // namespace

size_t convertSampleFormat(const void *source, AudioSampleFormat sourceFormat,
                           size_t sourceSamples, void *dest,
                           AudioSampleFormat destFormat, size_t destSizeBytes) {
  if (isDsd(sourceFormat) || isDsd(destFormat))
    throw std::invalid_argument("DSD must use lossless transport packing");

  auto const destSampleBytes = sampleSize(destFormat);
  auto const sourceSampleBytes = sampleSize(sourceFormat);

  if (sourceFormat == destFormat) {
    auto minBytesToCopy =
        std::min(sourceSamples * sourceSampleBytes, destSizeBytes);
    minBytesToCopy -= minBytesToCopy % sourceSampleBytes;
    memcpy(dest, source, minBytesToCopy);
    return minBytesToCopy / sourceSampleBytes;
  }
  if (sourceFormat == PCM_FLOAT32_LE || sourceFormat == PCM_FLOAT64_LE ||
      destFormat == PCM_FLOAT32_LE || destFormat == PCM_FLOAT64_LE)
    throw std::invalid_argument("Float PCM requires a matching output format");

  const uint8_t *sourcePtr = static_cast<const uint8_t *>(source);
  uint8_t *destPtr = static_cast<uint8_t *>(dest);

  for (size_t i = 0; i < sourceSamples; i++) {
    if ((i + 1) * destSampleBytes > destSizeBytes) {
      return i;
    }
    const int32_t rawSample = getSample(sourcePtr, sourceFormat);
    destPtr += putSample(destPtr, rawSample, destFormat);
    sourcePtr += sourceSampleBytes;
  }
  return sourceSamples;
}

namespace {
// Scale a signed integer sample by `gain` and clamp to a `bits`-wide range.
// 64-bit arithmetic: a 32-bit range overflows `long` where it is 32 bits wide.
inline int64_t scaleClamp(int64_t sample, double gain, int bits) {
  const int64_t maxv = (int64_t(1) << (bits - 1)) - 1;
  const int64_t minv = -(int64_t(1) << (bits - 1));
  const int64_t scaled = std::llround(static_cast<double>(sample) * gain);
  return std::clamp(scaled, minv, maxv);
}
} // namespace

void applyGainInPlace(void *buffer, size_t bytes, AudioSampleFormat format,
                      float gain) {
  // Full volume must not touch the bits — keeps bit-perfect playback intact.
  if (gain >= 1.0f) {
    return;
  }
  if (isDsd(format))
    throw std::invalid_argument("Software gain cannot alter DSD");
  if (gain < 0.0f) {
    gain = 0.0f;
  }

  uint8_t *ptr = static_cast<uint8_t *>(buffer);
  switch (format) {
  case PCM_FLOAT32_LE: {
    auto *samples = reinterpret_cast<float *>(ptr);
    for (size_t i = 0; i < bytes / sizeof(float); ++i)
      samples[i] = static_cast<float>(double(samples[i]) * gain);
    break;
  }
  case PCM_FLOAT64_LE: {
    auto *samples = reinterpret_cast<double *>(ptr);
    for (size_t i = 0; i < bytes / sizeof(double); ++i)
      samples[i] *= double(gain);
    break;
  }
  case PCM16_LE: {
    int16_t *samples = reinterpret_cast<int16_t *>(ptr);
    const size_t count = bytes / sizeof(int16_t);
    for (size_t i = 0; i < count; ++i) {
      samples[i] = static_cast<int16_t>(scaleClamp(samples[i], gain, 16));
    }
    break;
  }
  case PCM32_LE: {
    int32_t *samples = reinterpret_cast<int32_t *>(ptr);
    const size_t count = bytes / sizeof(int32_t);
    for (size_t i = 0; i < count; ++i) {
      samples[i] = static_cast<int32_t>(scaleClamp(samples[i], gain, 32));
    }
    break;
  }
  case PCM24_LE: {
    // 24-bit value in the low 3 bytes of a 32-bit LE word; sign in bit 23.
    int32_t *samples = reinterpret_cast<int32_t *>(ptr);
    const size_t count = bytes / sizeof(int32_t);
    for (size_t i = 0; i < count; ++i) {
      const int32_t v =
          static_cast<int32_t>(static_cast<uint32_t>(samples[i]) << 8) >> 8;
      samples[i] = static_cast<int32_t>(scaleClamp(v, gain, 24)) & 0xFFFFFF;
    }
    break;
  }
  case PCM24_3LE: {
    const size_t count = bytes / 3;
    for (size_t i = 0; i < count; ++i) {
      uint8_t *b = ptr + i * 3;
      const int32_t raw = b[0] | (b[1] << 8) | (b[2] << 16);
      const int32_t v = static_cast<int32_t>(static_cast<uint32_t>(raw) << 8) >> 8;
      const int32_t scaled = static_cast<int32_t>(scaleClamp(v, gain, 24));
      b[0] = scaled & 0xFF;
      b[1] = (scaled >> 8) & 0xFF;
      b[2] = (scaled >> 16) & 0xFF;
    }
    break;
  }
  default:
    break;
  }
}
