#ifndef AUDIO_SAMPLE_FORMAT_H
#define AUDIO_SAMPLE_FORMAT_H

#include <cstddef>
#include <cstdint>
#include <optional>

enum AudioSampleFormat {
  PCM16_LE,
  PCM24_LE,
  PCM32_LE,
  PCM24_3LE,
  DSD_U8,
  DSD_U16_LE,
  DSD_U32_LE,
  DSD_U16_BE,
  DSD_U32_BE,
  DOP24_LE,
  DOP24_3LE,
  DOP32_LE
};

inline bool isDsd(AudioSampleFormat format) { return format >= DSD_U8; }
inline bool isDop(AudioSampleFormat format) { return format >= DOP24_LE; }

inline size_t sampleSize(AudioSampleFormat format) {
  switch (format) {
  case DSD_U8:
    return 1;
  case DSD_U16_LE:
  case DSD_U16_BE:
    return 2;
  case DSD_U32_LE:
  case DSD_U32_BE:
  case DOP24_LE:
  case DOP32_LE:
    return 4;
  case DOP24_3LE:
    return 3;
  case AudioSampleFormat::PCM16_LE:
    return 2;
  case AudioSampleFormat::PCM24_LE:
  case AudioSampleFormat::PCM32_LE:
    return 4;
  case AudioSampleFormat::PCM24_3LE:
    return 3;
  default:
    return 0;
  }
}

inline size_t sampleBits(AudioSampleFormat format) {
  if (isDsd(format))
    return isDop(format) ? 24 : 1;
  switch (format) {
  case AudioSampleFormat::PCM16_LE:
    return 16;
  case AudioSampleFormat::PCM24_LE:
  case AudioSampleFormat::PCM24_3LE:
  case AudioSampleFormat::PCM32_LE:
    return 24;
  default:
    return 0;
  }
}

inline const char *const sampleFormatToString(AudioSampleFormat format) {
  switch (format) {
  case DSD_U8:
    return "DSD_U8";
  case DSD_U16_LE:
    return "DSD_U16_LE";
  case DSD_U32_LE:
    return "DSD_U32_LE";
  case DSD_U16_BE:
    return "DSD_U16_BE";
  case DSD_U32_BE:
    return "DSD_U32_BE";
  case DOP24_LE:
    return "DoP_S24_LE";
  case DOP24_3LE:
    return "DoP_S24_3LE";
  case DOP32_LE:
    return "DoP_S32_LE";
  case AudioSampleFormat::PCM16_LE:
    return "PCM16_LE";
  case AudioSampleFormat::PCM24_LE:
    return "PCM24_LE";
  case AudioSampleFormat::PCM32_LE:
    return "PCM32_LE";
  case AudioSampleFormat::PCM24_3LE:
    return "PCM24_3LE";
  default:
    return "Unknown";
  }
}

/// @brief The next on-wire format to offer a device that refused @p format.
///
/// Each step holds every bit the pipeline keeps in the format before it, so
/// convertSampleFormat() carries samples across with their values unchanged.
/// @return std::nullopt for DSD, and once no PCM container is left to try.
inline std::optional<AudioSampleFormat> pcmFallback(AudioSampleFormat format) {
  switch (format) {
  case AudioSampleFormat::PCM16_LE:
    return AudioSampleFormat::PCM24_LE;
  case AudioSampleFormat::PCM24_LE:
    return AudioSampleFormat::PCM32_LE;
  case AudioSampleFormat::PCM32_LE:
    return AudioSampleFormat::PCM24_3LE;
  default:
    return std::nullopt;
  }
}

inline int32_t packIntegers(int32_t a, int32_t b) {
  return ((b & 0xffff) << 16) + (a & 0xffff);
}

// Make sure buffer has enough space to allocate framesToBytes()
void convertToFormat(void *buffer, const int32_t *const samples[], size_t size,
                     AudioSampleFormat format);

/// @brief Convert a buffer of samples from one format to another.
/// @param source data to convert
/// @param sourceFormat format to convert from
/// @param sourceSamples number of samples in source
/// @param dest destination buffer
/// @param destFormat destination sample format
/// @param destSizeBytes size of the destination buffer in bytes
/// @return the number of samples converted
size_t convertSampleFormat(const void *source, AudioSampleFormat sourceFormat,
                           size_t sourceSamples, void *dest,
                           AudioSampleFormat destFormat, size_t destSizeBytes);

/// @brief Apply a linear gain in place to interleaved samples.
///
/// Used by the software-volume path. @p gain is a linear amplitude multiplier:
/// `gain >= 1.0` is a no-op (so full volume stays bit-perfect), `gain <= 0` mutes.
/// @p buffer holds samples in the given ALSA on-wire @p format and @p bytes must
/// be a whole number of samples; partial trailing bytes are left untouched.
void applyGainInPlace(void *buffer, size_t bytes, AudioSampleFormat format,
                      float gain);

#endif // AUDIO_SAMPLE_FORMAT_H
