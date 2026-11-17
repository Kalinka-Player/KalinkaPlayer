#include "dsp/output/PlanarPcm.h"

#include <gtest/gtest.h>

#include <cstring>
#include <stdexcept>
#include <vector>

namespace {
constexpr size_t kFrames = 5;

/// Interleaved bytes and what each channel should read back as.
struct Interleaved {
  std::vector<uint8_t> bytes;
  std::vector<std::vector<double>> planar;
};

/// Frame i of channel c holds @p sample(i, c), written by @p put.
template <class Sample, class Put>
Interleaved interleaved(unsigned channels, size_t width, Sample sample,
                        Put put) {
  Interleaved out{std::vector<uint8_t>(kFrames * channels * width),
                  std::vector<std::vector<double>>(channels)};
  for (size_t i = 0; i < kFrames; ++i)
    for (unsigned c = 0; c < channels; ++c)
      out.planar[c].push_back(
          put(&out.bytes[(i * channels + c) * width], sample(i, c)));
  return out;
}

std::vector<std::vector<double>> read(const Interleaved &in,
                                      AudioSampleFormat format) {
  const auto channels = unsigned(in.planar.size());
  std::vector<std::vector<double>> out(channels,
                                       std::vector<double>(kFrames, -9.0));
  std::vector<double *> planes;
  for (auto &plane : out)
    planes.push_back(plane.data());
  readPlanar(in.bytes.data(), format, channels, kFrames, planes.data());
  return out;
}

/// A different integer for every frame and channel, of both signs.
int64_t integer(size_t i, unsigned c, int64_t full) {
  const int64_t value = full / 7 * int64_t(i + 1) + int64_t(c) * 3 + 1;
  return i % 2 ? -value : value;
}

template <class T> double putWord(uint8_t *at, T value, double full) {
  std::memcpy(at, &value, sizeof(T));
  return double(value) / full;
}
} // namespace

TEST(PlanarPcm, EveryFormatReadsEachChannelIntoItsOwnPlane) {
  for (const unsigned channels : {1u, 2u, 3u}) {
    const auto pcm16 = interleaved(
        channels, 2, [](size_t i, unsigned c) { return integer(i, c, 32768); },
        [](uint8_t *at, int64_t v) {
          return putWord(at, int16_t(v), 32768.0);
        });
    EXPECT_EQ(read(pcm16, PCM16_LE), pcm16.planar) << channels;

    // The top byte of a 24-bit word is padding, whatever it holds.
    const auto pcm24 = interleaved(
        channels, 4,
        [](size_t i, unsigned c) { return integer(i, c, 1 << 23); },
        [](uint8_t *at, int64_t v) {
          const auto word = uint32_t(v) & 0xffffffu;
          putWord(at, word | 0xa5000000u, 1.0);
          return double(v) / 8388608.0;
        });
    EXPECT_EQ(read(pcm24, PCM24_LE), pcm24.planar) << channels;

    const auto packed = interleaved(
        channels, 3,
        [](size_t i, unsigned c) { return integer(i, c, 1 << 23); },
        [](uint8_t *at, int64_t v) {
          at[0] = uint8_t(v);
          at[1] = uint8_t(v >> 8);
          at[2] = uint8_t(v >> 16);
          return double(v) / 8388608.0;
        });
    EXPECT_EQ(read(packed, PCM24_3LE), packed.planar) << channels;

    const auto pcm32 = interleaved(
        channels, 4,
        [](size_t i, unsigned c) { return integer(i, c, 1ll << 31); },
        [](uint8_t *at, int64_t v) {
          return putWord(at, int32_t(v), 2147483648.0);
        });
    EXPECT_EQ(read(pcm32, PCM32_LE), pcm32.planar) << channels;

    const auto single = interleaved(
        channels, 4,
        [](size_t i, unsigned c) {
          return float(integer(i, c, 1 << 20)) / 0x1p20f;
        },
        [](uint8_t *at, float v) { return putWord(at, v, 1.0); });
    EXPECT_EQ(read(single, PCM_FLOAT32_LE), single.planar) << channels;

    const auto wide = interleaved(
        channels, 8,
        [](size_t i, unsigned c) {
          return double(integer(i, c, 1 << 30)) / 0x1p30;
        },
        [](uint8_t *at, double v) { return putWord(at, v, 1.0); });
    EXPECT_EQ(read(wide, PCM_FLOAT64_LE), wide.planar) << channels;
  }
}

TEST(PlanarPcm, DsdHasNoPlanarPcm) {
  const std::vector<uint8_t> bytes(4);
  double plane[4];
  double *planes[] = {plane};
  EXPECT_THROW(readPlanar(bytes.data(), DSD_U8, 1, 4, planes),
               std::invalid_argument);
}
