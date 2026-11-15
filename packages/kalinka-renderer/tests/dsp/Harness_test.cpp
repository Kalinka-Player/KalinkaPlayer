#include "Analysis.h"
#include "ReferenceFilters.h"
#include "SigmaDelta.h"
#include "Signals.h"

#include "ContainerTestSupport.h"
#include "DsdContainer.h"
#include "DsdFormat.h"

#include <gtest/gtest.h>

#include <cmath>
#include <numbers>
#include <random>

using namespace harness;

namespace {
constexpr double kRate = 48000;
constexpr unsigned kDsd64 = 2822400;

std::vector<double> noise(size_t frames, double deviation, unsigned seed) {
  std::mt19937_64 generator(seed);
  std::normal_distribution<double> gauss(0.0, deviation);
  std::vector<double> out(frames);
  for (double &x : out) {
    x = gauss(generator);
  }
  return out;
}

std::vector<double> sum(std::vector<double> a, const std::vector<double> &b) {
  for (size_t n = 0; n < a.size(); ++n) {
    a[n] += b[n];
  }
  return a;
}

std::vector<double> asLevels(const std::vector<uint8_t> &bits) {
  std::vector<double> out(bits.size());
  for (size_t n = 0; n < bits.size(); ++n) {
    out[n] = bits[n] ? 1.0 : -1.0;
  }
  return out;
}
} // namespace

TEST(HarnessFft, MatchesTheDefinition) {
  std::mt19937 generator(3);
  std::uniform_real_distribution<double> uniform(-1, 1);
  std::vector<std::complex<double>> data(64);
  for (auto &x : data) {
    x = {uniform(generator), uniform(generator)};
  }
  const auto spectrum = fft(data);
  for (size_t k = 0; k < data.size(); ++k) {
    std::complex<long double> direct = 0;
    for (size_t n = 0; n < data.size(); ++n) {
      direct += std::complex<long double>(data[n]) *
                std::polar(1.0L, -2 * std::numbers::pi_v<long double> *
                                     (long double)(k * n) / data.size());
    }
    EXPECT_NEAR(spectrum[k].real(), double(direct.real()), 1e-13);
    EXPECT_NEAR(spectrum[k].imag(), double(direct.imag()), 1e-13);
  }
}

TEST(HarnessWindow, SidelobesAreBelow170Db) {
  const size_t size = 1024;
  const auto window = blackmanHarris7(size);
  const auto spectrum = realSpectrum(window, size * 64);
  const double peak = std::abs(spectrum[0]);
  double sidelobe = 0;
  for (size_t k = 8 * 64; k < spectrum.size(); ++k) {
    sidelobe = std::max(sidelobe, std::abs(spectrum[k]));
  }
  EXPECT_LT(20 * std::log10(sidelobe / peak), -170.0);
}

TEST(HarnessBandPower, AFullScaleSineIsZeroDbfs) {
  EXPECT_NEAR(bandPowerDb(sine(997, kRate, 1 << 16), kRate, 20, 20000), 0.0,
              0.01);
  EXPECT_NEAR(bandPowerDb(sine(997, kRate, 1 << 16, 0.5), kRate, 20, 20000),
              -6.0206, 0.01);
}

TEST(HarnessBandPower, WhiteNoiseSpreadsEvenly) {
  // Variance 1e-6 spread over 24 kHz, of which a tenth is in a 2.4 kHz band.
  const auto x = noise(1 << 18, 1e-3, 1);
  const double expected = 10 * std::log10(1e-6 / 10 / 0.5);
  EXPECT_NEAR(bandPowerDb(x, kRate, 1000, 3400), expected, 0.2);
}

TEST(HarnessThdN, ReportsAKnownHarmonic) {
  const auto x = sum(sine(1000, kRate, 1 << 17, 0.5),
                     sine(3000, kRate, 1 << 17, 0.5e-4));
  const auto result = thdPlusN(x, kRate, 1000);
  EXPECT_NEAR(result.fundamentalDbfs, -6.0206, 1e-4);
  EXPECT_NEAR(result.thdnDb, -80.0, 0.05);
}

