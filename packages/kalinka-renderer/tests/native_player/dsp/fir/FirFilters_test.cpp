#include "dsp/fir/HalfBandDecimator.h"
#include "dsp/fir/SymmetricFir.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <random>
#include <vector>

namespace {
std::vector<double> noise(size_t count, unsigned seed) {
  std::mt19937 random(seed);
  std::uniform_real_distribution<double> uniform(-1.0, 1.0);
  std::vector<double> out(count);
  for (auto &v : out)
    v = uniform(random);
  return out;
}

/// What the whole filter @p taps makes of @p x, history zero, for every t.
std::vector<double> convolved(const std::vector<double> &taps,
                              const std::vector<double> &x) {
  std::vector<double> y(x.size());
  for (size_t t = 0; t < x.size(); ++t)
    for (size_t j = 0; j < taps.size() && j <= t; ++j)
      y[t] += taps[j] * x[t - j];
  return y;
}

/// A symmetric filter's whole taps from its first half, the centre last.
std::vector<double> symmetricTaps(const std::vector<double> &half) {
  std::vector<double> taps(half);
  taps.insert(taps.end(), half.rbegin() + 1, half.rend());
  return taps;
}

/// A half-band's whole taps from those at odd offsets from its centre.
std::vector<double> halfBandTaps(const std::vector<double> &side) {
  const size_t centre = 2 * side.size() - 1;
  std::vector<double> taps(2 * centre + 1);
  taps[centre] = 0.5;
  for (size_t i = 0; i < side.size(); ++i)
    taps[centre - 2 * i - 1] = taps[centre + 2 * i + 1] = side[i];
  return taps;
}

/// What a half-band of @p side keeps of @p x: the whole filter's every second
/// output.
std::vector<double> halved(const std::vector<double> &side,
                           const std::vector<double> &x) {
  const auto full = convolved(halfBandTaps(side), x);
  std::vector<double> kept;
  for (size_t t = 1; t < full.size(); t += 2)
    kept.push_back(full[t]);
  return kept;
}

/// Fed in blocks of the given sizes, cycled, to the end of @p x.
template <class Filter, class Run>
std::vector<double> inBlocks(Filter &filter, const std::vector<double> &x,
                             const std::vector<size_t> &sizes, Run run) {
  std::vector<double> out, block(filter.capacity() + 1);
  size_t done = 0;
  for (size_t i = 0; done < x.size(); ++i) {
    const size_t count = std::min(sizes[i % sizes.size()], x.size() - done);
    std::copy_n(x.begin() + done, count, filter.input());
    const size_t made = run(filter, count, block.data());
    out.insert(out.end(), block.begin(), block.begin() + made);
    done += count;
  }
  return out;
}

double worstDifference(const std::vector<double> &a,
                       const std::vector<double> &b) {
  double worst = 0;
  for (size_t i = 0; i < a.size(); ++i)
    worst = std::max(worst, std::abs(a[i] - b[i]));
  return worst;
}

const auto filterRun = [](SymmetricFir &fir, size_t count, double *out) {
  fir.filter(count, out);
  return count;
};
const auto decimateRun = [](HalfBandDecimator &d, size_t count, double *out) {
  return d.decimate(count, out);
};
} // namespace

TEST(SymmetricFir, FiltersAsTheWholeTapsConvolve) {
  const auto half = noise(17, 1);
  const auto x = noise(3000, 2);
  const auto expected = convolved(symmetricTaps(half), x);
  for (const auto path : {FirPath::Reference, FirPath::Vector}) {
    SymmetricFir fir(half.data(), half.size(), 256, path);
    const auto out = inBlocks(fir, x, {256, 1, 37, 100, 7}, filterRun);
    ASSERT_EQ(out.size(), expected.size());
    EXPECT_LT(worstDifference(out, expected), 1e-13);
  }
}

TEST(SymmetricFir, WhereABlockEndsDoesNotChangeAVectorOutput) {
  const auto half = noise(30, 3);
  const auto x = noise(5000, 4);
  SymmetricFir a(half.data(), half.size(), 512, FirPath::Vector);
  SymmetricFir b(half.data(), half.size(), 512, FirPath::Vector);
  EXPECT_EQ(inBlocks(a, x, {512}, filterRun),
            inBlocks(b, x, {3, 500, 17, 64, 1}, filterRun));
}

TEST(SymmetricFir, EveryLengthFiltersAlikeWhereverABlockEnds) {
  const auto x = noise(700, 20);
  for (size_t half = 1; half <= 20; ++half) {
    const auto taps = noise(half, 20 + unsigned(half));
    SymmetricFir whole(taps.data(), half, 128, FirPath::Vector);
    SymmetricFir uneven(taps.data(), half, 128, FirPath::Vector);
    const auto out = inBlocks(whole, x, {128}, filterRun);
    ASSERT_EQ(out.size(), x.size());
    EXPECT_LT(worstDifference(out, convolved(symmetricTaps(taps), x)), 1e-13)
        << half << " taps to the centre";
    EXPECT_EQ(inBlocks(uneven, x, {3, 100, 17, 64, 1, 9}, filterRun), out)
        << half << " taps to the centre";
  }
}

TEST(SymmetricFir, ResetForgetsTheHistory) {
  const auto half = noise(9, 5);
  const auto x = noise(400, 6);
  SymmetricFir fir(half.data(), half.size(), 128, FirPath::Vector);
  const auto first = inBlocks(fir, x, {128}, filterRun);
  fir.reset();
  EXPECT_EQ(inBlocks(fir, x, {128}, filterRun), first);
}

