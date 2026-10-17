#pragma once

#include "Mp4.h"

#include <cstdint>
#include <span>
#include <vector>

/// @brief The audio track and sample tables of unencrypted fragmented MP4.
struct FragmentedMp4 {
  /// The samples of one track fragment, in decoding order.
  struct Fragment {
    /// The first sample's decode time, in track timescale units.
    uint64_t time = 0;
    std::vector<uint32_t> durations, sizes;
    /// The samples' bytes back to back, as `sizes` divides them.
    std::vector<uint8_t> data;
  };
  /// Where a fragment starts in the file and when, in track timescale units.
  struct Reference {
    uint64_t offset = 0, time = 0, duration = 0;
  };
  mp4::AudioTrack track;
  uint32_t defaultDuration = 0, defaultSize = 0;

  /// Reads a `moov` box that announces movie fragments.
  static FragmentedMp4 parseInitialization(std::span<const uint8_t> bytes);
  /// Reads whole `moof` and `mdat` boxes, as a media segment holds them.
  std::vector<Fragment> parseSegments(std::span<const uint8_t> bytes) const;
  /// The fragments a `sidx` box's payload lists for this track; empty when
  /// it counts in another timescale or points at further indexes.
  /// @param anchor The file offset just past the `sidx` box.
  std::vector<Reference> parseIndex(std::span<const uint8_t> sidx,
                                    uint64_t anchor) const;
  /// When the fragment a `moof` box's payload describes starts and how
  /// long it lasts, without its media; `offset` is left 0.
  Reference timing(std::span<const uint8_t> moof) const;
  /// The fragments an `mfra` box's payload lists for this track, without
  /// durations; empty when it lists none.
  std::vector<Reference> parseRandomAccess(std::span<const uint8_t> mfra) const;
};
