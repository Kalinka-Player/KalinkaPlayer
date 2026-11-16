#pragma once

#include "dsp/dsd/DsdOptions.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

/// Each byte with its bits in the opposite order.
inline constexpr std::array<uint8_t, 256> kBitReversed = [] {
  std::array<uint8_t, 256> table{};
  for (unsigned byte = 0; byte < 256; ++byte)
    for (unsigned bit = 0; bit < 8; ++bit)
      table[byte] |= uint8_t(((byte >> bit) & 1) << (7 - bit));
  return table;
}();

/**
 * @brief Each channel's DSD bytes for stage 1: the last bytes of the block
 * before, then the block being converted, so a group can reach back across
 * the block's start.
 *
 * Bytes stay in the stream's bit order. Asked for at construction, each
 * channel also keeps them with their bits reversed, for tables built in the
 * other order. Kernels may read dsd_kernels::kByteSlack bytes past a block.
 * @note Not thread-safe. Allocates only when constructed.
 */
class ByteHistory {
public:
  /**
   * @param carried Bytes each block keeps from the one before.
   * @param capacity The most bytes per channel one block holds.
   */
  ByteHistory(unsigned channels, size_t carried, size_t capacity,
              DsdBitOrder order, bool keepReversed);

  /// Takes @p bytes bytes of each channel, interleaved one byte per channel,
  /// as the next block.
  void append(const uint8_t *in, size_t bytes);
  /// Keeps the last carried bytes of the @p bytes appended for the next block.
  void advance(size_t bytes);
  /// Forgets every byte, as if all before had been the idle pattern 0x69.
  void reset();

  /// Channel @p c's carried bytes, then the block's.
  const uint8_t *bytes(unsigned c) const { return direct[c].data(); }
  /// The same with their bits reversed, or null unless kept.
  const uint8_t *reversed(unsigned c) const {
    return mirrored.empty() ? nullptr : mirrored[c].data();
  }

private:
  size_t carried;
  /// The idle pattern in time order, whichever bit comes first.
  uint8_t idle;
  std::vector<std::vector<uint8_t>> direct, mirrored;
};
