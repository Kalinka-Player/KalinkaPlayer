#include "dsp/LoadMeter.h"

#include <gtest/gtest.h>

using namespace std::chrono_literals;

TEST(LoadMeter, ItIsTheTimeTakenOverTheAudioMade) {
  LoadMeter load;
  load.add(10ms, 48000, 48000);
  load.add(20ms, 96000, 96000);
  EXPECT_DOUBLE_EQ(load.take(), 0.015) << "30 ms over 2 s";
}

TEST(LoadMeter, ATakeStartsTheCountOver) {
  LoadMeter load;
  load.add(10ms, 48000, 48000);
  load.take();
  EXPECT_EQ(load.take(), 0.0) << "nothing made since";
  load.add(5ms, 4800, 48000);
  EXPECT_DOUBLE_EQ(load.take(), 0.05);
}

TEST(LoadMeter, AudioWithoutARateIsNotCounted) {
  LoadMeter load;
  load.add(10ms, 48000, 0);
  EXPECT_EQ(load.take(), 0.0);
}
