#pragma once

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <string>
#include <vector>

/// @brief HTTP routes for a generated DASH fixture: `dash/<name>`.
inline auto dashFixtures(const std::string &name) {
  std::map<std::string, std::string, std::less<>> bodies;
  const auto directory =
      std::filesystem::path(KALINKA_TEST_DATA_DIR) / "dash" / name;
  for (const auto &entry : std::filesystem::directory_iterator(directory)) {
    std::ifstream in(entry.path(), std::ios::binary);
    bodies["/" + entry.path().filename().string()] =
        std::string(std::istreambuf_iterator<char>(in), {});
  }
  return bodies;
}

/// @brief The segmented FLAC fixture of 16 or 24 bits.
inline auto dashFixtures(int bits) {
  return dashFixtures(std::to_string(bits));
}

/// @brief The stereo ramp every lossless fixture encodes, as little-endian
/// PCM of `bits` from frame `first`; 24 bits sit in 4-byte slots.
inline std::vector<uint8_t> rampPcm(int bits, size_t first = 0) {
  const size_t rate = bits == 16 ? 44100 : 96000;
  std::vector<uint8_t> bytes;
  for (size_t i = first; i < rate * 5 / 4; ++i)
    for (int channel = 0; channel < 2; ++channel) {
      int32_t value = (int(i % 503) - 251) * 57 * (channel ? -1 : 1);
      if (bits == 24)
        value = (value * 256 + int(i % 127) - 63) & 0xffffff;
      for (int b = 0; b < (bits == 16 ? 2 : 4); ++b)
        bytes.push_back(uint32_t(value) >> (8 * b));
    }
  return bytes;
}
