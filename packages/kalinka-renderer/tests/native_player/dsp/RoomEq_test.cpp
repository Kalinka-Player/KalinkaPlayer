#include "ReferenceFilters.h"
#include "dsp/peq/RoomEq.h"
#include <cmath>
#include <complex>
#include <gtest/gtest.h>
#include <limits>
#include <numbers>
#include <optional>
#include <vector>

namespace {
/// Magnitude of one lane of @p section at @p w radians per sample.
long double gainAt(const peq::Section &section, size_t lane, long double w) {
  const auto z = std::polar(1.0L, -w);
  const long double b0 = section.b0[lane], b1 = section.b1[lane],
                    b2 = section.b2[lane];
  const long double a1 = section.a1[lane], a2 = section.a2[lane];
  return std::abs((b0 + z * (b1 + z * b2)) / (1.0L + z * (a1 + z * a2)));
}

using Planes = std::array<std::vector<double>, 2>;

/// Run the requested bands in their original order, independent of how the
/// stereo planner pairs, reorders or splits them into parallel banks.
Planes referenceOutput(const peq::Profile &profile, unsigned rate,
                       const Planes &input) {
  Planes expected;
  for (size_t ch = 0; ch < input.size(); ++ch) {
    std::vector<harness::BiquadCoefficients> coeffs;
    const auto &bands = !profile.shared.empty() ? profile.shared
                       : ch == 0                ? profile.left
                                                : profile.right;
    for (const auto &band : bands) {
      if (peq::isFlat(band))
        continue;
      peq::Profile single;
      single.shared = {band};
      const auto plan = peq::prepare(&single, rate, 1);
      EXPECT_TRUE(plan.applicable);
      EXPECT_EQ(plan.pairs[0].count, 1u);
      const auto &s = plan.pairs[0].sections[0];
      coeffs.push_back({s.b0[0], s.b1[0], s.b2[0], s.a1[0], s.a2[0]});
    }
    expected[ch].resize(input[ch].size());
    harness::BiquadCascade(coeffs).process(input[ch], expected[ch]);
  }
  return expected;
}

/// @p planes through @p cascade in place, read in blocks of uneven sizes.
void filterInBlocks(const peq::Cascade &cascade, Planes &planes) {
  const size_t sizes[]{0,  1,  2,  3,  7,   8,    9,   10,
                       11, 19, 20, 21, 257, 1024, 4097};
  std::array<peq::State, peq::kBands> history{};
  size_t at = 0;
  for (size_t block = 0; at < planes[0].size(); ++block) {
    const size_t n =
        std::min(sizes[block % std::size(sizes)], planes[0].size() - at);
    peq::filter(cascade, history.data(), planes[0].data() + at,
                planes[1].data() + at, n);
    at += n;
  }
}

/// The largest difference between two signals, infinite if either is not
/// finite.
double worstError(const std::vector<double> &a, const std::vector<double> &b) {
  double worst = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    if (!std::isfinite(a[i]) || !std::isfinite(b[i]))
      return std::numeric_limits<double>::infinity();
    worst = std::max(worst, std::abs(a[i] - b[i]));
  }
  return worst;
}

/// Expects @p cascade to follow the scalar reference on a signal that differs
/// by channel and stops dead halfway, and to give the same samples whether
/// read whole or in uneven blocks.
void expectTheReference(const peq::Cascade &cascade, const peq::Profile &profile,
                        unsigned rate) {
  Planes planes;
  for (size_t ch = 0; ch < planes.size(); ++ch) {
    planes[ch].resize(17003);
    for (size_t i = 0; i < planes[ch].size() / 2; ++i)
      planes[ch][i] =
          0.3 + std::sin((ch + 1.0) * i * 0.001) * std::cos(i * 0.07);
  }
  const auto expected = referenceOutput(profile, rate, planes);
  auto whole = planes;
  std::array<peq::State, peq::kBands> history{};
  peq::filter(cascade, history.data(), whole[0].data(), whole[1].data(),
              whole[0].size());
  filterInBlocks(cascade, planes);
  EXPECT_EQ(planes, whole);
  for (size_t ch = 0; ch < planes.size(); ++ch)
    EXPECT_LT(worstError(planes[ch], expected[ch]), 1e-10) << "channel " << ch;
}

#if defined(__aarch64__)
/// Sections left in series behind the parallel modes, or nothing where the
/// whole cascade runs in series.
std::optional<size_t> serialSections(const peq::Cascade &cascade) {
  if (!cascade.parallel)
    return std::nullopt;
  return cascade.count - cascade.parallel->replaced;
}

