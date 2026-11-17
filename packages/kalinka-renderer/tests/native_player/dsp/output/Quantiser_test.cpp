#include "dsp/output/Quantiser.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <map>
#include <vector>

namespace {
constexpr size_t kFrames = 1000;

/// @p frames frames of one constant value on every channel.
struct Planar {
  Planar(unsigned channels, size_t frames, double value)
      : data(channels, std::vector<double>(frames, value)) {
    for (auto &channel : data)
      planes.push_back(channel.data());
  }
  std::vector<std::vector<double>> data;
  std::vector<const double *> planes;
};

std::vector<int32_t> packed(Quantiser &quantiser, const Planar &in,
                            GainRamp::Span gain, AudioSampleFormat format,
                            unsigned bits) {
  const unsigned channels = unsigned(in.data.size());
  std::vector<uint8_t> bytes(gain.frames * channels * sampleSize(format));
  quantiser.pack(in.planes.data(), channels, gain.frames, gain, format, bits,
                 bytes.data());
  std::vector<int32_t> samples;
  for (size_t at = 0; at < bytes.size(); at += sampleSize(format)) {
    int32_t value = 0;
    switch (format) {
    case PCM16_LE: {
      int16_t v;
      std::memcpy(&v, &bytes[at], 2);
      value = v;
      break;
    }
    case PCM24_3LE:
      value = int32_t(uint32_t(bytes[at]) << 8 | uint32_t(bytes[at + 1]) << 16 |
                      uint32_t(bytes[at + 2]) << 24) >>
              8;
      break;
    default:
      std::memcpy(&value, &bytes[at], 4);
    }
    samples.push_back(value);
  }
  return samples;
}

GainRamp::Span hold(double gain, size_t frames = kFrames) {
  return {gain, gain, frames};
}
} // namespace

