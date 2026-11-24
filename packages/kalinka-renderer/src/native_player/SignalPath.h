#pragma once

#include <array>
#include <string>
#include <vector>

/// The SignalStep kinds the renderer reports, in the order a path holds them.
namespace signal_step {
inline constexpr const char *kDsdToPcm = "dsd_to_pcm";
inline constexpr const char *kDsdGain = "dsd_gain";
inline constexpr const char *kDither = "dither";
inline constexpr const char *kFir = "fir";
inline constexpr const char *kPeq = "peq";
inline constexpr const char *kPreamp = "preamp";
inline constexpr std::array kAll{kDsdToPcm, kFir,     kPeq,
                                 kPreamp,   kDsdGain, kDither};
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

  bool operator==(const SignalStep &) const = default;
};

/// @brief What is done to the decoded samples on the way to the device, in
/// order; the volume is not part of it.
struct SignalPath {
  std::vector<SignalStep> steps;
  /// What the steps hold back ahead of what is heard.
  double latencyMs = 0;

  bool operator==(const SignalPath &) const = default;
};
