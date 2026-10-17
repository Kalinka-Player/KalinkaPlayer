#pragma once

#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <string>

/// @brief HTTP routes for the generated segmented FLAC fixtures.
inline auto dashFixtures(int bits) {
  std::map<std::string, std::string, std::less<>> bodies;
  const auto directory = std::filesystem::path(KALINKA_TEST_DATA_DIR) / "dash" /
                         std::to_string(bits);
  for (const auto &entry : std::filesystem::directory_iterator(directory)) {
    std::ifstream in(entry.path(), std::ios::binary);
    bodies["/" + entry.path().filename().string()] =
        std::string(std::istreambuf_iterator<char>(in), {});
  }
  return bodies;
}
