#include "ByteRing.h"

#include <algorithm>
#include <chrono>
#include <future>
#include <random>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

using namespace std::chrono_literals;

namespace {
std::string readString(ByteRing &ring, size_t bytes) {
  std::string out(bytes, '\0');
  out.resize(ring.read(out.data(), bytes));
  return out;
}

size_t writeString(ByteRing &ring, const std::string &text) {
  return ring.write(text.data(), text.size());
}

/// A wait run on a thread of its own, stopped when the test is done with it,
/// so a wait that never ends fails the test instead of hanging it.
class Waiting {
public:
  template <typename Wait>
  explicit Waiting(Wait wait)
      : result(std::async(std::launch::async,
                          [this, wait] { return wait(source.get_token()); })) {}
  ~Waiting() { source.request_stop(); }

  bool finishes(std::chrono::milliseconds within) {
    return result.wait_for(within) == std::future_status::ready;
  }
  size_t get() { return result.get(); }
  void stop() { source.request_stop(); }

private:
  std::stop_source source;
  std::future<size_t> result;
};

/// Runs @p body on a thread; a body that never returns fails the test
/// instead of hanging it, once @p stop has been requested.
template <typename Body>
void finishesWithin(std::chrono::milliseconds limit, std::stop_source &stop,
                    Body body) {
  auto done = std::async(std::launch::async, body);
  if (done.wait_for(limit) != std::future_status::ready) {
    stop.request_stop();
    done.wait();
    FAIL() << "did not finish within " << limit.count() << " ms";
  }
  done.get();
}
} // namespace

TEST(ByteRing, HandsBytesOverInOrderAcrossTheWrap) {
  ByteRing ring(10);
  EXPECT_EQ(writeString(ring, "abcdefg"), 7u);
  EXPECT_EQ(readString(ring, 5), "abcde");
  EXPECT_EQ(writeString(ring, "hijklmn"), 7u);
  EXPECT_EQ(ring.size(), 9u);
  EXPECT_EQ(readString(ring, 100), "fghijklmn");
  EXPECT_TRUE(ring.empty());
}

TEST(ByteRing, TakesOnlyWhatThereIsRoomFor) {
  ByteRing ring(8);
  EXPECT_EQ(writeString(ring, "0123456789"), 8u);
  EXPECT_EQ(writeString(ring, "x"), 0u);
  EXPECT_EQ(readString(ring, 3), "012");
  EXPECT_EQ(writeString(ring, "89abc"), 3u);
  EXPECT_EQ(readString(ring, 8), "3456789a");
  EXPECT_EQ(readString(ring, 8), "");
}

TEST(ByteRing, AReaderWaitsForAllItAskedFor) {
  ByteRing ring(16);
  Waiting waited(
      [&](std::stop_token token) { return ring.waitForData(token, 8); });
  writeString(ring, "abc");
  EXPECT_FALSE(waited.finishes(50ms)) << "three bytes are not eight";
  writeString(ring, "defgh");
  ASSERT_TRUE(waited.finishes(5s));
  EXPECT_EQ(waited.get(), 8u);
}

TEST(ByteRing, AReaderAsksForAtMostTheCapacity) {
  ByteRing ring(4);
  writeString(ring, "abcd");
  EXPECT_EQ(ring.waitForData({}, 100), 4u);
}

TEST(ByteRing, AReaderStopsWaitingAtTheEnd) {
  ByteRing ring(16);
  Waiting waited(
      [&](std::stop_token token) { return ring.waitForData(token, 8); });
  writeString(ring, "ab");
  ring.setEof();
  ASSERT_TRUE(waited.finishes(5s));
  EXPECT_EQ(waited.get(), 2u) << "what was written before the end";
  EXPECT_TRUE(ring.isEof());
  ring.resetEof();
  EXPECT_FALSE(ring.isEof());
}

TEST(ByteRing, AReaderStopsWaitingWhenItsTokenStops) {
  ByteRing ring(16);
  Waiting waited(
      [&](std::stop_token token) { return ring.waitForData(token, 1); });
  EXPECT_FALSE(waited.finishes(50ms));
  waited.stop();
  ASSERT_TRUE(waited.finishes(5s));
  EXPECT_EQ(waited.get(), 0u);
}

TEST(ByteRing, ATimedWaitGivesUp) {
  ByteRing ring(16);
  writeString(ring, "ab");
  const auto start = std::chrono::steady_clock::now();
  EXPECT_EQ(ring.waitForDataFor({}, 30ms, 8), 2u);
  EXPECT_GE(std::chrono::steady_clock::now() - start, 30ms);
}

TEST(ByteRing, AWriterWaitsForAllTheRoomItAskedFor) {
  ByteRing ring(8);
  writeString(ring, "01234567");
  Waiting waited(
      [&](std::stop_token token) { return ring.waitForSpace(token, 4); });
  readString(ring, 2);
  EXPECT_FALSE(waited.finishes(50ms)) << "two bytes of room are not four";
  readString(ring, 3);
  ASSERT_TRUE(waited.finishes(5s));
  EXPECT_EQ(waited.get(), 5u);
}

TEST(ByteRing, AWriterStopsWaitingWhenItsTokenStops) {
  ByteRing ring(4);
  writeString(ring, "0123");
  Waiting waited(
      [&](std::stop_token token) { return ring.waitForSpace(token, 1); });
  EXPECT_FALSE(waited.finishes(50ms));
  waited.stop();
  ASSERT_TRUE(waited.finishes(5s));
  EXPECT_EQ(waited.get(), 0u);
}

