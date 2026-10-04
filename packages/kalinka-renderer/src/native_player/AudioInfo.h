#ifndef AUDIO_INFO_H
#define AUDIO_INFO_H

#include "AudioSampleFormat.h"

#include <cstdint>
#include <optional>
#include <string>

/// @brief Defines the type of the stream.
enum StreamType {
  // Indicates that the stream is a sequence of bytes
  // that need to be processed (i.g. compressed data)
  BYTES,
  // Indicates that the stream is a sequence of audio frames
  // that can be directly played (i.g. PCM data).
  // The format of the frames is defined by the StreamAudioFormat.
  FRAMES
};

inline std::string streamTypeToString(StreamType type) {
  switch (type) {
  case BYTES:
    return "bytes";
  case FRAMES:
    return "frames";
  default:
    return "unnknown";
  }
}

/// @brief Defines the audio format of the frames in the stream.
struct StreamAudioFormat {
  unsigned int sampleRate = 0;
  unsigned int channels = 0;
  unsigned int bitsPerSample = 0;
  AudioSampleFormat sampleFormat;
  // Nonzero for DSD: sampleRate counts packed transport frames, while this
  // is the original one-bit audio rate. No resampling takes place.
  unsigned int dsdSampleRate = 0;

  bool operator==(const StreamAudioFormat &other) const = default;
  bool operator!=(const StreamAudioFormat &other) const = default;

  std::string toString() const {
    return "<AudioFormat sampleRate=" + std::to_string(sampleRate) +
           ", channels=" + std::to_string(channels) +
           ", bitsPerSample=" + std::to_string(bitsPerSample) +
           ", sampleFormat=" + sampleFormatToString(sampleFormat) + ">";
  }
};

/// @brief How the renderer holds the output device.
/// @note Shared is not proof that anything alters the samples, only that
/// nothing rules it out — the most a renderer can say about a path it shares.
enum class DeviceAccess {
  Unknown,
  Exclusive,  ///< the device itself, nothing in between
  Shared      ///< something sits between us and the card
};

inline std::string deviceAccessToString(DeviceAccess access) {
  switch (access) {
  case DeviceAccess::Exclusive:
    return "exclusive";
  case DeviceAccess::Shared:
    return "shared";
  default:
    return "unknown";
  }
}

/// @brief What the output device runs at, and how it is held.
struct DeviceInfo {
  StreamAudioFormat format;
  DeviceAccess access = DeviceAccess::Unknown;

  bool operator==(const DeviceInfo &other) const = default;
  bool operator!=(const DeviceInfo &other) const = default;

  std::string toString() const {
    return "<DeviceInfo format=" + format.toString() +
           ", access=" + deviceAccessToString(access) + ">";
  }
};

/// @brief Defines the information about the stream.
struct StreamInfo {
  // The format of the audio frames in the stream.
  // If the stream is a sequence of bytes, this field
  // is not used.
  StreamAudioFormat format;

  // The type of the stream.
  StreamType streamType;
  // The size of the stream in frames or bytes,
  // depending on the stream type.
  unsigned long streamSize = 0;

  bool operator==(const StreamInfo &other) const = default;
  bool operator!=(const StreamInfo &other) const = default;

  /// @brief How long the stream runs, or nullopt when its length is not known.
  /// @note streamSize stays 0 when the container states no length. That is
  /// "unknown", never "empty", so it must not collapse to a duration of 0.
  std::optional<uint64_t> durationMs() const {
    if (streamSize == 0 || format.sampleRate == 0) {
      return std::nullopt;
    }
    unsigned long frames = streamSize;
    if (streamType == BYTES) {
      const unsigned int bytesPerFrame =
          format.channels * format.bitsPerSample / 8;
      if (bytesPerFrame == 0) {
        return std::nullopt;
      }
      frames /= bytesPerFrame;
    }
    return static_cast<uint64_t>(frames) * 1000 / format.sampleRate;
  }

  std::string toString() const {
    return "<StreamInfo format=" + format.toString() +
           ", streamType=" + streamTypeToString(streamType) +
           ", streamSize=" + std::to_string(streamSize) + ">";
  }
};

#endif
