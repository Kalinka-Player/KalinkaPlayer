#include "dsp/dsd/DsdToPcmProcessor.h"

#include <gtest/gtest.h>

#include <random>
#include <vector>

namespace {
/// Raw DSD as DsdContainer hands it in `pcm` mode.
StreamAudioFormat raw(unsigned dsdRate, unsigned channels = 2) {
  return {dsdRate / 8, channels, 1, DSD_U8, dsdRate};
}

/// A device taking S32 in stereo at each of @p rates.
std::vector<StreamAudioFormat> takes(std::initializer_list<unsigned> rates) {
  std::vector<StreamAudioFormat> device;
  for (const unsigned rate : rates)
    device.push_back({rate, 2, 24, PCM32_LE, 0});
  return device;
}

std::vector<uint8_t> randomBytes(size_t count) {
  std::mt19937 random(1);
  std::vector<uint8_t> bytes(count);
  for (auto &byte : bytes)
    byte = uint8_t(random());
  return bytes;
}
} // namespace

TEST(DsdToPcmProcessor, TakesRawDsdAndNothingElse) {
  const DsdToPcmProcessor processor({}, 1.0);
  EXPECT_TRUE(processor.handles(raw(2822400)));
  EXPECT_TRUE(processor.handles(raw(3072000, 1)));
  EXPECT_FALSE(processor.handles({352800, 2, 32, DSD_U32_LE, 2822400}));
  EXPECT_FALSE(processor.handles({44100, 2, 16, PCM16_LE, 0}));
  EXPECT_FALSE(processor.handles(raw(2822400 / 2))) << "below DSD64";
}

TEST(DsdToPcmProcessor, FourTimesTheBaseWhereTheDeviceTakesIt) {
  const auto rate = [](std::vector<StreamAudioFormat> device, unsigned dsd) {
    return DsdToPcmProcessor(std::move(device), 1.0)
        .output(raw(dsd))
        .format.sampleRate;
  };
  EXPECT_EQ(rate(takes({88200, 176400, 352800}), 2822400), 176400u);
  EXPECT_EQ(rate(takes({88200, 352800}), 2822400), 352800u);
  EXPECT_EQ(rate(takes({88200}), 2822400), 88200u);
  EXPECT_EQ(rate({}, 2822400), 176400u) << "nothing known";
  EXPECT_EQ(rate(takes({96000, 192000}), 6144000), 192000u);
}

TEST(DsdToPcmProcessor, TheOutputIsFloatAtTheRateWithTheRatioOfFrames) {
  const auto out = DsdToPcmProcessor({}, 1.0).output(raw(5644800));
  EXPECT_EQ(out.format.sampleFormat, PCM_FLOAT64_LE);
  EXPECT_EQ(out.format.channels, 2u);
  EXPECT_EQ(out.format.sampleRate, 176400u);
  EXPECT_EQ(out.ratio, 4u) << "four DSD128 bytes a channel to a frame";
}

TEST(DsdToPcmProcessor, ItConvertsAsTheConverterDoesTimesTheGain) {
  constexpr double kGain = 1.25;
  // DSD128 takes the 50 kHz filter, and the latency is the converter's.
  DsdToPcm reference({5644800, 176400, 2, DsdFilter::Cut50k,
                      DsdBitOrder::MsbFirst});
  DsdToPcmProcessor processor({}, kGain);
  EXPECT_EQ(processor.start(raw(5644800)), reference.latency());

  const auto in = randomBytes(2 * 6000);
  std::vector<double> left(reference.maxFrames(6000)), right(left.size());
  double *planes[] = {left.data(), right.data()};
  const size_t expected = reference.process(in.data(), 6000, planes);
  std::vector<double> out(2 * (expected + 1));
  ASSERT_EQ(processor.process(in.data(), reinterpret_cast<uint8_t *>(out.data()),
                              6000),
            expected);
  for (size_t i = 0; i < expected; ++i) {
    EXPECT_EQ(out[2 * i], left[i] * kGain);
    EXPECT_EQ(out[2 * i + 1], right[i] * kGain);
  }
}

TEST(DsdToPcmProcessor, FedSilenceSettlesToZero) {
  DsdToPcmProcessor processor({}, 1.0);
  processor.start(raw(2822400));
  const auto in = randomBytes(2 * 2000);
  std::vector<double> out(2 * 2000);
  auto *bytes = reinterpret_cast<uint8_t *>(out.data());
  processor.process(in.data(), bytes, 2000);
  processor.process(nullptr, bytes, 2000);
  const size_t made = processor.process(nullptr, bytes, 2000);
  ASSERT_EQ(made, 1000u);
  for (size_t i = 0; i < 2 * made; ++i)
    EXPECT_EQ(out[i], 0.0);
}