TEST(HalfBandDecimator, KeepsEverySecondOutputOfTheWholeFilter) {
  const auto side = noise(11, 7);
  const auto x = noise(3001, 8);
  const auto expected = halved(side, x);
  for (const auto path : {FirPath::Reference, FirPath::Vector}) {
    HalfBandDecimator decimator(side.data(), side.size(), 256, path);
    const auto out =
        inBlocks(decimator, x, {256, 1, 37, 100, 7, 2}, decimateRun);
    ASSERT_EQ(out.size(), expected.size());
    EXPECT_LT(worstDifference(out, expected), 1e-13);
  }
}

TEST(HalfBandDecimator, EveryLengthDecimatesAlikeWhereverABlockEnds) {
  const auto x = noise(700, 50);
  for (size_t sides = 1; sides <= 20; ++sides) {
    const auto side = noise(sides, 50 + unsigned(sides));
    const auto expected = halved(side, x);
    HalfBandDecimator whole(side.data(), sides, 128, FirPath::Vector);
    HalfBandDecimator uneven(side.data(), sides, 128, FirPath::Vector);
    const auto out = inBlocks(whole, x, {128}, decimateRun);
    ASSERT_EQ(out.size(), expected.size());
    EXPECT_LT(worstDifference(out, expected), 1e-13) << sides << " side taps";
    EXPECT_EQ(inBlocks(uneven, x, {3, 100, 17, 64, 1, 9}, decimateRun), out)
        << sides << " side taps";
  }
}

TEST(HalfBandDecimator, AnImpulseComesOutItsDelayLater) {
  const auto side = noise(5, 12);
  HalfBandDecimator decimator(side.data(), side.size(), 64, FirPath::Vector);
  constexpr size_t kAt = 10;
  std::fill_n(decimator.input(), 64, 0.0);
  decimator.input()[2 * kAt] = 1.0;
  std::vector<double> out(HalfBandDecimator::maxOutputs(64));
  out.resize(decimator.decimate(64, out.data()));
  const auto centre = std::ranges::find(out, 0.5);
  ASSERT_NE(centre, out.end());
  EXPECT_EQ(2 * size_t(centre - out.begin()), 2 * kAt + decimator.delay())
      << "output m stands at input 2m";
}

TEST(HalfBandDecimator, AnOddBlockLeavesItsLastInputWaiting) {
  const auto side = noise(4, 9);
  HalfBandDecimator decimator(side.data(), side.size(), 8, FirPath::Vector);
  std::vector<double> out(HalfBandDecimator::maxOutputs(8));
  std::fill_n(decimator.input(), 3, 1.0);
  EXPECT_EQ(decimator.decimate(3, out.data()), 1u);
  std::fill_n(decimator.input(), 1, 1.0);
  EXPECT_EQ(decimator.decimate(1, out.data()), 1u) << "the fourth completes";
}

TEST(HalfBandDecimator, ResetForgetsTheHistoryAndThePhase) {
  const auto side = noise(6, 10);
  const auto x = noise(301, 11);
  HalfBandDecimator decimator(side.data(), side.size(), 64, FirPath::Vector);
  const auto first = inBlocks(decimator, x, {63}, decimateRun);
  decimator.reset();
  EXPECT_EQ(inBlocks(decimator, x, {63}, decimateRun), first);
}

TEST(HalfBandDecimator, HowTheInputIsSplitDoesNotChangeTheOutput) {
  const auto side = noise(11, 13);
  const auto x = noise(2000, 14);
  HalfBandDecimator whole(side.data(), side.size(), 64, FirPath::Vector);
  HalfBandDecimator uneven(side.data(), side.size(), 64, FirPath::Vector);
  EXPECT_EQ(inBlocks(whole, x, {64}, decimateRun),
            inBlocks(uneven, x, {3, 1, 60, 17, 2, 1, 1, 5}, decimateRun));
}

TEST(HalfBandDecimator, AFilterLongerThanTheBlockStillConvolves) {
  const auto side = noise(58, 15);
  const auto x = noise(1500, 16);
  const auto expected = halved(side, x);
  for (const auto path : {FirPath::Reference, FirPath::Vector}) {
    HalfBandDecimator decimator(side.data(), side.size(), 64, path);
    const auto out = inBlocks(decimator, x, {64, 1, 37, 5, 2}, decimateRun);
    ASSERT_EQ(out.size(), expected.size());
    EXPECT_LT(worstDifference(out, expected), 1e-13);
  }
  HalfBandDecimator whole(side.data(), side.size(), 64, FirPath::Vector);
  HalfBandDecimator uneven(side.data(), side.size(), 64, FirPath::Vector);
  EXPECT_EQ(inBlocks(whole, x, {64}, decimateRun),
            inBlocks(uneven, x, {3, 1, 60, 17, 2, 1, 1, 5}, decimateRun));
}

TEST(HalfBandDecimator, AnEmptyBlockChangesNothing) {
  const auto side = noise(7, 17);
  const auto x = noise(500, 18);
  const auto withEmpty = [](HalfBandDecimator &d, size_t count, double *out) {
    EXPECT_EQ(d.decimate(0, out), 0u);
    const size_t made = d.decimate(count, out);
    EXPECT_EQ(d.decimate(0, out + made), 0u);
    return made;
  };
  HalfBandDecimator plain(side.data(), side.size(), 16, FirPath::Vector);
  HalfBandDecimator emptied(side.data(), side.size(), 16, FirPath::Vector);
  EXPECT_EQ(inBlocks(plain, x, {3, 4, 1, 2, 5}, decimateRun),
            inBlocks(emptied, x, {3, 4, 1, 2, 5}, withEmpty));
}
