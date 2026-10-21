#include "GainFade.h"

#include <gtest/gtest.h>

#include <vector>

namespace {
// A thousand frames a second makes a 10 ms fade ten frames long.
constexpr unsigned kRate = 1000;
constexpr auto kLength = std::chrono::milliseconds(10);

std::vector<int16_t> faded(GainFade &fade, size_t frames, float target) {
  std::vector<int16_t> samples(frames, 10000);
  fade.apply(samples.data(), samples.size() * sizeof(int16_t),
             AudioSampleFormat::PCM16_LE, 1, kRate, target);
  return samples;
}
} // namespace

TEST(GainFadeTest, AChangeIsSpreadOverTheFadeLength) {
  GainFade fade(kLength);
  const auto samples = faded(fade, 20, 0.5f);
  EXPECT_EQ(samples[0], 9500);
  for (size_t i = 1; i < 10; ++i)
    EXPECT_NEAR(samples[i - 1] - samples[i], 500, 1) << "frame " << i;
  for (size_t i = 9; i < samples.size(); ++i)
    EXPECT_EQ(samples[i], 5000) << "frame " << i;
}

TEST(GainFadeTest, AFadeCarriesOnAcrossBuffers) {
  GainFade whole(kLength);
  GainFade split(kLength);
  const auto expected = faded(whole, 20, 0.5f);
  std::vector<int16_t> pieces;
  for (size_t frames : {3, 3, 3, 3, 8}) {
    const auto piece = faded(split, frames, 0.5f);
    pieces.insert(pieces.end(), piece.begin(), piece.end());
  }
  ASSERT_EQ(pieces.size(), expected.size());
  for (size_t i = 0; i < pieces.size(); ++i)
    EXPECT_NEAR(pieces[i], expected[i], 1) << "frame " << i;
}

TEST(GainFadeTest, ANewTargetMidFadeCarriesOnFromTheLevelReached) {
  GainFade fade(kLength);
  EXPECT_EQ(faded(fade, 5, 0.0f).back(), 5000);
  const auto back = faded(fade, 10, 1.0f);
  EXPECT_EQ(back.front(), 5500) << "a jump back to the old level clicks";
  EXPECT_EQ(back.back(), 10000);
}

TEST(GainFadeTest, SettleTakesTheGainAtOnce) {
  GainFade fade(kLength);
  fade.settle(0.25f);
  EXPECT_EQ(faded(fade, 3, 0.25f), std::vector<int16_t>(3, 2500));
}

TEST(GainFadeTest, FullVolumeIsBitPerfectOnceTheFadeLands) {
  GainFade fade(kLength);
  fade.settle(0.5f);
  faded(fade, 10, 1.0f);
  std::vector<int16_t> samples = {12345, -321, 32767, -32768, 1};
  const auto original = samples;
  fade.apply(samples.data(), samples.size() * sizeof(int16_t),
             AudioSampleFormat::PCM16_LE, 1, kRate, 1.0f);
  EXPECT_EQ(samples, original);
}