/// The modes in each parallel bank of one channel, in the order they run.
std::vector<size_t> bankSizes(const peq::Cascade &cascade, size_t lane = 0) {
  std::vector<size_t> sizes;
  if (cascade.parallel)
    for (const auto &bank : cascade.parallel->channels[lane].banks)
      if (bank.count)
        sizes.push_back(bank.count);
  return sizes;
}
#endif
} // namespace

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

TEST(RoomEq, AutoHeadroomIncludesResonantSectionsAndOnlyTheLouderChannel) {
  using enum peq::Type;
  for (const peq::Band band :
       {peq::Band{LowShelf, 2000, -6, 3}, peq::Band{LowShelf, 2000, 6, 3},
        peq::Band{HighShelf, 2000, -6, 3}, peq::Band{HighShelf, 2000, 6, 3},
        peq::Band{HighPass, 2000, 0, 3}, peq::Band{LowPass, 2000, 0, 3}}) {
    SCOPED_TRACE(testing::Message() << int(band.type) << " " << band.db);
    auto p = std::make_shared<peq::Profile>();
    p->left.push_back(band);
    p->right.push_back({Peak, 700, 1, 1});
    auto plan = peq::prepare(p.get(), 48000, 2);
    long double peak = 0;
    for (unsigned k = 0; k <= 8192; ++k)
      peak = std::max(peak, gainAt(plan.pairs[0].sections[0], 0,
                                   std::numbers::pi_v<long double> * k / 8192));
    EXPECT_NEAR(-plan.automaticPreampDb, double(20 * std::log10(peak)), 0.001);
  }
}

TEST(RoomEq, PassBandsWithoutResonanceNeedNoHeadroom) {
  using enum peq::Type;
  for (const peq::Band band :
       {peq::Band{HighPass, 2000, 0, 0.3}, peq::Band{HighPass, 2000, 0, 0.5},
        peq::Band{LowPass, 2000, 0, 0.5},
        peq::Band{LowPass, 2000, 0, std::numbers::sqrt2 / 2},
        peq::Band{FirstOrderHighPass, 2000, 0, 0},
        peq::Band{FirstOrderLowPass, 2000, 0, 0}}) {
    peq::Profile profile;
    profile.shared.push_back(band);
    const auto plan = peq::prepare(&profile, 48000, 2);
    EXPECT_TRUE(plan.active);
    EXPECT_EQ(plan.automaticPreampDb, 0);
  }
}

