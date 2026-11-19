#include "dsp/output/TruePeak.h"

#include <gtest/gtest.h>

#include <cmath>
#include <numbers>
#include <vector>

namespace {
/// A sine at a quarter of the rate whose samples fall an eighth of a cycle
/// off its crests: every one of them is at 0.707 of a true peak of 1.
std::vector<double> offCrest(size_t frames) {
  std::vector<double> out(frames);
  for (size_t n = 0; n < frames; ++n)
    out[n] = std::sin(std::numbers::pi / 2 * double(n) + std::numbers::pi / 4);
  return out;
}

/// The peak of @p samples once their first hundred have led in, so that
/// what is read is not the jump from the meter's silence.
double peakDb(TruePeakMeter &meter, const std::vector<double> &samples,
              unsigned rate, double gain = 1.0, size_t chunk = 1000) {
  constexpr size_t kLeadIn = 100;
  const double *lead[] = {samples.data()};
  meter.feed(lead, 1, kLeadIn, rate, gain);
  meter.resetPeak();
  for (size_t at = kLeadIn; at < samples.size(); at += chunk) {
    const double *planes[] = {samples.data() + at};
    meter.feed(planes, 1, std::min(chunk, samples.size() - at), rate, gain);
  }
  return 20 * std::log10(meter.peak());
}
} // namespace

TEST(TruePeakMeter, ItFindsThePeakBetweenTheSamples) {
  for (const unsigned rate : {44100u, 48000u, 88200u, 96000u}) {
    TruePeakMeter meter(1000, 2);
    EXPECT_NEAR(peakDb(meter, offCrest(4000), rate), 0.0, 0.01) << rate;
  }
}

TEST(TruePeakMeter, AboveNinetySixKilohertzTheSamplesAreThePeak) {
  TruePeakMeter meter(1000, 2);
  EXPECT_NEAR(peakDb(meter, offCrest(4000), 176400),
              20 * std::log10(std::sqrt(0.5)), 1e-9);
}

TEST(TruePeakMeter, ALowToneReadsItsSamplePeak) {
  std::vector<double> tone(48000);
  for (size_t n = 0; n < tone.size(); ++n)
    tone[n] = 0.5 * std::sin(2 * std::numbers::pi * 1000 * double(n) / 48000);
  TruePeakMeter meter(1000, 2);
  EXPECT_NEAR(peakDb(meter, tone, 48000), 20 * std::log10(0.5), 0.01);
}

TEST(TruePeakMeter, TheGainScalesThePeak) {
  TruePeakMeter meter(1000, 2);
  EXPECT_NEAR(peakDb(meter, offCrest(4000), 48000, 0.5),
              20 * std::log10(0.5), 0.01);
}

TEST(TruePeakMeter, HowTheSamplesAreSplitDoesNotChangeThePeak) {
  const auto samples = offCrest(3000);
  TruePeakMeter whole(1000, 1), split(1000, 1);
  EXPECT_DOUBLE_EQ(peakDb(whole, samples, 48000, 1.0, 1000),
                   peakDb(split, samples, 48000, 1.0, 7));
}

TEST(TruePeakMeter, ASteadyLevelReadsAsItIs) {
  const std::vector<double> level(1000, -0.8);
  const double *planes[] = {level.data()};
  for (const unsigned rate : {48000u, 96000u}) {
    TruePeakMeter meter(1000, 1);
    meter.feed(planes, 1, 1000, rate, 1.0);
    meter.resetPeak();
    meter.feed(planes, 1, 1000, rate, 1.0);
    EXPECT_NEAR(meter.peak(), 0.8, 1e-12) << rate;
  }
}

TEST(TruePeakMeter, AJumpFromSilenceReadsItsOvershoot) {
  const std::vector<double> level(1000, 0.8);
  const double *planes[] = {level.data()};
  TruePeakMeter meter(1000, 1);
  meter.feed(planes, 1, 1000, 48000, 1.0);
  EXPECT_GT(meter.peak(), 0.85) << "the step rings, as a DAC's would";
}

TEST(TruePeakMeter, EveryChannelCounts) {
  const std::vector<double> quiet(1000, 0.0), loud(1000, -0.8);
  const double *planes[] = {quiet.data(), loud.data()};
  TruePeakMeter meter(1000, 2);
  meter.feed(planes, 2, 1000, 48000, 1.0);
  meter.resetPeak();
  meter.feed(planes, 2, 1000, 48000, 1.0);
  EXPECT_NEAR(meter.peak(), 0.8, 1e-12);
}

TEST(TruePeakMeter, AResetForgetsThePeakButNotTheSamplesBefore) {
  const auto samples = offCrest(2000);
  TruePeakMeter whole(1000, 1), reset(1000, 1);
  const double *first[] = {samples.data()}, *second[] = {samples.data() + 1000};
  for (TruePeakMeter *meter : {&whole, &reset})
    meter->feed(first, 1, 1000, 48000, 1.0);
  reset.resetPeak();
  EXPECT_EQ(reset.peak(), 0.0);
  whole.resetPeak();
  for (TruePeakMeter *meter : {&whole, &reset})
    meter->feed(second, 1, 1000, 48000, 1.0);
  EXPECT_EQ(reset.peak(), whole.peak()) << "as if the stream had run on";
}

TEST(TruePeakMeter, ANewRateStartsTheSamplesOver) {
  const std::vector<double> level(1000, 0.8), silence(1000, 0.0);
  const double *loud[] = {level.data()}, *quiet[] = {silence.data()};
  TruePeakMeter meter(1000, 1);
  meter.feed(loud, 1, 1000, 48000, 1.0);
  meter.resetPeak();
  meter.feed(quiet, 1, 1000, 44100, 1.0);
  EXPECT_EQ(meter.peak(), 0.0) << "nothing of the 48 kHz stream rings on";
}

TEST(TruePeakMeter, MoreThanItWasMadeForIsRefused) {
  TruePeakMeter meter(100, 1);
  const std::vector<double> samples(200, 0.0);
  const double *planes[] = {samples.data(), samples.data()};
  EXPECT_THROW(meter.feed(planes, 1, 200, 48000, 1.0), std::invalid_argument);
  EXPECT_THROW(meter.feed(planes, 2, 100, 48000, 1.0), std::invalid_argument);
}
