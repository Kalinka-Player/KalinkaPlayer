#include "dsp/output/OutputKernels.h"
#include "dsp/output/TpdfDither.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <limits>
#include <vector>

namespace {
std::vector<double> draw(uint64_t seed, size_t count) {
  TpdfDither dither(seed);
  std::vector<double> values(count);
  dither.fill(values.data(), count);
  return values;
}

/// @p count values from fills of 1 to 7 values in turn.
std::vector<double> drawInPieces(uint64_t seed, size_t count) {
  TpdfDither dither(seed);
  std::vector<double> values(count);
  size_t done = 0;
  for (size_t piece = 1; done < count; piece = piece % 7 + 1) {
    const size_t n = std::min(piece, count - done);
    dither.fill(values.data() + done, n);
    done += n;
  }
  return values;
}

void expectTriangular(const std::vector<double> &values) {
  double sum = 0, squares = 0;
  size_t inner = 0;
  for (const double v : values) {
    ASSERT_GT(v, -1.0);
    ASSERT_LT(v, 1.0);
    sum += v;
    squares += v * v;
    inner += std::abs(v) < 0.5;
  }
  const double n = double(values.size());
  EXPECT_NEAR(sum / n, 0.0, 1e-3);
  EXPECT_NEAR(squares / n, 1.0 / 6.0, 1e-3);
  // A triangle on (-1, 1) holds three quarters of its area within ±0.5.
  EXPECT_NEAR(double(inner) / n, 0.75, 1e-3);
}

/// Neighbours' correlation against the variance, 1/6.
double neighbourCorrelation(const std::vector<double> &values) {
  double product = 0;
  for (size_t i = 1; i < values.size(); ++i)
    product += values[i] * values[i - 1];
  return product / double(values.size() - 1) * 6.0;
}
} // namespace

TEST(TpdfDither, ValuesAreTriangularOverTwoLsb) {
  expectTriangular(draw(1, 4'000'000));
}

TEST(TpdfDither, NeighbouringValuesAreUncorrelated) {
  // Channels filled in turn are independent.
  EXPECT_NEAR(neighbourCorrelation(draw(2, 1'000'000)), 0.0, 0.01);
}

TEST(TpdfDither, FillsOfAnyLengthKeepEveryValueFresh) {
  const auto values = drawInPieces(3, 4'000'000);
  expectTriangular(values);
  EXPECT_NEAR(neighbourCorrelation(values), 0.0, 0.01);
}

TEST(TpdfDither, ASeedGivesTheSameValuesEveryTime) {
  EXPECT_EQ(draw(7, 1000), draw(7, 1000));
  EXPECT_NE(draw(7, 1000), draw(8, 1000));
  EXPECT_EQ(drawInPieces(7, 1000), drawInPieces(7, 1000));
}

TEST(TpdfDither,
     QuantiseKeepsSeededSamplesAndClippingAcrossHoldsRampsAndTails) {
  for (unsigned bits : {16u, 24u, 32u}) {
    TpdfDither actual(42), reference(42);
    const double scale = double(1u << (bits - 1));
    const int32_t low = int32_t(-scale), high = int32_t(scale - 1);
    // Include ties on either side of zero and both clipping thresholds.
    const double levels[] = {0.5,        1.5,       -0.5,       -1.5,
                             high + 0.5, low - 0.5, high + 1.5, low - 1.5};
    for (size_t count : {1, 2, 3, 4, 5, 9, 16, 1023, 1024}) {
      for (double step : {0.0, -0x1p-12, 0.0}) {
        std::vector<double> noise(count), in(count), scratch(count);
        std::vector<int32_t> got(count), expected(count);
        reference.fill(noise.data(), count);
        for (size_t i = 0; i < count; ++i)
          in[i] = (levels[i % std::size(levels)] - noise[i]) / scale;
        if (count >= 16) {
          in[12] = std::numeric_limits<double>::max();
          in[13] = -in[12];
          in[14] = std::numeric_limits<double>::quiet_NaN();
        }
        const auto clipped =
            output_kernels::quantise(in.data(), count, 1.0, step, scale,
                                     noise.data(), low, high, expected.data());
        SCOPED_TRACE(::testing::Message() << bits << " bits, " << count
                                          << " samples, step " << step);
        EXPECT_EQ(actual.quantise(in.data(), count, 1.0, step, scale, low, high,
                                  scratch.data(), got.data()),
                  clipped);
        EXPECT_EQ(got, expected);
      }
    }
  }
}
