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
  int32_t result = 0;
  switch (format) {
  case PCM16_LE:
    result = *reinterpret_cast<const int16_t *>(source);
    return (result << 8);
  case PCM24_LE:
    result = (*reinterpret_cast<const int32_t *>(source) & 0xffffff);
    result |= (0xff000000 * (result & 0x800000));
    return result;
  case PCM32_LE:
    result = *reinterpret_cast<const int32_t *>(source) >> 8;
    return result;
  case PCM24_3LE:
    result = ((source[0] << 8) | (source[1] << 16) | (source[2] << 24)) >> 8;
    return result;
  default:
    break;
  }
  throw std::runtime_error("getSample: Unsupported format");
}

inline size_t putSample(uint8_t *dest, int32_t rawSample,
                        AudioSampleFormat format) {
  switch (format) {
  case PCM16_LE:
    *reinterpret_cast<int16_t *>(dest) = (rawSample >> 8) & 0xffff;
    return 2u;
  case PCM24_LE:
    *reinterpret_cast<int32_t *>(dest) = rawSample & 0xffffff;
    return 4u;
  case PCM32_LE:
    *reinterpret_cast<int32_t *>(dest) = rawSample << 8;
    return 4u;
  case PCM24_3LE:
    *dest++ = rawSample & 0xff;
    *dest++ = (rawSample >> 8) & 0xff;
    *dest = (rawSample >> 16) & 0xff;
    return 3u;
  default:
    throw std::runtime_error("putSample: Unsupported format");
  }
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
// (getSample()/putSample() are not reused here: their PCM24_LE path does not
// sign-extend, which is fine for bit-pattern format conversion but wrong for an
// arithmetic multiply.)
inline long scaleClamp(long sample, double gain, int bits) {
  const long maxv = (1L << (bits - 1)) - 1;
  const long minv = -(1L << (bits - 1));
  long scaled = std::lround(static_cast<double>(sample) * gain);
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
