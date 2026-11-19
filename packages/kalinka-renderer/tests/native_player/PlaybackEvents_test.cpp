#include "PlaybackEvents.h"

#include <gtest/gtest.h>

#include <chrono>
#include <thread>

using namespace std::chrono_literals;

namespace {
PlaybackEvent clipping(uint64_t samples, StreamId stream = 1) {
  return {stream, Clipping{samples}};
}
} // namespace

TEST(PlaybackEvents, WhatWasPostedIsTaken) {
  PlaybackEvents events;
  events.post(clipping(3));
  EXPECT_EQ(events.take({}), std::vector{clipping(3)});
}

TEST(PlaybackEvents, ARunOfOneKindIsTakenAsItsLatest) {
  PlaybackEvents events;
  events.post(clipping(3));
  events.post(clipping(7, 2));
  EXPECT_EQ(events.take({}), std::vector{clipping(7, 2)});
}

TEST(PlaybackEvents, ATakeWaitsForAPost) {
  PlaybackEvents events;
  std::jthread poster([&events] {
    std::this_thread::sleep_for(50ms);
    events.post(clipping(1));
  });
  const auto began = std::chrono::steady_clock::now();
  EXPECT_EQ(events.take({}), std::vector{clipping(1)});
  EXPECT_GE(std::chrono::steady_clock::now() - began, 40ms);
}

TEST(PlaybackEvents, AStopEndsTheWaitWithNothing) {
  PlaybackEvents events;
  std::stop_source stop;
  std::jthread stopper([&stop] {
    std::this_thread::sleep_for(20ms);
    stop.request_stop();
  });
  EXPECT_TRUE(events.take(stop.get_token()).empty());
  events.post(clipping(1));
  EXPECT_TRUE(events.take(stop.get_token()).empty()) << "stopped for good";
}

TEST(PlaybackEvents, WhatWasTakenIsNotTakenAgain) {
  PlaybackEvents events;
  events.post(clipping(1));
  events.take({});
  std::stop_source stop;
  std::jthread stopper([&stop] {
    std::this_thread::sleep_for(20ms);
    stop.request_stop();
  });
  EXPECT_TRUE(events.take(stop.get_token()).empty());
}