TEST(HarnessThdN, ReportsKnownNoise) {
  const auto x =
      sum(sine(1000, kRate, 1 << 17, 0.5), noise(1 << 17, 1e-5, 2));
  // In-band share of white noise: (20000 - 20) / 24000.
  const double noisePower = 1e-10 * (19980.0 / 24000.0);
  const double expected = 10 * std::log10(noisePower / (0.25 / 2));
  EXPECT_NEAR(thdPlusN(x, kRate, 1000).thdnDb, expected, 0.2);
}

TEST(HarnessResponse, ATwoTapAverageIsHalfPowerAtAQuarterOfTheRate) {
  const std::vector<double> average{0.5, 0.5};
  EXPECT_NEAR(gainDb(average, kRate / 4, kRate), -3.0103, 1e-4);
  EXPECT_NEAR(phaseAt(average, kRate / 4, kRate), -std::numbers::pi / 4,
              1e-12);
  const auto grid = response(average, kRate, 64);
  EXPECT_NEAR(grid.magnitudeDb[16], -3.0103, 1e-4);
  EXPECT_NEAR(grid.phase[16], -std::numbers::pi / 4, 1e-12);
}

TEST(HarnessResponse, UnwrapsTheLinearPhaseOfADelay) {
  const auto delayed = impulse(256, 40);
  const auto grid = response(delayed, kRate, 256);
  for (size_t k = 0; k < grid.phase.size(); ++k) {
    EXPECT_NEAR(grid.phase[k], -2 * std::numbers::pi * 40.0 * k / 256, 1e-9);
  }
}

TEST(HarnessNull, FindsTheLagAndGainAndLeavesNothing) {
  const auto reference = multitone(std::vector<double>{100, 1000, 7000}, kRate,
                                   20000, 0.9);
  std::vector<double> measured(25000, 0.0);
  for (size_t n = 0; n < reference.size(); ++n) {
    measured[n + 123] = 0.5 * reference[n];
  }
  const auto result = nullTest(reference, measured, 1000, true);
  EXPECT_EQ(result.lag, 123);
  EXPECT_NEAR(result.gain, 0.5, 1e-12);
  EXPECT_LT(result.peakDbfs, -290.0);
  const auto unscaled = nullTest(reference, measured, 1000, false);
  EXPECT_NEAR(unscaled.peakDbfs, 20 * std::log10(0.45), 0.01);
}

TEST(HarnessClicks, ACleanToneAndAFadeAreNotClicks) {
  auto tone = sine(1000, kRate, 48000, 0.5);
  EXPECT_TRUE(findClicks(tone, 1000, kRate, 0.5).at.empty());
  const size_t fade = 1200;
  for (size_t n = 0; n < fade; ++n) {
    tone[24000 + n] *= 1.0 - double(n) / fade;
  }
  std::fill(tone.begin() + 24000 + fade, tone.end(), 0.0);
  EXPECT_TRUE(findClicks(tone, 1000, kRate, 0.5).at.empty());
}

TEST(HarnessClicks, ACutMidWaveformIsOne) {
  auto tone = sine(1000, kRate, 48000, 0.5);
  std::fill(tone.begin() + 24012, tone.end(), 0.0);
  const auto clicks = findClicks(tone, 1000, kRate, 0.5);
  ASSERT_EQ(clicks.at.size(), 1u);
  EXPECT_EQ(clicks.at[0], 24012u);
}

TEST(HarnessSignals, MultitonePeaksWhereAsked) {
  const auto x = multitone(std::vector<double>{50, 440, 3000, 12000}, kRate,
                           48000, 0.5);
  const double peak =
      std::ranges::max(x, {}, [](double v) { return std::abs(v); });
  EXPECT_NEAR(std::abs(peak), 0.5, 1e-12);
}

