#pragma once

#include "Mp4.h"

#include <cstdint>
#include <span>
#include <vector>

/// @brief Where each access unit of a progressive MP4's audio track lies in
/// the file and when it plays.
struct Mp4SampleTable {
  mp4::AudioTrack track;
  /// Absolute file offsets and sizes, in decoding order.
  std::vector<uint64_t> offsets;
  std::vector<uint32_t> sizes;
  /// Decode times in track timescale units; `end` follows the last unit.
  std::vector<uint64_t> times;
  uint64_t end = 0;

  /// Reads a whole `moov` box, header included, whose track is not
  /// fragmented. Throws for an index that is inconsistent or too large.
  static Mp4SampleTable parse(std::span<const uint8_t> moov);
};