TEST(ByteRing, DiscardFreesTheRoomAtOnceAndHidesWhatItDropped) {
  ByteRing ring(8);
  writeString(ring, "0123");
  readString(ring, 1);
  writeString(ring, "4567");
  ring.discard();
  EXPECT_TRUE(ring.empty());
  EXPECT_EQ(ring.waitForSpace({}, 8), 8u) << "without the reader's help";
  EXPECT_EQ(writeString(ring, "abcdefgh"), 8u);
  EXPECT_EQ(readString(ring, 8), "abcdefgh");
}

TEST(ByteRing, ResizesOnlyWhenEmpty) {
  ByteRing ring(4);
  writeString(ring, "ab");
  EXPECT_THROW(ring.setCapacity(16), std::invalid_argument);
  readString(ring, 2);
  EXPECT_THROW(ring.setCapacity(0), std::invalid_argument);

  ring.setCapacity(16);
  EXPECT_EQ(ring.capacity(), 16u);
  EXPECT_EQ(writeString(ring, "0123456789abcdefXYZ"), 16u);
  EXPECT_EQ(readString(ring, 16), "0123456789abcdef");

  writeString(ring, "x");
  ring.discard();
  ring.setCapacity(3);
  EXPECT_EQ(writeString(ring, "pqrs"), 3u);
  EXPECT_EQ(readString(ring, 4), "pqr");
}

TEST(ByteRing, KeepsEveryByteInOrderBetweenThreads) {
  constexpr size_t kTotal = 1 << 22, kCapacity = 4099;
  // Asks that together exceed the capacity can each wait for the other.
  constexpr size_t kWriterAsks = 64, kReaderAsks = kCapacity - kWriterAsks;
  ByteRing ring(kCapacity);
  std::stop_source stop;
  finishesWithin(20s, stop, [&] {
    // Its own token, so a failed assertion below stops it on the way out.
    std::jthread writer([&](std::stop_token token) {
      std::mt19937 random(1);
      std::vector<uint8_t> chunk(3000);
      size_t sent = 0;
      while (sent < kTotal && !token.stop_requested()) {
        const auto want =
            std::min<size_t>(1 + random() % chunk.size(), kTotal - sent);
        ring.waitForSpace(token, 1 + random() % kWriterAsks);
        for (size_t i = 0; i < want; ++i)
          chunk[i] = uint8_t((sent + i) * 131);
        sent += ring.write(chunk.data(), want);
      }
      ring.setEof();
    });
    std::mt19937 random(2);
    std::vector<uint8_t> chunk(5000);
    size_t received = 0;
    while (!stop.stop_requested()) {
      ring.waitForData(stop.get_token(), 1 + random() % kReaderAsks);
      const auto count = ring.read(chunk.data(), 1 + random() % chunk.size());
      for (size_t i = 0; i < count; ++i)
        ASSERT_EQ(chunk[i], uint8_t((received + i) * 131))
            << "byte " << received + i;
      received += count;
      if (!count && ring.isEof() && ring.empty())
        break;
    }
    EXPECT_EQ(received, kTotal);
  });
}

// A seek discards from the writer while the reader may be mid-copy: the
// reader must never see a byte dropped before it started, nor a torn mix.
TEST(ByteRing, ADiscardRacingAReadLeavesOnlyWholeRunsOfNewData) {
  constexpr uint32_t kRuns = 3000;
  constexpr uint32_t kLastRun = 4000;
  ByteRing ring(4096);
  std::stop_source stop;
  finishesWithin(20s, stop, [&] {
    std::jthread writer([&](std::stop_token token) {
      std::mt19937 random(3);
      std::vector<uint32_t> words(512);
      for (uint32_t run = 1; run <= kRuns && !token.stop_requested(); ++run) {
        ring.discard();
        const uint32_t length = run == kRuns ? kLastRun : 1 + random() % 2000;
        for (uint32_t index = 0; index < length && !token.stop_requested();) {
          const auto batch = std::min<uint32_t>(words.size(), length - index);
          for (uint32_t i = 0; i < batch; ++i)
            words[i] = run << 16 | (index + i);
          ring.waitForSpace(token, 4);
          index += ring.write(words.data(), batch * 4) / 4;
        }
      }
      ring.setEof();
    });
    std::vector<uint32_t> words(700);
    uint32_t run = 0, next = 0;
    while (!stop.stop_requested()) {
      ring.waitForData(stop.get_token(), 4);
      const auto count = ring.read(words.data(), words.size() * 4);
      ASSERT_EQ(count % 4, 0u);
      for (size_t i = 0; i < count / 4; ++i) {
        const uint32_t wordRun = words[i] >> 16, index = words[i] & 0xffff;
        if (wordRun != run) {
          ASSERT_GT(wordRun, run) << "a dropped run came back";
          ASSERT_EQ(index, 0u) << "run " << wordRun << " began part way";
          run = wordRun;
        } else {
          ASSERT_EQ(index, next) << "run " << run << " skipped or repeated";
        }
        next = index + 1;
      }
      if (!count && ring.isEof() && ring.empty())
        break;
    }
    EXPECT_EQ(run, kRuns);
    EXPECT_EQ(next, kLastRun) << "the last run arrives whole";
  });
}
