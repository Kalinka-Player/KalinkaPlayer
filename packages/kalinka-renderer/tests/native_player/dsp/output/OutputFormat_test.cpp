#include "dsp/output/OutputFormat.h"

#include <gtest/gtest.h>

namespace {
StreamAudioFormat pcm(AudioSampleFormat format, unsigned bits,
                      unsigned rate = 44100, unsigned channels = 2) {
  return {rate, channels, bits, format, 0};
}

void expectChoice(const OutputChoice &choice, AudioSampleFormat format,
                  unsigned bits) {
  EXPECT_EQ(choice.format, format) << sampleFormatToString(choice.format);
  EXPECT_EQ(choice.bits, bits);
}
} // namespace

TEST(OutputFormat, TheSourcesOwnFormatWhenTheDeviceTakesIt) {
  const std::vector device{pcm(PCM16_LE, 16), pcm(PCM32_LE, 24)};
  expectChoice(chooseOutput(pcm(PCM16_LE, 16), device, 2), PCM16_LE, 16);
}

TEST(OutputFormat, AWiderContainerWhereTheDeviceLacksTheSourcesOwn) {
  const std::vector device{pcm(PCM16_LE, 16), pcm(PCM32_LE, 24)};
  expectChoice(chooseOutput(pcm(PCM24_3LE, 24), device, 2), PCM32_LE, 24);
}

TEST(OutputFormat, TheBitsAreThoseTheDeviceKeeps) {
  const std::vector device{pcm(PCM32_LE, 24)};
  expectChoice(chooseOutput(pcm(PCM32_LE, 32), device, 2), PCM32_LE, 24);
}

TEST(OutputFormat, OnlyTheSourcesRateAndTheDevicesChannelsCount) {
  const std::vector device{pcm(PCM16_LE, 16, 48000),
                           pcm(PCM16_LE, 16, 44100, 1), pcm(PCM32_LE, 24)};
  expectChoice(chooseOutput(pcm(PCM16_LE, 16, 44100, 1), device, 2), PCM32_LE,
               24);
}

TEST(OutputFormat, ASourceThatDoesNotSayItsBitsLosesNone) {
  const std::vector device{pcm(PCM16_LE, 16), pcm(PCM32_LE, 24)};
  expectChoice(chooseOutput(pcm(PCM24_3LE, 0), device, 2), PCM32_LE, 24);
}

TEST(OutputFormat, WithoutADeviceMatchIntegerPcmKeepsItsDeclaredPrecision) {
  expectChoice(chooseOutput(pcm(PCM24_3LE, 24), {}, 2), PCM24_3LE, 24);
  expectChoice(chooseOutput(pcm(PCM32_LE, 24), {}, 2), PCM32_LE, 24);
  expectChoice(chooseOutput(pcm(PCM32_LE, 0), {}, 2), PCM32_LE, 32);
}

TEST(OutputFormat, FloatStaysFloatWhereTheDeviceTakesIt) {
  const std::vector device{pcm(PCM_FLOAT32_LE, 32), pcm(PCM32_LE, 24)};
  expectChoice(chooseOutput(pcm(PCM_FLOAT32_LE, 32), device, 2),
               PCM_FLOAT32_LE, 32);
}

TEST(OutputFormat, FloatGoesToTheWidestIntegerContainerTheDeviceTakes) {
  const std::vector device{pcm(PCM16_LE, 16), pcm(PCM24_3LE, 24),
                           pcm(PCM32_LE, 24)};
  expectChoice(chooseOutput(pcm(PCM_FLOAT64_LE, 64), device, 2), PCM32_LE,
               24);
  const std::vector narrow{pcm(PCM16_LE, 16)};
  expectChoice(chooseOutput(pcm(PCM_FLOAT64_LE, 64), narrow, 2), PCM16_LE,
               16);
}

TEST(OutputFormat, NothingKnownLeavesFloatAndDsdAsTheyAre) {
  expectChoice(chooseOutput(pcm(PCM_FLOAT64_LE, 64), {}, 2), PCM_FLOAT64_LE,
               64);
  const std::vector device{pcm(PCM32_LE, 24)};
  EXPECT_EQ(chooseOutput(pcm(DSD_U8, 1, 352800), device, 2).format, DSD_U8);
}
