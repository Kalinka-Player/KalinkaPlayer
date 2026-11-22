#include "ReferenceFilters.h"
#include "dsp/peq/RoomEq.h"
#include <cmath>
#include <complex>
#include <gtest/gtest.h>
#include <limits>
#include <numbers>

TEST(RoomEq, TwentySectionsMatchTheScalarReferenceAcrossShortReads) {
  auto profile = std::make_shared<peq::Profile>();
  std::vector<harness::BiquadCoefficients> reference;
  for (size_t i = 0; i < 20; ++i) {
    const double hz = 100 * std::pow(1.28, i), gain = i % 2 ? -2 : 3;
    profile->left.push_back({peq::Type::Peak, hz, gain, 1.4});
    reference.push_back(harness::peaking(hz, gain, 1.4, 192000));
  }
  auto plan = peq::prepare(profile.get(), 192000, 2);
  ASSERT_EQ(plan.pairs[0].count, 20u);
  std::vector<double> left(1027), right(1027), expected(1027);
  for (size_t i = 0; i < left.size(); ++i)
    left[i] = right[i] = std::sin(double(i) / 7);
  const auto dry = right;
  auto mono = left;
  std::array<peq::State, peq::kBands> monoHistory{};
  peq::filter(plan.pairs[0], monoHistory.data(), mono.data(), nullptr,
              mono.size());
  harness::BiquadCascade(reference).process(left, expected);
  std::array<peq::State, peq::kBands> history{};
  size_t at = 0;
  for (size_t count : {3, 1, 1000, 23}) {
    peq::filter(plan.pairs[0], history.data(), left.data() + at,
                right.data() + at, count);
    at += count;
  }
  EXPECT_EQ(right, dry);
  EXPECT_EQ(left, mono);
  for (size_t i = 0; i < left.size(); ++i)
    ASSERT_NEAR(left[i], expected[i], 1e-11) << i;
}

TEST(RoomEq, AutoHeadroomIncludesResonantShelvesAndOnlyTheLouderChannel) {
  for (auto type : {peq::Type::LowShelf, peq::Type::HighShelf}) {
    for (double db : {-6.0, 6.0}) {
      auto p = std::make_shared<peq::Profile>();
      p->left.push_back({type, 2000, db, 3});
      p->right.push_back({peq::Type::Peak, 700, 1, 1});
      auto plan = peq::prepare(p.get(), 48000, 2);
      const auto &s = plan.pairs[0].sections[0];
      double peak = 0;
      for (unsigned k = 0; k <= 8192; ++k) {
        const auto z = std::polar(1.0, -std::numbers::pi * k / 8192);
        peak = std::max(peak, std::abs((s.b0[0] + z * (s.b1[0] + z * s.b2[0])) /
                                       (1.0 + z * (s.a1[0] + z * s.a2[0]))));
      }
      EXPECT_NEAR(-plan.automaticPreampDb, 20 * std::log10(peak), 0.001);
    }
  }
}

TEST(RoomEq, AboveNyquistSkipsTheWholeProfileAtThatRate) {
  auto p = std::make_shared<peq::Profile>();
  p->shared = {{peq::Type::Peak, 300, 2, 1},
               {peq::Type::HighShelf, 23000, -6, 0.7}};
  EXPECT_FALSE(peq::prepare(p.get(), 44100, 2).applicable);
  EXPECT_TRUE(peq::prepare(p.get(), 48000, 2).active);
}

