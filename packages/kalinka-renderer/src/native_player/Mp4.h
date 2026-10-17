#pragma once

#include <cstdint>
#include <span>
#include <string_view>
#include <vector>

/// ISO base media file format (MP4) boxes, read with every size checked.
namespace mp4 {
using Bytes = std::span<const uint8_t>;

/// @brief One box: its four-character type and the payload after its header.
struct Box {
  std::string_view type;
  Bytes payload;
  /// Where the box starts in the bytes its parent was parsed from.
  size_t offset, header;
};

/// Throws the error every malformed or unsupported MP4 gets.
[[noreturn]] void invalid();
/// The big-endian integer of `width` bytes at `offset`; throws when outside.
uint64_t integer(Bytes bytes, size_t offset, size_t width);
/// The boxes that tile `bytes` exactly; offsets count from `origin`.
std::vector<Box> boxes(Bytes bytes, size_t origin = 0);
/// The only box of `type` among `items`; throws unless there is exactly one.
Box one(const std::vector<Box> &items, std::string_view type);
/// The only child of `type` inside `box`.
Box inside(const Box &box, std::string_view type);
/// Throws if any of `items` marks protected (encrypted) content.
void refuseEncrypted(const std::vector<Box> &items);

/// @brief The audio track of a movie and what decoding it needs.
struct AudioTrack {
  enum class Codec { FLAC, AAC };
  Codec codec = Codec::FLAC;
  /// FLAC: the 34-byte STREAMINFO block. AAC: the AudioSpecificConfig.
  std::vector<uint8_t> config;
  uint32_t id = 0, timescale = 0;
  /// The track's length in `timescale` units, or 0 when the file does not say.
  uint64_t duration = 0;
};

/// The first sound track (`trak`) of a `moov` box; throws when it has none.
Box soundTrack(const Box &moov);

/// @brief The first sound track of a `moov` box.
/// @note Throws for a movie without one, a codec other than FLAC or AAC, or
/// encrypted audio.
AudioTrack audioTrack(const Box &moov);
} // namespace mp4
