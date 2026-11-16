#include "AudioInfo.h"
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
  EXPECT_EQ(pcmOutputFormats(PCM16_LE, 16),
            (std::vector<AudioSampleFormat>{PCM16_LE, PCM24_LE, PCM32_LE,
                                             PCM24_3LE}));
}

TEST(AudioSampleFormatTest, Packed24BitInputCanUseAnS32OnlyDac) {
  const auto formats = pcmOutputFormats(PCM24_3LE, 24);
  ASSERT_EQ(formats, (std::vector<AudioSampleFormat>{PCM24_3LE, PCM24_LE,
                                                     PCM32_LE}));
  // The SMSL DAC accepts only S32. Preserve sign, full scale and the least
  // significant source bit when expanding Shairport's packed stereo samples.
  const std::vector<uint8_t> source = {
      0x01, 0x00, 0x00, 0xff, 0xff, 0xff,
      0xff, 0xff, 0x7f, 0x00, 0x00, 0x80,
      0x56, 0x34, 0x12, 0xaa, 0xcb, 0xed};
  std::vector<int32_t> output(source.size() / 3);
  ASSERT_EQ(convertSampleFormat(source.data(), PCM24_3LE, output.size(),
                                 output.data(), formats.back(), output.size() * 4),
            output.size());
  EXPECT_EQ(output, (std::vector<int32_t>{256, -256, 2147483392,
                                         -2147483647 - 1, 0x12345600,
                                         -0x12345600}));
  std::vector<uint8_t> restored(source.size());
  ASSERT_EQ(convertSampleFormat(output.data(), PCM32_LE, output.size(),
                                 restored.data(), PCM24_3LE, restored.size()),
            output.size());
  EXPECT_EQ(restored, source);
}

TEST(AudioSampleFormatTest, DsdHasNoPcmFallback) {
  EXPECT_EQ(pcmOutputFormats(DSD_U32_BE, 1),
            (std::vector<AudioSampleFormat>{DSD_U32_BE}));
  EXPECT_EQ(pcmOutputFormats(DOP32_LE, 24),
            (std::vector<AudioSampleFormat>{DOP32_LE}));
}

TEST(AudioSampleFormatTest, True32BitAndFloatHaveNoLossyFallback) {
  EXPECT_EQ(sampleBits(PCM32_LE), 32u);
  EXPECT_EQ(pcmOutputFormats(PCM32_LE, 32),
            (std::vector<AudioSampleFormat>{PCM32_LE}));
  EXPECT_EQ(pcmOutputFormats(PCM32_LE, 24),
            (std::vector<AudioSampleFormat>{PCM32_LE, PCM24_LE, PCM24_3LE}));
  EXPECT_EQ(pcmOutputFormats(PCM_FLOAT32_LE, 32),
            (std::vector<AudioSampleFormat>{PCM_FLOAT32_LE}));
  EXPECT_EQ(pcmOutputFormats(PCM_FLOAT64_LE, 64),
            (std::vector<AudioSampleFormat>{PCM_FLOAT64_LE}));
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

TEST(AudioSampleFormatTest, InterleavesPlanarSamplesMaskedToTheirDepth) {
  const int32_t left[] = {1, -1}, right[] = {0x123456, -0x123456};
  const int32_t *const channels[] = {left, right};
  std::vector<uint8_t> pcm;
  interleave({48000, 2, 24, PCM24_LE}, channels, 2, pcm);
  EXPECT_EQ(pcm, (std::vector<uint8_t>{1, 0, 0, 0, 0x56, 0x34, 0x12, 0, 0xff,
                                       0xff, 0xff, 0, 0xaa, 0xcb, 0xed, 0}));
  interleave({48000, 1, 16, PCM16_LE}, channels, 2, pcm);
  EXPECT_EQ(pcm, (std::vector<uint8_t>{1, 0, 0xff, 0xff}));
  interleave({48000, 1, 32, PCM32_LE}, channels + 1, 1, pcm);
  EXPECT_EQ(pcm, (std::vector<uint8_t>{0x56, 0x34, 0x12, 0}));
}