TEST(RoomEq, PassBandsFollowTheirPrototypesAtEveryRate) {
  constexpr long double pi = std::numbers::pi_v<long double>;
  using enum peq::Type;
  // A first-order band has no Q.
  const peq::Band shapes[]{{HighPass, 0, 0, 0.5},
                           {HighPass, 0, 0, std::numbers::sqrt2 / 2},
                           {HighPass, 0, 0, 4},
                           {LowPass, 0, 0, 0.5},
                           {LowPass, 0, 0, std::numbers::sqrt2 / 2},
                           {LowPass, 0, 0, 4},
                           {FirstOrderHighPass, 0, 0, 0},
                           {FirstOrderLowPass, 0, 0, 0}};
  for (unsigned rate : {44100u, 192000u, 384000u}) {
    for (double hz : {20.0, 80.0, 2500.0, 18000.0}) {
      for (peq::Band band : shapes) {
        band.hz = hz;
        SCOPED_TRACE(testing::Message()
                     << rate << " Hz, type " << int(band.type) << ", " << hz
                     << " Hz, Q " << band.q);
        peq::Profile profile;
        profile.shared.push_back(band);
        const auto plan = peq::prepare(&profile, rate, 2);
        ASSERT_TRUE(plan.active);
        ASSERT_EQ(plan.pairs[0].count, 1u);
        const auto &s = plan.pairs[0].sections[0];
        EXPECT_EQ(s.b0[0], s.b0[1]);
        const bool high =
            band.type == HighPass || band.type == FirstOrderHighPass;
        EXPECT_EQ(s.b0[0] + (high ? s.b1[0] : -s.b1[0]) + s.b2[0], 0)
            << "the stopband end is an exact zero";
        EXPECT_NEAR(double(gainAt(s, 0, high ? pi : 0)), 1, 1e-7);
        const long double corner = std::tan(pi * hz / rate);
        for (double octaves = -6; octaves <= 6; octaves += 0.5) {
          const double f = hz * std::pow(2.0, octaves);
          if (f >= 0.499 * rate)
            break;
          // The bilinear transform maps f to this analog frequency, in
          // units of the corner.
          const long double x = std::tan(pi * f / rate) / corner;
          const long double prototype =
              peq::isFirstOrder(band.type)
                  ? (high ? x : 1) / std::hypot(1, x)
                  : (high ? x * x : 1) / std::hypot(1 - x * x, x / band.q);
          const long double measured = gainAt(s, 0, 2 * pi * f / rate);
          EXPECT_NEAR(double(20 * std::log10(measured / prototype)), 0, 1e-6)
              << f << " Hz";
        }
      }
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
  expectTheReference(pair, *profile, 192000);
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
    if (count < 3)
      EXPECT_FALSE(front.parallel);
    else
      EXPECT_EQ(serialSections(front), count > 5 && count % 5 == 1 ? 1u : 0u)
          << "one mode over whole groups of five would pad another group";
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

TEST(RoomEq, PassBandsAmongPeaksMatchTheReferenceAndBlockDc) {
  constexpr unsigned rate = 192000;
  using enum peq::Type;
  auto profile = std::make_shared<peq::Profile>();
  profile->left = {{HighPass, 20, 0, std::numbers::sqrt2 / 2},
                   {LowShelf, 69, 2.87, 0.66},
                   {Peak, 245, -0.46, 2.42},
                   {Peak, 1261, 1.26, 0.49},
                   {Peak, 4966, -1.7, 2.04},
                   {Peak, 10361, -0.75, 1.42},
                   {LowPass, 20000, 0, 1.2}};
  // Fourth-order Butterworth edges, as two sections each.
  profile->right = {{HighPass, 35, 0, 0.5412},
                    {HighPass, 35, 0, 1.3066},
                    {Peak, 391, -0.85, 2.98},
                    {LowPass, 18000, 0, 0.5412},
                    {LowPass, 18000, 0, 1.3066}};
  const auto plan = peq::prepare(profile.get(), rate, 2);
  const auto &pair = plan.pairs[0];
  ASSERT_EQ(pair.count, 7u);
#if defined(__aarch64__)
  ASSERT_TRUE(pair.parallel);
#endif
  Planes planes;
  for (size_t ch = 0; ch < planes.size(); ++ch) {
    planes[ch].resize(2 * rate);
    for (size_t i = 0; i < planes[ch].size(); ++i)
      planes[ch][i] =
          0.5 + (i < rate ? 0.4 * std::sin((ch + 1.0) * i * 0.003) : 0.0);
  }
  const auto expected = referenceOutput(*profile, rate, planes);
  filterInBlocks(pair, planes);
  for (size_t ch = 0; ch < planes.size(); ++ch) {
    EXPECT_LT(worstError(planes[ch], expected[ch]), 1e-10) << "channel " << ch;
    EXPECT_LT(std::abs(planes[ch].back()), 1e-9)
        << "a second of DC through the high pass, channel " << ch;
  }
}

TEST(RoomEq, RealPolesAndFirstOrderBandsRunAsModes) {
  using enum peq::Type;
  peq::Profile profile;
  // A 12 dB Linkwitz-Riley edge is one real pole twice, and a wide cut has
  // two real poles.
  profile.shared = {{FirstOrderHighPass, 25, 0, 0}, {HighPass, 40, 0, 0.5},
                    {LowShelf, 69, 2.87, 0.66},     {Peak, 245, -0.46, 2.42},
                    {Peak, 391, -0.85, 2.98},       {Peak, 1261, 1.26, 0.49},
                    {Peak, 3000, -4, 0.45},         {Peak, 4966, -1.7, 2.04},
                    {Peak, 10361, -0.75, 1.42},     {LowPass, 15000, 0, 0.5}};
  for (unsigned rate : {44100u, 192000u}) {
    SCOPED_TRACE(rate);
    const auto plan = peq::prepare(&profile, rate, 2);
    const auto &pair = plan.pairs[0];
    ASSERT_EQ(pair.count, 10u);
#if defined(__aarch64__)
    EXPECT_EQ(bankSizes(pair), std::vector<size_t>{10});
    EXPECT_EQ(serialSections(pair), 0u);
#endif
    expectTheReference(pair, profile, rate);
  }
}

TEST(RoomEq, FirstOrderPassesHaveUnityPeakGainRegardlessOfQ) {
  for (const auto type :
       {peq::Type::FirstOrderLowPass, peq::Type::FirstOrderHighPass}) {
    peq::Profile profile;
    profile.shared = {{type, 1200, 0, 1}};
    EXPECT_DOUBLE_EQ(peq::prepare(&profile, 48000, 2).automaticPreampDb, 0);
  }
  peq::Profile resonant;
  resonant.shared = {{peq::Type::LowPass, 1200, 0, 1}};
  EXPECT_NEAR(peq::prepare(&resonant, 48000, 2).automaticPreampDb,
              -10 * std::log10(4.0 / 3), 1e-12);
}

TEST(RoomEq, TwoFirstOrderBandsShareOneSection) {
  using enum peq::Type;
  constexpr unsigned rate = 96000;
  constexpr long double pi = std::numbers::pi_v<long double>;
  peq::Profile profile;
  profile.shared = {{FirstOrderHighPass, 30, 0, 0},
                    {FirstOrderLowPass, 9000, 0, 0},
                    {FirstOrderHighPass, 30, 0, 0}};
  const auto plan = peq::prepare(&profile, rate, 2);
  const auto &pair = plan.pairs[0];
  ASSERT_EQ(pair.count, 2u) << "the first two as one section, the third alone";
  EXPECT_EQ(plan.automaticPreampDb, 0);
  for (double f : {5.0, 30.0, 200.0, 9000.0, 30000.0}) {
    const long double warped = std::tan(pi * f / rate);
    const long double high = warped / std::tan(pi * 30 / rate);
    const long double low = warped / std::tan(pi * 9000 / rate);
    const long double prototype =
        high * high / (1 + high * high) / std::hypot(1, low);
    long double measured = 1;
    for (size_t i = 0; i < pair.count; ++i)
      measured *= gainAt(pair.sections[i], 0, 2 * pi * f / rate);
    EXPECT_NEAR(double(20 * std::log10(measured / prototype)), 0, 1e-6)
        << f << " Hz";
  }
  expectTheReference(pair, profile, rate);
}

TEST(RoomEq, ARepeatedFirstOrderBandIsOneSectionAmongTheModes) {
  using enum peq::Type;
  peq::Profile profile;
  profile.shared = {
      {FirstOrderHighPass, 30, 0, 0}, {FirstOrderHighPass, 30, 0, 0},
      {Peak, 245, -0.46, 2.42},       {Peak, 1261, 1.26, 0.49},
      {Peak, 4966, -1.7, 2.04},       {FirstOrderLowPass, 9000, 0, 0}};
  const auto plan = peq::prepare(&profile, 96000, 2);
  const auto &pair = plan.pairs[0];
  ASSERT_EQ(pair.count, 5u);
#if defined(__aarch64__)
  EXPECT_EQ(bankSizes(pair), std::vector<size_t>{5});
  EXPECT_EQ(serialSections(pair), 0u);
#endif
  expectTheReference(pair, profile, 96000);
}

TEST(RoomEq, RepeatedPolesFillASecondBank) {
  using enum peq::Type;
  const double butterworth = std::numbers::sqrt2 / 2;
  peq::Profile profile;
  // Linkwitz-Riley edges: 24 dB per octave as a section twice, and 12 dB as
  // one section with a real double pole.
  profile.shared = {
      {HighPass, 30, 0, butterworth},   {HighPass, 30, 0, butterworth},
      {LowShelf, 69, 2.87, 0.66},       {Peak, 245, -0.46, 2.42},
      {Peak, 1261, 1.26, 0.49},         {Peak, 4966, -1.7, 2.04},
      {Peak, 10361, -0.75, 1.42},       {LowPass, 18000, 0, butterworth},
      {LowPass, 18000, 0, butterworth}, {HighPass, 12, 0, 0.5}};
  for (unsigned rate : {44100u, 192000u}) {
    SCOPED_TRACE(rate);
    const auto plan = peq::prepare(&profile, rate, 2);
    const auto &pair = plan.pairs[0];
    ASSERT_EQ(pair.count, 10u);
#if defined(__aarch64__)
    EXPECT_EQ(bankSizes(pair), (std::vector<size_t>{5, 5}));
    EXPECT_EQ(serialSections(pair), 0u);
#endif
    expectTheReference(pair, profile, rate);
  }
}

TEST(RoomEq, ALoneRepeatStaysInSeriesBehindWholeGroups) {
  using enum peq::Type;
  peq::Profile profile;
  profile.shared = {{HighPass, 20, 0, std::numbers::sqrt2 / 2},
                    {HighPass, 20, 0, std::numbers::sqrt2 / 2}};
  for (unsigned i = 0; i < 9; ++i)
    profile.shared.push_back(
        {Peak, 70 * std::pow(1.8, i), i % 2 ? -2.5 : 1.5, 1.4});
  const auto plan = peq::prepare(&profile, 192000, 2);
  const auto &pair = plan.pairs[0];
  ASSERT_EQ(pair.count, 11u);
#if defined(__aarch64__)
  EXPECT_EQ(bankSizes(pair), std::vector<size_t>{10});
  EXPECT_EQ(serialSections(pair), 1u);
#endif
  expectTheReference(pair, profile, 192000);
}

TEST(RoomEq, ASteepButterworthEdgeKeepsWholeGroupsOfModes) {
  using enum peq::Type;
  peq::Profile profile;
  for (double q : {0.5098, 0.6013, 0.9, 2.5629})
    profile.shared.push_back({HighPass, 20, 0, q});
  for (unsigned i = 0; i < 8; ++i)
    profile.shared.push_back(
        {Peak, 90 * std::pow(1.9, i), i % 2 ? -2.5 : 1.5, 1.4});
  const auto plan = peq::prepare(&profile, 192000, 2);
  const auto &pair = plan.pairs[0];
  ASSERT_EQ(pair.count, 12u);
#if defined(__aarch64__)
  EXPECT_EQ(bankSizes(pair), std::vector<size_t>{10});
  EXPECT_EQ(serialSections(pair), 2u);
#endif
  expectTheReference(pair, profile, 192000);
}

TEST(RoomEq, ChannelsThatNeedDifferentSerialSectionsShareOneLayout) {
  using enum peq::Type;
  auto profile = std::make_shared<peq::Profile>();
  for (unsigned i = 0; i < 7; ++i)
    profile->left.push_back(
        {Peak, 60 * std::pow(2.1, i), i % 2 ? -3.0 : 2.0, 1.4});
  profile->right = {{HighPass, 40, 0, std::numbers::sqrt2 / 2},
                    {HighPass, 40, 0, std::numbers::sqrt2 / 2},
                    {Peak, 300, -4, 2},
                    {Peak, 2500, 3, 1},
                    {HighShelf, 9000, -2, 0.7}};
  const auto plan = peq::prepare(profile.get(), 96000, 2);
  const auto &pair = plan.pairs[0];
#if defined(__aarch64__)
  EXPECT_EQ(pair.count, 8u) << "seven modes on the left, one section in series";
  EXPECT_EQ(serialSections(pair), 1u);
  EXPECT_EQ(bankSizes(pair, 0), std::vector<size_t>{7});
  EXPECT_EQ(bankSizes(pair, 1), std::vector<size_t>{4});
#else
  EXPECT_EQ(pair.count, 7u);
#endif
  expectTheReference(pair, *profile, 96000);
}

TEST(RoomEq, ARepeatBesideAFullChannelFillsASecondBank) {
  auto profile = std::make_shared<peq::Profile>();
  for (size_t i = 0; i < peq::kBands; ++i)
    profile->left.push_back(
        {peq::Type::Peak, 30 * std::pow(1.35, i), i % 2 ? -3.0 : 2.0, 1.4});
  profile->right = profile->left;
  profile->right.front() = profile->right.back();
  const auto plan = peq::prepare(profile.get(), 192000, 2);
  const auto &pair = plan.pairs[0];
  ASSERT_EQ(pair.count, peq::kBands);
#if defined(__aarch64__)
  EXPECT_EQ(bankSizes(pair, 0), std::vector<size_t>{20});
  EXPECT_EQ(bankSizes(pair, 1), (std::vector<size_t>{15, 5}));
  EXPECT_EQ(serialSections(pair), 0u);
#endif
  expectTheReference(pair, *profile, 192000);
}

TEST(RoomEq, TwoRepeatsBesideAFullChannelUseTheCascade) {
  auto profile = std::make_shared<peq::Profile>();
  for (size_t i = 0; i < peq::kBands; ++i)
    profile->left.push_back(
        {peq::Type::Peak, 30 * std::pow(1.35, i), i % 2 ? -3.0 : 2.0, 1.4});
  profile->right = profile->left;
  profile->right[0] = profile->right[1] = profile->right.back();
  const auto plan = peq::prepare(profile.get(), 192000, 2);
  const auto &pair = plan.pairs[0];
  ASSERT_EQ(pair.count, peq::kBands);
#if defined(__aarch64__)
  EXPECT_FALSE(pair.parallel) << "no history slot is left for the repeat";
#endif
  expectTheReference(pair, *profile, 192000);
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
