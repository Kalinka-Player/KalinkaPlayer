#include "dsp/output/OutputKernels.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <random>
#include <vector>

namespace {
constexpr int32_t kLow = -32768, kHigh = 32767;
constexpr double kScale = 32768.0;

struct Quantised {
  std::vector<int32_t> values;
  size_t clipped = 0;
};

/**
 * Samples in 64ths, gains in 32nds and dither in odd 8ths: every product is
 * exact and no sum falls on a half, so fused or not, any rounding agrees.
 */
struct Input {
  explicit Input(size_t count, unsigned seed) : x(count), dither(count) {
    std::mt19937 random(seed);
    for (size_t i = 0; i < count; ++i) {
      x[i] = double(int(random() % 161) - 80) / 64.0;
      dither[i] = double(2 * int(random() % 8) - 7) / 8.0;
    }
  }
  std::vector<double> x, dither;
};

Quantised expected(const Input &in, double from, double step) {
  Quantised out;
  for (size_t i = 0; i < in.x.size(); ++i) {
    const double gain = from + step * double(i + 1);
    const double v = std::round(in.x[i] * gain * kScale + in.dither[i]);
    out.clipped += v < kLow || v > kHigh;
    out.values.push_back(int32_t(std::clamp(v, double(kLow), double(kHigh))));
  }
  return out;
}

Quantised kernel(const Input &in, double from, double step) {
  Quantised out;
  out.values.resize(in.x.size());
  out.clipped = output_kernels::quantise(in.x.data(), in.x.size(), from, step,
                                         kScale, in.dither.data(), kLow, kHigh,
                                         out.values.data());
  return out;
}
} // namespace

TEST(OutputKernels, HoldsAndRampsOfEveryLengthMatchTheFormula) {
  struct Gain {
    double from, step;
  };
  for (size_t count = 1; count <= 37; ++count) {
    const Input in(count, unsigned(count));
    for (const auto &[from, step] :
         {Gain{1.0, 0.0}, Gain{0.5, 0.0}, Gain{0.5, 1.0 / 32},
          Gain{1.0, -1.0 / 32}}) {
      const auto want = expected(in, from, step);
      const auto got = kernel(in, from, step);
      EXPECT_EQ(got.values, want.values)
          << count << " samples from " << from << " by " << step;
      EXPECT_EQ(got.clipped, want.clipped)
          << count << " samples from " << from << " by " << step;
    }
  }
}

TEST(OutputKernels, OnlyRealSamplesAreCountedAsClipped) {
  for (size_t count = 1; count <= 17; ++count) {
    const std::vector<double> loud(count, 2.0), dither(count, 0.0);
    std::vector<int32_t> out(count);
    EXPECT_EQ(output_kernels::quantise(loud.data(), count, 1.0, 0.0, kScale,
                                       dither.data(), kLow, kHigh, out.data()),
              count);
    EXPECT_TRUE(std::ranges::all_of(out, [](int32_t v) { return v == kHigh; }));
  }
}
