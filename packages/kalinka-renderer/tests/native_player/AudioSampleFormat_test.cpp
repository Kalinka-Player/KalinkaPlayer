#include "AudioSampleFormat.h"

#include <gtest/gtest.h>
#include <vector>

TEST(AudioSampleFormatTest, ConvertPCM16ToPCM16) {
  std::vector<int16_t> sourceSamples = {1000, -1000, 32767, -32768};
  std::vector<int16_t> destSamples(sourceSamples.size());

  size_t convertedSamples = convertSampleFormat(
      sourceSamples.data(), AudioSampleFormat::PCM16_LE, sourceSamples.size(),
      destSamples.data(), AudioSampleFormat::PCM16_LE,
      destSamples.size() * sizeof(int16_t));

  ASSERT_EQ(convertedSamples, sourceSamples.size());
  for (size_t i = 0; i < sourceSamples.size(); ++i) {
    EXPECT_EQ(sourceSamples[i], destSamples[i]) << "i = " << i;
  }
}

TEST(AudioSampleFormatTest, ConvertPCM24ToPCM24) {
  std::vector<int32_t> sourceSamples = {1000000, -1000000, 8388607, -8388608};
  std::vector<int32_t> destSamples(sourceSamples.size());

  size_t convertedSamples = convertSampleFormat(
      sourceSamples.data(), AudioSampleFormat::PCM24_LE, sourceSamples.size(),
      destSamples.data(), AudioSampleFormat::PCM24_LE,
      destSamples.size() * sizeof(int32_t));

  ASSERT_EQ(convertedSamples, sourceSamples.size());
  for (size_t i = 0; i < sourceSamples.size(); ++i) {
    EXPECT_EQ(sourceSamples[i] & 0xFFFFFF, destSamples[i] & 0xFFFFFF)
        << "i = " << i;
  }
}

TEST(AudioSampleFormatTest, ConvertPCM32ToPCM32) {
  std::vector<int32_t> sourceSamples = {100000000, -100000000, 2147483392,
                                        -2147483648};
  std::vector<int32_t> destSamples(sourceSamples.size());

  size_t convertedSamples = convertSampleFormat(
      sourceSamples.data(), AudioSampleFormat::PCM32_LE, sourceSamples.size(),
      destSamples.data(), AudioSampleFormat::PCM32_LE,
      destSamples.size() * sizeof(int32_t));

  ASSERT_EQ(convertedSamples, sourceSamples.size());
  for (size_t i = 0; i < sourceSamples.size(); ++i) {
    EXPECT_EQ(sourceSamples[i], destSamples[i]) << "i = " << i;
  }
}

TEST(AudioSampleFormatTest, ConvertPCM16ToPCM24) {
  std::vector<int16_t> sourceSamples = {1000, -1000, 32767, -32768};
  std::vector<int32_t> destSamples(sourceSamples.size());
  const std::vector<int32_t> expectedSamples = {256000, 16521216, 8388352,
                                                8388608};

  size_t convertedSamples = convertSampleFormat(
      sourceSamples.data(), AudioSampleFormat::PCM16_LE, sourceSamples.size(),
      destSamples.data(), AudioSampleFormat::PCM24_LE,
      destSamples.size() * sizeof(int32_t));

  ASSERT_EQ(convertedSamples, sourceSamples.size());
  for (size_t i = 0; i < sourceSamples.size(); ++i) {
    EXPECT_EQ(destSamples[i], expectedSamples[i]) << "i = " << i;
  }
}

// A DAC that only takes S32 still plays 16-bit bit-perfectly: the sample moves
// to the top 16 bits and nothing below it is invented.
TEST(AudioSampleFormatTest, ConvertPCM16ToPCM32KeepsEveryBit) {
  const std::vector<int16_t> sourceSamples = {1000, -1000, 32767, -32768, -1};
  std::vector<int32_t> destSamples(sourceSamples.size());

  const size_t convertedSamples = convertSampleFormat(
      sourceSamples.data(), AudioSampleFormat::PCM16_LE, sourceSamples.size(),
      destSamples.data(), AudioSampleFormat::PCM32_LE,
      destSamples.size() * sizeof(int32_t));

  ASSERT_EQ(convertedSamples, sourceSamples.size());
  for (size_t i = 0; i < sourceSamples.size(); ++i) {
    EXPECT_EQ(destSamples[i], int32_t{sourceSamples[i]} * 65536) << "i = " << i;
    EXPECT_EQ(destSamples[i] >> 16, sourceSamples[i]) << "i = " << i;
  }
}

