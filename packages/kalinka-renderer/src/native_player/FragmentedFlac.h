#pragma once

#include "StreamState.h"

#include <array>
#include <cstdint>
#include <span>
#include <vector>

/// @brief The sample frame at @p time in @p timescale units.
/// @note Throws unless the time falls exactly on a sample within a week.
uint64_t samplesAt(uint64_t time, uint64_t timescale, unsigned rate);

/// @brief FLAC configuration and sample tables from unencrypted fragmented MP4.
struct FragmentedFlac {
  struct Fragment {
    uint64_t time = 0;
    std::vector<uint32_t> durations;
    std::vector<uint8_t> frames;
  };
  uint32_t trackId = 0, timescale = 0;
  uint32_t defaultDuration = 0, defaultSize = 0;
  StreamAudioFormat format{};
  std::array<uint8_t, 34> streamInfo{};

  static FragmentedFlac parseInitialization(std::span<const uint8_t> bytes);
  std::vector<Fragment> parseSegments(std::span<const uint8_t> bytes) const;
  uint64_t samplePosition(uint64_t time) const;
};
