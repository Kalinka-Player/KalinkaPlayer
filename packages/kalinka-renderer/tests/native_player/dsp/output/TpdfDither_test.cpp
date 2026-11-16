#include "dsp/output/TpdfDither.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <vector>

namespace {
std::vector<double> draw(uint64_t seed, size_t count) {
  TpdfDither dither(seed);
  std::vector<double> values(count);
  dither.fill(values.data(), count);
  return values;
}
} // namespace

TEST(TpdfDither, ValuesAreTriangularOverTwoLsb) {
  const auto values = draw(1, 4'000'000);
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

TEST(TpdfDither, NeighbouringValuesAreUncorrelated) {
  const auto values = draw(2, 1'000'000);
  double product = 0;
  for (size_t i = 1; i < values.size(); ++i)
    product += values[i] * values[i - 1];
  // Against the variance, 1/6: channels filled in turn are independent.
  EXPECT_NEAR(product / double(values.size() - 1) * 6.0, 0.0, 0.01);
}

TEST(TpdfDither, ASeedGivesTheSameValuesEveryTime) {
  EXPECT_EQ(draw(7, 1000), draw(7, 1000));
  EXPECT_NE(draw(7, 1000), draw(8, 1000));
}
