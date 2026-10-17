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

  /// Reads the units a whole `moov` box, header included, lists; a
  /// fragmented movie's fragments follow them. Throws for an index that is
  /// inconsistent or too large.
  static Mp4SampleTable parse(std::span<const uint8_t> moov);
  /// Whether a `moov` box's sample tables list any audio units, as a
  /// fragmented movie's may for those before its first fragment.
  static bool listsSamples(std::span<const uint8_t> moov);
};
