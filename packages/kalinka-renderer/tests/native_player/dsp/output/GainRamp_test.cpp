#include "dsp/output/GainRamp.h"

#include <gtest/gtest.h>

namespace {
constexpr unsigned kRate = 48000;
// 25 ms at 48 kHz.
constexpr size_t kRampFrames = 1200;

void expectSpan(const GainRamp::Span &span, double from, double to,
                size_t frames) {
  EXPECT_DOUBLE_EQ(span.from, from);
  EXPECT_DOUBLE_EQ(span.to, to);
  EXPECT_EQ(span.frames, frames);
}
} // namespace

TEST(GainRamp, AChangeIsSpreadOverTheRampThenHeld) {
  GainRamp ramp(std::chrono::milliseconds(25));
  ramp.aim(0.5, kRate);
  expectSpan(ramp.take(5000), 1.0, 0.5, kRampFrames);
  expectSpan(ramp.take(5000), 0.5, 0.5, 5000);
  EXPECT_TRUE(ramp.holding(0.5));
}

TEST(GainRamp, ARampCarriesOnAcrossTakes) {
  GainRamp ramp(std::chrono::milliseconds(25));
  ramp.aim(0.5, kRate);
  expectSpan(ramp.take(kRampFrames / 2), 1.0, 0.75, kRampFrames / 2);
  EXPECT_FALSE(ramp.holding(0.5));
  expectSpan(ramp.take(kRampFrames), 0.75, 0.5, kRampFrames / 2);
}

TEST(GainRamp, ANewTargetMidRampCarriesOnFromTheLevelReached) {
  GainRamp ramp(std::chrono::milliseconds(25));
  ramp.aim(0.5, kRate);
  ramp.take(kRampFrames / 2);
  ramp.aim(1.0, kRate);
  expectSpan(ramp.take(5000), 0.75, 1.0, kRampFrames);
}

TEST(GainRamp, TheSameTargetAgainDoesNotRestartTheRamp) {
  GainRamp ramp(std::chrono::milliseconds(25));
  ramp.aim(0.5, kRate);
  ramp.take(kRampFrames / 2);
  ramp.aim(0.5, kRate);
  expectSpan(ramp.take(5000), 0.75, 0.5, kRampFrames / 2);
}

TEST(GainRamp, SettleTakesTheGainAtOnce) {
  GainRamp ramp(std::chrono::milliseconds(25));
  ramp.aim(0.5, kRate);
  ramp.take(10);
  ramp.settle(0.25);
  EXPECT_TRUE(ramp.holding(0.25));
  expectSpan(ramp.take(100), 0.25, 0.25, 100);
}

TEST(GainRamp, ItStartsHoldingUnity) {
  GainRamp ramp(std::chrono::milliseconds(25));
  EXPECT_TRUE(ramp.holding(1.0));
}