TEST(AudioSampleFormatTest, A16BitStreamIsOfferedEveryWiderContainer) {
  std::vector<AudioSampleFormat> tried;
  for (auto format = pcmFallback(PCM16_LE); format;
       format = pcmFallback(*format)) {
    tried.push_back(*format);
  }
  EXPECT_EQ(tried, (std::vector<AudioSampleFormat>{PCM24_LE, PCM32_LE,
                                                   PCM24_3LE}));
}

TEST(AudioSampleFormatTest, DsdHasNoPcmFallback) {
  EXPECT_FALSE(pcmFallback(DSD_U32_BE).has_value());
  EXPECT_FALSE(pcmFallback(DOP32_LE).has_value());
}

TEST(AudioSampleFormatTest, ConvertPCM24ToPCM32) {
  std::vector<int32_t> sourceSamples = {1000000, 0x1000000 - 1000000, 8388607,
                                        0x1000000 - 8388608};
  std::vector<int32_t> destSamples(sourceSamples.size());
  const std::vector<int32_t> expectedSamples = {
      256000000, (0x1000000 - 1000000) << 8, 2147483392, (int32_t)0x80000000u};

  size_t convertedSamples = convertSampleFormat(
      sourceSamples.data(), AudioSampleFormat::PCM24_LE, sourceSamples.size(),
      destSamples.data(), AudioSampleFormat::PCM32_LE,
      destSamples.size() * sizeof(int32_t));

  ASSERT_EQ(convertedSamples, sourceSamples.size());
  for (size_t i = 0; i < sourceSamples.size(); ++i) {
    EXPECT_EQ(destSamples[i], expectedSamples[i]) << "i = " << i;
  }
}

TEST(AudioSampleFormatTest, ConvertPCM32ToPCM24) {
  std::vector<int32_t> sourceSamples = {256000000, (0x1000000 - 1000000) << 8,
                                        2147483392, (int32_t)0x80000000u};
  std::vector<int32_t> destSamples(sourceSamples.size());
  const std::vector<int32_t> expectedSamples = {1000000, 0x1000000 - 1000000,
                                                8388607, 0x1000000 - 8388608};

  size_t convertedSamples = convertSampleFormat(
      sourceSamples.data(), AudioSampleFormat::PCM32_LE, sourceSamples.size(),
      destSamples.data(), AudioSampleFormat::PCM24_LE,
      destSamples.size() * sizeof(int32_t));

  ASSERT_EQ(convertedSamples, sourceSamples.size());
  for (size_t i = 0; i < convertedSamples; ++i) {
    EXPECT_EQ(destSamples[i], expectedSamples[i]) << "i = " << i;
  }
}

TEST(AudioSampleFormatTest, ConvertPCM24ToPCM24_3) {
  std::vector<int32_t> sourceSamples = {1000000, 0x1000000 - 1000000, 8388607,
                                        0x1000000 - 8388608};
  std::vector<uint8_t> destBytes(sourceSamples.size() * 3);
  const std::vector<uint8_t> expectedBytes = {
      0x40, 0x42, 0x0f, 0xc0, 0xbd, 0xf0, 0xff, 0xff, 0x7f, 0x00, 0x00, 0x80};

  size_t convertedSamples = convertSampleFormat(
      sourceSamples.data(), AudioSampleFormat::PCM24_LE, sourceSamples.size(),
      destBytes.data(), AudioSampleFormat::PCM24_3LE, destBytes.size());

  ASSERT_EQ(convertedSamples, expectedBytes.size() / 3);
  for (size_t i = 0; i < convertedSamples * 3; ++i) {
    EXPECT_EQ(destBytes[i], expectedBytes[i]) << "i = " << i;
  }
}