TEST(HarnessSignals, TheSweepRisesExponentially) {
  const double seconds = 10, from = 20, to = 20000;
  const auto sweep = logSweep(from, to, kRate, size_t(seconds * kRate), 0.5);
  const auto upwardCrossing = [&sweep](size_t n) {
    while (!(sweep[n] < 0 && sweep[n + 1] >= 0)) {
      ++n;
    }
    return (double(n) + sweep[n] / (sweep[n] - sweep[n + 1])) / kRate;
  };
  // Where a period spans enough samples for a linear zero crossing.
  for (double t : {0.5, 3.0, 6.0, 8.0}) {
    const double first = upwardCrossing(size_t(t * kRate));
    const double second = upwardCrossing(size_t(first * kRate) + 1);
    const double expected =
        from * std::pow(to / from, (first + second) / 2 / seconds);
    EXPECT_NEAR(1 / (second - first) / expected, 1.0, 2e-3) << "at " << t;
  }
}

TEST(HarnessSigmaDelta, TheOutOfBandGainPeaksWhereAsked) {
  const SigmaDelta modulator(7, 64, 1.5);
  EXPECT_NEAR(modulator.noiseGainDb(std::numbers::pi), 20 * std::log10(1.5),
              0.01);
  EXPECT_LT(modulator.noiseGainDb(2 * std::numbers::pi * 10000 / kDsd64),
            -100.0);
}

TEST(HarnessSigmaDelta, AQuarterSecondAtHalfModulationIsCleanAndStable) {
  SigmaDelta modulator(7, 64);
  const auto tone = sine(1000, kDsd64, kDsd64 / 4, 0.5);
  const auto bits = modulator.modulate(tone);
  const auto result = thdPlusN(asLevels(bits), kDsd64, 1000);
  EXPECT_NEAR(result.fundamentalDbfs, -6.0206, 0.01);
  EXPECT_LT(result.thdnDb, -125.0);
}

TEST(HarnessSigmaDelta, HalfModulationStaysStableUpToDsd256) {
  for (unsigned multiple : {128u, 256u}) {
    const unsigned rate = 44100 * multiple;
    SigmaDelta modulator(7, multiple);
    EXPECT_NO_THROW(modulator.modulate(sine(1000, rate, rate, 0.5)))
        << "DSD" << multiple;
  }
}

TEST(HarnessSigmaDelta, SilenceStaysQuietInTheAudioBand) {
  SigmaDelta modulator(7, 64);
  const auto bits = modulator.modulate(std::vector<double>(kDsd64 / 4, 0.0));
  EXPECT_LT(bandPowerDb(asLevels(bits), kDsd64, 20, 20000), -110.0);
}

namespace {
container_test::Bytes playDsd(container_test::Bytes file) {
  ContainerStreamDecoder decoder(
      1,
      std::make_unique<DsdContainer>([](unsigned rate, unsigned channels) {
        return StreamAudioFormat{rate / 8, channels, 1, DSD_U8, rate};
      }),
      65536);
  decoder.connectTo(
      std::make_shared<container_test::BytesInput>(std::move(file)));
  return container_test::drain(decoder);
}

std::vector<uint8_t> interleaved(
    const std::vector<std::vector<uint8_t>> &channels) {
  std::vector<std::vector<uint8_t>> packed;
  for (const auto &bits : channels) {
    packed.push_back(packMsbFirst(bits));
  }
  std::vector<uint8_t> out;
  for (size_t i = 0; i < packed.front().size(); ++i) {
    for (const auto &bytes : packed) {
      out.push_back(bytes[i]);
    }
  }
  return out;
}
} // namespace

