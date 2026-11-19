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
  for (const char *value : {"", "4", "x4", "AUTO"})
    EXPECT_THROW(dsdPcmMultiple(value), std::invalid_argument) << value;
}

TEST(DsdPcmRate, TheFamilyIsTheBaseRateADsdRateIsAMultipleOf) {
  EXPECT_EQ(dsdBaseRate(kDsd64), 44100u);
  EXPECT_EQ(dsdBaseRate(16 * kDsd64), 44100u);
  EXPECT_EQ(dsdBaseRate(kDsd64At48), 48000u);
}

TEST(DsdPcmRate, TheRatesAreNamedForTheFamily) {
  EXPECT_EQ(dsdPcmRatesText(kDsd64), "44.1, 88.2, 176.4 or 352.8 kHz");
  EXPECT_EQ(dsdPcmRatesText(kDsd64At48), "48, 96, 192 or 384 kHz");
}

TEST(DsdPcmRate, ADsdRateIsNamedByItsMultiple) {
  EXPECT_EQ(dsdRateName(kDsd64), "DSD64");
  EXPECT_EQ(dsdRateName(4 * kDsd64), "DSD256");
  EXPECT_EQ(dsdRateName(kDsd64At48), "DSD64");
  EXPECT_EQ(dsdRateName(3000000), "DSD at 3000000 Hz");
}

TEST(DsdPcmRate, APcmRateIsNamedInKiloHertz) {
  EXPECT_EQ(pcmRateText(176400), "176.4 kHz");
  EXPECT_EQ(pcmRateText(44100), "44.1 kHz");
  EXPECT_EQ(pcmRateText(48000), "48 kHz");
  EXPECT_EQ(pcmRateText(384000), "384 kHz");
}
