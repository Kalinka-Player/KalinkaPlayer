#include "dsp/dsd/DsdGainSetting.h"

#include <gtest/gtest.h>

#include <stdexcept>

TEST(DsdGainSetting, EachValueIsItsGain) {
  EXPECT_EQ(dsdGainDb("0"), 0.0);
  EXPECT_EQ(dsdGainDb("safe"), 2.8);
  EXPECT_EQ(dsdGainDb("6"), 6.0);
}

TEST(DsdGainSetting, AnythingElseIsRefused) {
  for (const char *setting : {"", "auto", "2.8", "+6", "-6", "6 dB"})
    EXPECT_THROW(dsdGainDb(setting), std::invalid_argument) << setting;
}
