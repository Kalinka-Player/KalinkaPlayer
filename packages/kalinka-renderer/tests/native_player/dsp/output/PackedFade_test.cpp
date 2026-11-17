#include "dsp/output/PackedFade.h"

#include <gtest/gtest.h>

#include <cmath>
#include <cstring>
#include <vector>

namespace {
constexpr unsigned kRate = 48000;
// 10 ms at kRate.
constexpr size_t kLength = 480;

/// The gain the OutputStage's fade gives frame @p frame of a fade to silence.
double fallen(size_t frame) {
  return 1.0 - double(frame + 1) / double(kLength);
}

template <class T> std::vector<uint8_t> asBytes(const std::vector<T> &values) {
  std::vector<uint8_t> bytes(values.size() * sizeof(T));
  std::memcpy(bytes.data(), values.data(), bytes.size());
  return bytes;
}

template <class T> std::vector<T> asSamples(const std::vector<uint8_t> &bytes) {
  std::vector<T> values(bytes.size() / sizeof(T));
  std::memcpy(values.data(), bytes.data(), bytes.size());
  return values;
}
} // namespace

TEST(PackedFade, FallsToSilenceOverTheFadeAsTheStageWould) {
  constexpr int32_t kHalf = 1 << 30;
  auto bytes = asBytes(std::vector<int32_t>(2 * kLength, kHalf));
  PackedFade fade(1);
  fade.begin(kRate);
  fade.out(bytes.data(), PCM32_LE, 2, 24, kLength);
  const auto out = asSamples<int32_t>(bytes);
  for (size_t i = 0; i < kLength; ++i)
    for (unsigned c = 0; c < 2; ++c) {
      const int32_t v = out[2 * i + c];
      ASSERT_EQ(v % 256, 0) << "dithered at 24 bits, frame " << i;
      ASSERT_LE(std::abs(double(v / 256) - fallen(i) * double(kHalf / 256)),
                1.5)
          << "frame " << i;
    }
}

TEST(PackedFade, AFadeWrittenInPiecesIsOneFade) {
  constexpr int16_t kLevel = 1 << 14;
  auto bytes = asBytes(std::vector<int16_t>(2 * kLength, kLevel));
  PackedFade fade(1);
  fade.begin(kRate);
  // Where a device's ring wraps: the rest of the fade comes in a second call.
  fade.out(bytes.data(), PCM16_LE, 2, 16, 100);
  fade.out(bytes.data() + 100 * 4, PCM16_LE, 2, 16, kLength - 100);
  const auto out = asSamples<int16_t>(bytes);
  for (size_t i = 0; i < kLength; ++i)
    ASSERT_LE(std::abs(double(out[2 * i]) - fallen(i) * kLevel), 1.5)
        << "frame " << i;
}

TEST(PackedFade, ThreeByteSamplesFadeInPlace) {
  constexpr int32_t kLevel = 1 << 22;
  std::vector<uint8_t> bytes(kLength * 2 * 3);
  for (size_t s = 0; s < kLength * 2; ++s) {
    bytes[3 * s] = uint8_t(kLevel);
    bytes[3 * s + 1] = uint8_t(kLevel >> 8);
    bytes[3 * s + 2] = uint8_t(kLevel >> 16);
  }
  PackedFade fade(1);
  fade.begin(kRate);
  fade.out(bytes.data(), PCM24_3LE, 2, 24, kLength);
  for (size_t i = 0; i < kLength; ++i) {
    const uint8_t *at = bytes.data() + 6 * i;
    const int32_t v = int32_t(uint32_t(at[0]) << 8 | uint32_t(at[1]) << 16 |
                              uint32_t(at[2]) << 24) >>
                      8;
    ASSERT_LE(std::abs(double(v) - fallen(i) * kLevel), 1.5) << "frame " << i;
  }
}

TEST(PackedFade, PastItsEndItSilences) {
  auto bytes = asBytes(std::vector<int16_t>(2 * 2 * kLength, 1 << 14));
  PackedFade fade(1);
  fade.begin(kRate);
  fade.out(bytes.data(), PCM16_LE, 2, 16, 2 * kLength);
  const auto out = asSamples<int16_t>(bytes);
  for (size_t i = kLength; i < 2 * kLength; ++i)
    ASSERT_EQ(out[2 * i], 0) << "frame " << i;
}

TEST(PackedFade, RefusesDsd) {
  std::vector<uint8_t> bytes(64, 0x69);
  PackedFade fade(1);
  fade.begin(kRate);
  EXPECT_THROW(fade.out(bytes.data(), DSD_U8, 2, 1, 32), std::invalid_argument);
}