TEST(AudioSampleFormatTest, ConvertPCM24_3ToPCM24) {
  std::vector<uint8_t> sourceBytes = {0x40, 0x42, 0x0f, 0xc0, 0xbd, 0xf0,
                                      0xff, 0xff, 0x7f, 0x00, 0x00, 0x80};
  const std::vector<int32_t> expectedSamples = {1000000, 0x1000000 - 1000000,
                                                8388607, 0x1000000 - 8388608};
  std::vector<int32_t> destSamples(expectedSamples.size());

  size_t convertedSamples = convertSampleFormat(
      sourceBytes.data(), AudioSampleFormat::PCM24_3LE, sourceBytes.size() / 3,
      destSamples.data(), AudioSampleFormat::PCM24_LE,
      destSamples.size() * sizeof(int32_t));

  ASSERT_EQ(convertedSamples, expectedSamples.size());
  for (size_t i = 0; i < convertedSamples; ++i) {
    EXPECT_EQ(destSamples[i], expectedSamples[i]) << "i = " << i;
  }
}

TEST(AudioSampleFormatTest, ApplyGainUnityIsBitPerfect) {
  // gain >= 1.0 must leave every byte untouched so full volume stays bit-perfect.
  std::vector<int16_t> samples = {1000, -1000, 32767, -32768, 0};
  const std::vector<int16_t> original = samples;

  applyGainInPlace(samples.data(), samples.size() * sizeof(int16_t),
                   AudioSampleFormat::PCM16_LE, 1.0f);
  EXPECT_EQ(samples, original);

  applyGainInPlace(samples.data(), samples.size() * sizeof(int16_t),
                   AudioSampleFormat::PCM16_LE, 2.0f);
  EXPECT_EQ(samples, original);
}

TEST(AudioSampleFormatTest, ApplyGainZeroMutes) {
  std::vector<int32_t> samples = {2000000, -2000000, 8388607, -8388608};
  applyGainInPlace(samples.data(), samples.size() * sizeof(int32_t),
                   AudioSampleFormat::PCM24_LE, 0.0f);
  for (int32_t v : samples) {
    EXPECT_EQ(v & 0xFFFFFF, 0);
  }
}

TEST(AudioSampleFormatTest, ApplyGainHalfPCM16) {
  std::vector<int16_t> samples = {1000, -1000, 20000, -20000};
  applyGainInPlace(samples.data(), samples.size() * sizeof(int16_t),
                   AudioSampleFormat::PCM16_LE, 0.5f);
  EXPECT_NEAR(samples[0], 500, 1);
  EXPECT_NEAR(samples[1], -500, 1);
  EXPECT_NEAR(samples[2], 10000, 1);
  EXPECT_NEAR(samples[3], -10000, 1);
}

TEST(AudioSampleFormatTest, ApplyGainHalfPCM24LE) {
  std::vector<int32_t> samples = {2000000, -2000000};
  applyGainInPlace(samples.data(), samples.size() * sizeof(int32_t),
                   AudioSampleFormat::PCM24_LE, 0.5f);
  // 24-bit sign-extended comparison.
  EXPECT_NEAR(samples[0], 1000000, 1);
  int32_t neg = samples[1] | (samples[1] & 0x800000 ? 0xFF000000 : 0);
  EXPECT_NEAR(neg, -1000000, 1);
}

TEST(AudioSampleFormatTest, ApplyGainHalfPCM24_3LE) {
  // 1000000 == 0x0F4240, little-endian packed 3 bytes; half is 500000 == 0x07A120.
  std::vector<uint8_t> bytes = {0x40, 0x42, 0x0f};
  applyGainInPlace(bytes.data(), bytes.size(), AudioSampleFormat::PCM24_3LE,
                   0.5f);
  EXPECT_EQ(bytes[0], 0x20);
  EXPECT_EQ(bytes[1], 0xA1);
  EXPECT_EQ(bytes[2], 0x07);
}

TEST(AudioSampleFormatTest, DestinationBufferCannoFitAllSamples) {
  std::vector<int16_t> sourceSamples = {1000, -1000, 32767, -32768};
  std::vector<int16_t> destSamples(sourceSamples.size() - 1);

  size_t convertedSamples = convertSampleFormat(
      sourceSamples.data(), AudioSampleFormat::PCM16_LE, sourceSamples.size(),
      destSamples.data(), AudioSampleFormat::PCM16_LE,
      destSamples.size() * sizeof(int16_t));

  ASSERT_EQ(convertedSamples, destSamples.size());
  for (size_t i = 0; i < destSamples.size(); ++i) {
    EXPECT_EQ(sourceSamples[i], destSamples[i]) << "i = " << i;
  }
}