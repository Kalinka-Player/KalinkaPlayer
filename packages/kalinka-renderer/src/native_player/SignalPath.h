#pragma once

#include "AudioInfo.h"

#include <array>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

/// The SignalStep kinds the renderer reports, as its capabilities list them.
namespace signal_step {
inline constexpr const char *kChannels = "channels";
inline constexpr const char *kFloat = "float";
inline constexpr const char *kDsdToPcm = "dsd_to_pcm";
inline constexpr const char *kDither = "dither";
inline constexpr const char *kFir = "fir";
inline constexpr const char *kPeq = "peq";
inline constexpr const char *kPreamp = "preamp";
inline constexpr const char *kDsdGain = "dsd_gain";
inline constexpr const char *kVolume = "volume";
inline constexpr const char *kInteger = "integer";
inline constexpr const char *kZeroPadding = "zero_padding";
inline constexpr const char *kRepack = "repack";
inline constexpr std::array kAll{kChannels, kFloat,   kDsdToPcm,    kFir,
                                 kPeq,      kPreamp,  kDsdGain,     kVolume,
                                 kDither,   kInteger, kZeroPadding, kRepack};
} // namespace signal_step

/// @brief One step the samples are taken through between the decoder and
/// the device.
struct SignalStep {
  /// Stable, for a client to key on: "dsd_to_pcm".
  std::string kind;
  /// For a listener: "DSD to PCM".
  std::string name;
  /// How it is set, for a listener: "DSD64 to 176.4 kHz, 30 kHz filter".
  std::string summary;
  /// Every sample leaves the step with the value it came in with, at most
  /// zero-padded: a path of only such steps is bit-perfect.
  bool bitPerfect = false;

  bool operator==(const SignalStep &) const = default;
};

/// @brief What is done to the decoded samples on the way to the device, in
/// order, the software volume included.
struct SignalPath {
  std::vector<SignalStep> steps;
  /// What the steps hold back ahead of what is heard.
  double latencyMs = 0;

  bool operator==(const SignalPath &) const = default;
};

namespace signal_step {
/// Integer or float PCM in @p from, converted exactly to 64-bit float, for
/// @p purpose when the step has one of its own to name.
SignalStep toFloat(const StreamAudioFormat &from, std::string_view purpose = {});
/// Integer samples of @p from moved unchanged into the container of @p to;
/// nothing when the container is the same.
std::optional<SignalStep> container(const StreamAudioFormat &from,
                                    const StreamAudioFormat &to);
} // namespace signal_step
