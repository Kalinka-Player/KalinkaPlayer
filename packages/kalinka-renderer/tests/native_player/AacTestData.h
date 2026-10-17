#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

/// @brief The bytes of `tests/data/aac/<name>`.
inline std::vector<uint8_t> aacFixture(const std::string &name) {
  std::ifstream in(std::filesystem::path(KALINKA_TEST_DATA_DIR) / "aac" / name,
                   std::ios::binary);
  return {std::istreambuf_iterator<char>(in), {}};
}

/// @brief The path of `tests/data/aac/<name>`.
inline std::string aacFixturePath(const std::string &name) {
  return (std::filesystem::path(KALINKA_TEST_DATA_DIR) / "aac" / name).string();
}

/// The tones fixture's length: 1.25 s at 44.1 kHz, every container alike.
constexpr size_t TONES_FRAMES = 55125;

/// @brief How closely 16-bit stereo `pcm` from frame `first` follows the
/// tones the AAC fixtures encode, as a signal-to-noise ratio in dB.
inline double tonesSnr(const std::vector<uint8_t> &pcm, size_t first = 0) {
  double signal = 0, noise = 0;
  for (size_t i = 0; i + 4 <= pcm.size(); i += 4) {
    const auto frame = first + i / 4;
    for (int channel = 0; channel < 2; ++channel) {
      const auto actual =
          int16_t(pcm[i + 2 * channel] | pcm[i + 2 * channel + 1] << 8);
      const auto ideal =
          std::trunc(12000 * std::sin(2 * M_PI * (channel ? 660 : 440) *
                                      double(frame) / 44100));
      signal += ideal * ideal;
      noise += (actual - ideal) * (actual - ideal);
    }
  }
  return 10 * std::log10(signal / std::max(noise, 1.0));
}

/// @brief The largest difference between two equally long 16-bit PCM blocks.
inline int maxDifference(const std::vector<uint8_t> &a,
                         const std::vector<uint8_t> &b) {
  int largest = a.size() == b.size() ? 0 : 65536;
  for (size_t i = 0; i + 1 < std::min(a.size(), b.size()); i += 2)
    largest = std::max(largest, std::abs(int16_t(a[i] | a[i + 1] << 8) -
                                         int16_t(b[i] | b[i + 1] << 8)));
  return largest;
}