TEST(HarnessDsdFiles, DsfAndDffCarryTheBitsBothOrdersMonoAndStereo) {
  SigmaDelta left(7, 64), right(7, 64);
  const size_t samples = 8 * 10000;
  const std::vector<std::vector<uint8_t>> stereo{
      left.modulate(sine(1000, kDsd64, samples, 0.5)),
      right.modulate(sine(1500, kDsd64, samples, 0.25))};
  const std::vector<std::vector<uint8_t>> mono{stereo[0]};
  for (const auto &channels : {mono, stereo}) {
    const auto expected = interleaved(channels);
    EXPECT_EQ(playDsd(dsfFile(channels, kDsd64, true)), expected);
    EXPECT_EQ(playDsd(dsfFile(channels, kDsd64, false)), expected);
    EXPECT_EQ(playDsd(dffFile(channels, kDsd64)), expected);
  }
}

namespace {
std::vector<double> impulseResponse(BiquadCascade &filter, size_t frames) {
  const auto in = impulse(frames);
  std::vector<double> out(frames);
  filter.process(in, out);
  return out;
}
} // namespace

TEST(HarnessReferenceFilters, APeakingFilterHasItsGainAtItsCentre) {
  BiquadCascade bell({peaking(1000, 6, 2, kRate)});
  const auto response = impulseResponse(bell, 1 << 16);
  EXPECT_NEAR(gainDb(response, 1000, kRate), 6.0, 1e-9);
  EXPECT_NEAR(gainDb(response, 20, kRate), 0.0, 0.01);
  EXPECT_NEAR(gainDb(response, 20000, kRate), 0.0, 0.05);
}

TEST(HarnessReferenceFilters, ALowShelfLiftsDcAndLeavesNyquist) {
  BiquadCascade shelf({lowShelf(100, 6, 1, kRate)});
  const auto response = impulseResponse(shelf, 1 << 16);
  EXPECT_NEAR(gainDb(response, 0.001, kRate), 6.0, 1e-6);
  EXPECT_NEAR(gainDb(response, kRate / 2, kRate), 0.0, 1e-6);
  EXPECT_NEAR(gainDb(response, 100, kRate), 3.0, 0.05)
      << "half the gain, in dB, at the shelf frequency";
}

TEST(HarnessReferenceFilters, AResetForgetsTheHistory) {
  BiquadCascade bell({peaking(1000, 6, 2, kRate)});
  const auto first = impulseResponse(bell, 256);
  bell.reset();
  EXPECT_EQ(impulseResponse(bell, 256), first);
}

TEST(HarnessReferenceFilters, ConvolutionMatchesTheDefinition) {
  std::mt19937 generator(5);
  std::uniform_real_distribution<double> uniform(-1, 1);
  std::vector<double> x(300), h(37);
  for (double &v : x) {
    v = uniform(generator);
  }
  for (double &v : h) {
    v = uniform(generator);
  }
  const auto y = convolve(x, h);
  ASSERT_EQ(y.size(), x.size() + h.size() - 1);
  for (size_t n = 0; n < y.size(); ++n) {
    double direct = 0;
    for (size_t k = 0; k < h.size(); ++k) {
      if (n >= k && n - k < x.size()) {
        direct += h[k] * x[n - k];
      }
    }
    EXPECT_NEAR(y[n], direct, 1e-12) << n;
  }
}

TEST(HarnessReferenceFilters, ALinearPhaseFirIsSymmetricAndFollowsItsCurve) {
  const size_t taps = 4095;
  const auto fir = linearPhaseFir(taps, kRate, [](double f) {
    return f > 10000 ? -6.0 : 0.0;
  });
  for (size_t n = 0; n < taps / 2; ++n) {
    ASSERT_NEAR(fir[n], fir[taps - 1 - n], 1e-12) << n;
  }
  EXPECT_NEAR(gainDb(fir, 1000, kRate), 0.0, 0.01);
  EXPECT_NEAR(gainDb(fir, 15000, kRate), -6.0, 0.01);
  const double delay = double(taps - 1) / 2;
  EXPECT_NEAR(phaseAt(fir, 1000, kRate),
              std::remainder(-2 * std::numbers::pi * 1000 * delay / kRate,
                             2 * std::numbers::pi),
              1e-6)
      << "pure delay of (taps - 1) / 2";
}
