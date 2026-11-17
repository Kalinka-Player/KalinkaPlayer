#include "dsp/dsd/ByteHistory.h"

#include <gtest/gtest.h>

#include <vector>

namespace {
constexpr size_t kCarried = 3, kCapacity = 16;

std::vector<uint8_t> channel(const ByteHistory &history, unsigned c,
                             size_t bytes, bool reversed = false) {
  const uint8_t *from = reversed ? history.reversed(c) : history.bytes(c);
  return {from, from + kCarried + bytes};
}
} // namespace

TEST(ByteHistory, EachChannelGetsItsOwnBytesAfterWhatWasCarried) {
  for (const unsigned channels : {1u, 2u, 3u}) {
    ByteHistory history(channels, kCarried, kCapacity, DsdBitOrder::MsbFirst,
                        true);
    std::vector<uint8_t> first, second;
    for (size_t m = 0; m < 10; ++m)
      for (unsigned c = 0; c < channels; ++c) {
        first.push_back(uint8_t(16 * c + m));
        second.push_back(uint8_t(0x80 + 16 * c + m));
      }
    history.append(first.data(), 10);
    history.advance(10);
    history.append(second.data(), 10);
    for (unsigned c = 0; c < channels; ++c) {
      std::vector<uint8_t> want;
      for (size_t m = 7; m < 10; ++m)
        want.push_back(uint8_t(16 * c + m));
      for (size_t m = 0; m < 10; ++m)
        want.push_back(uint8_t(0x80 + 16 * c + m));
      EXPECT_EQ(channel(history, c, 10), want) << channels << " channels";
      for (auto &byte : want)
        byte = kBitReversed[byte];
      EXPECT_EQ(channel(history, c, 10, true), want) << channels << " channels";
    }
  }
}

TEST(ByteHistory, ResetCarriesTheIdlePatternInTheStreamsOrder) {
  ByteHistory msb(2, kCarried, kCapacity, DsdBitOrder::MsbFirst, false);
  EXPECT_EQ(channel(msb, 1, 0), std::vector<uint8_t>(kCarried, 0x69));
  EXPECT_EQ(msb.reversed(1), nullptr);
  ByteHistory lsb(2, kCarried, kCapacity, DsdBitOrder::LsbFirst, false);
  EXPECT_EQ(channel(lsb, 0, 0),
            std::vector<uint8_t>(kCarried, kBitReversed[0x69]));
}