TEST(Quantiser, SilenceDitheredRoundsToOneLsbEitherSideOneTimeInFour) {
  Quantiser quantiser(100'000, 1, 3);
  std::map<int32_t, size_t> counts;
  for (const int32_t v : packed(quantiser, Planar(1, 100'000, 0.0),
                                hold(0.5, 100'000), PCM16_LE, 16))
    ++counts[v];
  ASSERT_EQ(counts.size(), 3u);
  EXPECT_NEAR(double(counts[-1]) / 1e5, 0.125, 0.005);
  EXPECT_NEAR(double(counts[0]) / 1e5, 0.75, 0.005);
  EXPECT_NEAR(double(counts[1]) / 1e5, 0.125, 0.005);
}

TEST(Quantiser, EachContainerCarriesItsBitsAtTheTop) {
  // A quarter of full scale is a whole number of LSBs in every format.
  struct Case {
    AudioSampleFormat format;
    unsigned bits;
    int32_t quarter;
    int32_t lsb;
  };
  for (const auto &c :
       {Case{PCM16_LE, 16, 1 << 13, 1}, Case{PCM24_LE, 24, 1 << 21, 1},
        Case{PCM24_3LE, 24, 1 << 21, 1}, Case{PCM32_LE, 24, 1 << 29, 1 << 8},
        Case{PCM32_LE, 32, 1 << 29, 1}}) {
    Quantiser quantiser(kFrames, 2, 4);
    for (const int32_t v : packed(quantiser, Planar(2, kFrames, 0.5), hold(0.5),
                                  c.format, c.bits)) {
      EXPECT_LE(std::abs(v - c.quarter), c.lsb)
          << sampleFormatToString(c.format) << " at " << c.bits;
      EXPECT_EQ(v % c.lsb, 0) << "bits below the significant ones are zero";
    }
  }
}

TEST(Quantiser, EachChannelLandsInItsOwnSlotOfEveryFrame) {
  struct Case {
    AudioSampleFormat format;
    unsigned bits;
    int32_t lsb;
  };
  const double levels[] = {0.5, -0.25, 0.125};
  for (const auto &c : {Case{PCM16_LE, 16, 1 << 16}, Case{PCM24_LE, 24, 1 << 8},
                        Case{PCM24_3LE, 24, 1 << 8}, Case{PCM32_LE, 24, 1 << 8},
                        Case{PCM32_LE, 32, 1}}) {
    Quantiser quantiser(kFrames, 3, 10);
    Planar in(3, kFrames, 0.0);
    for (unsigned ch = 0; ch < 3; ++ch)
      std::ranges::fill(in.data[ch], levels[ch]);
    const auto samples = packed(quantiser, in, hold(1.0), c.format, c.bits);
    ASSERT_EQ(samples.size(), 3 * kFrames);
    // Read back at the scale of 32 bits, where a quarter is 1 << 29.
    const int shift = c.format == PCM16_LE ? 16 : c.format == PCM32_LE ? 0 : 8;
    for (size_t i = 0; i < samples.size(); ++i) {
      const double want = levels[i % 3] * 2147483648.0;
      EXPECT_LE(std::abs(double(int64_t(samples[i]) << shift) - want), c.lsb)
          << sampleFormatToString(c.format) << " at " << c.bits << ", sample "
          << i;
    }
  }
}

TEST(Quantiser, PastFullScaleClampsAndIsCounted) {
  Quantiser quantiser(kFrames, 1, 5);
  for (const int32_t v :
       packed(quantiser, Planar(1, kFrames, 1.5), hold(1.0), PCM16_LE, 16))
    EXPECT_EQ(v, 32767);
  EXPECT_EQ(quantiser.clipped(), kFrames);
  for (const int32_t v :
       packed(quantiser, Planar(1, kFrames, -1.5), hold(1.0), PCM16_LE, 16))
    EXPECT_EQ(v, -32768);
  EXPECT_EQ(quantiser.clipped(), 2 * kFrames);
}

TEST(Quantiser, EveryChannelOfAFrameGetsTheSameGain) {
  Quantiser quantiser(kFrames, 2, 6);
  Planar in(2, kFrames, 0.5);
  std::vector<double> out(kFrames * 2);
  const GainRamp::Span ramp{1.0, 0.0, kFrames};
  quantiser.pack(in.planes.data(), 2, kFrames, ramp, PCM_FLOAT64_LE, 64,
                 out.data());
  for (size_t i = 0; i < kFrames; ++i) {
    const double gain = 1.0 - double(i + 1) / double(kFrames);
    EXPECT_EQ(out[2 * i], out[2 * i + 1]);
    EXPECT_NEAR(out[2 * i], 0.5 * gain, 1e-15);
  }
  EXPECT_NEAR(out.back(), 0.0, 1e-15) << "the ramp ends on its last frame";
}

TEST(Quantiser, FloatIsScaledWithoutDitherOrRounding) {
  Quantiser quantiser(4, 1, 7);
  const double values[] = {0.125, -0.5, 0x1p-30, 0x1.0000000000001p-1};
  const double *planes[] = {values};
  std::vector<float> single(4);
  quantiser.pack(planes, 1, 4, hold(0.5, 4), PCM_FLOAT32_LE, 32, single.data());
  EXPECT_EQ(single, (std::vector<float>{0.0625f, -0.25f, 0x1p-31f, 0.25f}));
  std::vector<double> wide(4);
  quantiser.pack(planes, 1, 4, hold(0.5, 4), PCM_FLOAT64_LE, 64, wide.data());
  EXPECT_EQ(wide, (std::vector<double>{0.0625, -0.25, 0x1p-31,
                                       0x1.0000000000001p-2}));
}

TEST(Quantiser, AGainHeldAtZeroIsDigitalSilence) {
  Quantiser quantiser(kFrames, 2, 8);
  for (const int32_t v :
       packed(quantiser, Planar(2, kFrames, 0.7), hold(0.0), PCM32_LE, 24))
    EXPECT_EQ(v, 0);
}

TEST(Quantiser, ASeedGivesTheSameOutputEveryTime) {
  Quantiser a(kFrames, 2, 9), b(kFrames, 2, 9);
  const Planar in(2, kFrames, 0.3);
  EXPECT_EQ(packed(a, in, hold(0.5), PCM16_LE, 16),
            packed(b, in, hold(0.5), PCM16_LE, 16));
}
