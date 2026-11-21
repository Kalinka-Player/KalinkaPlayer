#include "AudioSampleFormat.h"
#include "AudioInfo.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>

void expandMono(uint8_t *data, size_t frames, unsigned channels, size_t width) {
  // Expand backwards so the next unread sample stays intact.
  for (size_t i = frames; i > 0; --i) {
    auto *first = data + (i - 1) * channels * width;
    std::memmove(first, data + (i - 1) * width, width);
    for (unsigned c = 1; c < channels; ++c)
      std::memcpy(first + c * width, first, width);
  }
}

void interleave(const StreamAudioFormat &format,
                const int32_t *const channels[], size_t frames,
                std::vector<uint8_t> &pcm) {
  const size_t width = sampleSize(format.sampleFormat);
  // A padded container leaves its spare high byte clear.
  const uint32_t mask =
      format.bitsPerSample < 32 ? (1u << format.bitsPerSample) - 1 : ~0u;
  pcm.resize(frames * format.channels * width);
  for (size_t i = 0; i < frames; ++i)
    for (unsigned channel = 0; channel < format.channels; ++channel) {
      const auto sample = uint32_t(channels[channel][i]) & mask;
      auto *dest = pcm.data() + (i * format.channels + channel) * width;
      for (size_t byte = 0; byte < width; ++byte)
        dest[byte] = sample >> (8 * byte);
    }
}

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
