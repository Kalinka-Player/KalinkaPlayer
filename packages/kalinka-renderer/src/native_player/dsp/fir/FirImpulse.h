#pragma once

#include <array>
#include <filesystem>
#include <string_view>
#include <vector>

/// Coefficients from REW's filter impulse WAV, at their original gain and rate.
/// Mono uses the first channel for both speakers.
struct FirImpulse {
  unsigned rate;
  std::array<std::vector<double>, 2> channels;
  static unsigned sampleRate(const std::filesystem::path &path);
  static FirImpulse decode(std::string_view wav);
  static FirImpulse load(const std::filesystem::path &path);
};
