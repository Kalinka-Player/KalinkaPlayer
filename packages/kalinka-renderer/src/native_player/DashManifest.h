#pragma once

#include <cstdint>
#include <span>
#include <string>
#include <vector>

/// @brief A bounded, static audio SegmentTemplate timeline; URLs remain
/// private.
struct DashManifest {
  struct Segment {
    uint64_t time, duration, number;
  };
  std::string initialization;
  std::string media;
  std::string baseUrl;
  std::string representation;
  uint64_t bandwidth = 0;
  uint64_t timescale = 1;
  std::vector<Segment> segments;

  static DashManifest parse(std::span<const uint8_t> xml,
                            const std::string &manifestUrl);
  std::string segmentUrl(size_t index) const;
};