TEST(RoomEq, IndependentDenseCurvesMatchAcrossBlockAndSampleBoundaries) {
  auto profile = std::make_shared<peq::Profile>();
  for (size_t i = 0; i < peq::kBands; ++i) {
    const double hz = 30 * std::pow(1.35, i);
    profile->left.push_back({peq::Type::Peak, hz, i % 2 ? -3.0 : 2.0, 1.4});
    if (i < 17)
      profile->right.push_back({i == 0 ? peq::Type::LowShelf : peq::Type::Peak,
                                1.2 * hz, i % 2 ? 2.0 : -3.0, 1.1});
  }
  auto plan = peq::prepare(profile.get(), 192000, 2);
  const auto &pair = plan.pairs[0];
#if defined(__aarch64__)
  ASSERT_TRUE(pair.parallel);
#endif
  std::array<std::vector<double>, 2> planes, expected;
  for (size_t ch = 0; ch < planes.size(); ++ch) {
    auto &data = planes[ch];
    data.resize(17003);
    for (size_t i = 0; i < data.size() / 2; ++i)
      data[i] = 0.3 + std::sin((ch + 1.0) * i * 0.001) * std::cos(i * 0.07);
    expected[ch].resize(data.size());
    std::vector<harness::BiquadCoefficients> coeffs;
    for (size_t m = 0; m < pair.count; ++m) {
      const auto &s = pair.sections[m];
      coeffs.push_back({s.b0[ch], s.b1[ch], s.b2[ch], s.a1[ch], s.a2[ch]});
    }
    harness::BiquadCascade(coeffs).process(data, expected[ch]);
  }
  auto continuous = planes;
  std::array<peq::State, peq::kBands> continuousHistory{};
  peq::filter(pair, continuousHistory.data(), continuous[0].data(),
              continuous[1].data(), continuous[0].size());
  const size_t sizes[]{0,  1,  2,  3,  7,   8,    9,   10,
                       11, 19, 20, 21, 257, 1024, 4097};
  std::array<peq::State, peq::kBands> history{};
  size_t at = 0;
  for (size_t block = 0; at < planes[0].size(); ++block) {
    const size_t n =
        std::min(sizes[block % std::size(sizes)], planes[0].size() - at);
    peq::filter(pair, history.data(), planes[0].data() + at,
                planes[1].data() + at, n);
    at += n;
  }
  EXPECT_EQ(planes, continuous);
  for (size_t ch = 0; ch < planes.size(); ++ch) {
    double peak = 0;
    for (size_t i = 0; i < planes[ch].size(); ++i) {
      ASSERT_TRUE(std::isfinite(planes[ch][i]));
      peak = std::max(peak, std::abs(planes[ch][i] - expected[ch][i]));
    }
    EXPECT_LT(peak, 1e-10) << "channel " << ch;
  }
}

TEST(RoomEq, SharedCurvesMatchTheReferenceForEveryBandCount) {
  constexpr unsigned rate = 192000;
  peq::Profile profile;
  std::vector<harness::BiquadCoefficients> reference;
  std::vector<double> input(2057);
  for (size_t i = 0; i < input.size(); ++i)
    input[i] = std::sin(double(i) / 11.0);
  for (size_t count = 1; count <= peq::kBands; ++count) {
    SCOPED_TRACE(count);
    const double hz = 110 * std::pow(1.25, count), db = count % 2 ? -3 : 2;
    profile.shared.push_back({peq::Type::Peak, hz, db, 1.4});
    reference.push_back(harness::peaking(hz, db, 1.4, rate));
    const auto plan = peq::prepare(&profile, rate, 5);
    const auto &front = plan.pairs.front();
    ASSERT_EQ(front.count, count);
    EXPECT_EQ(plan.pairs[2], front);
    EXPECT_EQ(plan.pairs[3].count, 0u);
#if defined(__aarch64__)
    EXPECT_EQ(front.parallel.has_value(), count >= 3 && count != 6);
    EXPECT_EQ(plan.pairs[2].parallel.has_value(), front.parallel.has_value());
#endif
    std::vector<double> expected(input.size());
    harness::BiquadCascade(reference).process(input, expected);
    auto left = input, right = input;
    std::array<peq::State, peq::kBands> history{};
    size_t at = 0;
    for (size_t frames : {1, 9, 10, 11, 1024, 1002}) {
      peq::filter(front, history.data(), left.data() + at, right.data() + at,
                  frames);
      at += frames;
    }
    EXPECT_EQ(left, right);
    for (size_t i = 0; i < left.size(); ++i)
      ASSERT_NEAR(left[i], expected[i], 1e-10) << i;
  }
}

TEST(RoomEq, RepeatedPolesUseTheOriginalCascade) {
  auto profile = std::make_shared<peq::Profile>();
  profile->shared.assign(12, {peq::Type::Peak, 80, -2, 1.4});
  const auto plan = peq::prepare(profile.get(), 192000, 2);
  EXPECT_TRUE(plan.active);
#if defined(__aarch64__)
  EXPECT_FALSE(plan.pairs[0].parallel);
#endif
}

TEST(RoomEq, ADecayingNormalHistoryFallsStraightToSilenceAtUnderflow) {
  peq::Cascade cascade;
  cascade.count = 1;
  cascade.sections[0].a1 = {-0.5, -0.5};
  peq::State state;
  state.z1.fill(std::numeric_limits<double>::min());
  std::array<double, 64> left{}, right{};
  peq::filter(cascade, &state, left.data(), right.data(), left.size());
  EXPECT_EQ(left.front(), std::numeric_limits<double>::min());
  EXPECT_EQ(left, right);
  EXPECT_TRUE(std::all_of(left.begin() + 1, left.end(),
                          [](double x) { return x == 0; }));
  EXPECT_EQ(state.z1, peq::Stereo{});
}
