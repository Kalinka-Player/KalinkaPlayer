#include "dsp/FlushFilterTails.h"
#include <gtest/gtest.h>
#include <limits>

namespace {
/// Half the smallest normal double: a subnormal, or zero while flushing.
double halfTheSmallestNormal() {
  volatile double smallest = std::numeric_limits<double>::min();
  volatile double half = smallest / 2;
  return half;
}
} // namespace

// Unflushed, a slow VFP core spends the quiet after each track in support code.
TEST(FlushFilterTails, FlushesSubnormalsWhileHeld) {
  dsp::FlushFilterTails guard;
  EXPECT_EQ(halfTheSmallestNormal(), 0.0);
}

TEST(FlushFilterTails, RestoresTheCallersMode) {
  { dsp::FlushFilterTails guard; }
  EXPECT_GT(halfTheSmallestNormal(), 0.0);
}
