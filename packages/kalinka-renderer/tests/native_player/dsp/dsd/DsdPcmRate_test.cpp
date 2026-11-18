#include "dsp/dsd/DsdPcmRate.h"

#include <gtest/gtest.h>

#include <stdexcept>
#include <vector>

namespace {
constexpr unsigned kDsd64 = 2822400;
constexpr unsigned kDsd64At48 = 3072000;

/// A device taking S32 in stereo at each of @p rates.
std::vector<StreamAudioFormat> takes(std::initializer_list<unsigned> rates) {
  std::vector<StreamAudioFormat> device;
  for (const unsigned rate : rates)
    device.push_back({rate, 2, 24, PCM32_LE, 0});
  return device;
}
} // namespace

TEST(DsdPcmRate, AutomaticTakesFourTimesTheBaseThenEightThenTwoThenOne) {
  EXPECT_EQ(dsdPcmRate(kDsd64, takes({88200, 176400, 352800})), 176400u);
  EXPECT_EQ(dsdPcmRate(kDsd64, takes({88200, 352800})), 352800u);
  EXPECT_EQ(dsdPcmRate(kDsd64, takes({44100, 88200})), 88200u);
  EXPECT_EQ(dsdPcmRate(kDsd64, takes({44100, 48000})), 44100u);
  EXPECT_EQ(dsdPcmRate(2 * kDsd64At48, takes({44100, 48000, 96000})), 96000u);
}

TEST(DsdPcmRate, TheRateAskedForWhereTheOutputTakesIt) {
  const auto device = takes({44100, 88200, 176400});
  EXPECT_EQ(dsdPcmRate(kDsd64, device, 1), 44100u);
  EXPECT_EQ(dsdPcmRate(kDsd64, device, 2), 88200u);
  EXPECT_EQ(dsdPcmRate(kDsd64, device, 8), 176400u) << "not taken: automatic";
}

TEST(DsdPcmRate, NothingKnownOfTheOutputIsLeftToOpeningIt) {
  EXPECT_EQ(dsdPcmRate(kDsd64, {}), 176400u);
  EXPECT_EQ(dsdPcmRate(kDsd64, {}, 8), 352800u);
}

TEST(DsdPcmRate, AnOutputTakingNoRateOfTheFamilyTakesNone) {
  EXPECT_EQ(dsdPcmRate(kDsd64, takes({48000, 96000, 192000})), std::nullopt);
  EXPECT_EQ(dsdPcmRate(kDsd64, takes({48000}), 1), std::nullopt);
  EXPECT_EQ(dsdPcmRate(kDsd64At48, takes({44100, 48000})), 48000u);
}

TEST(DsdPcmRate, OnlyStereoPcmCounts) {
  const std::vector<StreamAudioFormat> device{
      {176400, 1, 24, PCM32_LE, 0}, {88200, 2, 1, DSD_U32_LE, kDsd64}};
  EXPECT_EQ(dsdPcmRate(kDsd64, device), std::nullopt);
}

TEST(DsdPcmRate, TheSettingNamesAMultipleOrAutomatic) {
  EXPECT_EQ(dsdPcmMultiple("auto"), std::nullopt);
  EXPECT_EQ(dsdPcmMultiple("1x"), 1u);
  EXPECT_EQ(dsdPcmMultiple("8x"), 8u);
  EXPECT_THROW(dsdPcmMultiple("3x"), std::invalid_argument);
  EXPECT_THROW(dsdPcmMultiple("16x"), std::invalid_argument);
  for (const char *value : {"auto", "1x", "2x", "4x", "8x"})
    EXPECT_TRUE(isDsdPcmRate(value)) << value;
  for (const char *value : {"", "3x", "4", "x4", "AUTO"})
    EXPECT_FALSE(isDsdPcmRate(value)) << value;
}

TEST(DsdPcmRate, TheRatesAreNamedForTheFamily) {
  EXPECT_EQ(dsdPcmRatesText(kDsd64), "44.1, 88.2, 176.4 or 352.8 kHz");
  EXPECT_EQ(dsdPcmRatesText(kDsd64At48), "48, 96, 192 or 384 kHz");
}
