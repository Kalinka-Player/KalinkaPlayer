#include "BufferedStreamDecoder.h"

#include <gtest/gtest.h>

#include <array>
#include <atomic>
#include <memory>
#include <optional>
#include <span>
#include <thread>
#include <vector>

namespace {
using namespace std::chrono_literals;

constexpr unsigned RATE = 1000;
constexpr uint16_t TOTAL_FRAMES = 300;

/// What a FakeDecoder's hooks saw, kept past the decoder's lifetime.
struct Calls {
  std::atomic<int> closed{0};
  std::atomic<long> lastSeek{-1};
};

/// 16-bit mono frames whose samples count up from 0, a few per block.
class FakeDecoder final : public BufferedStreamDecoder {
public:
  FakeDecoder(std::shared_ptr<Calls> calls, size_t startOffsetMs = 0,
              bool seekable = true, std::optional<uint16_t> failAt = {})
      : AudioGraphNode(1), BufferedStreamDecoder(64, startOffsetMs),
        calls(std::move(calls)), seekable(seekable), failAt(failAt) {
    start();
  }
  ~FakeDecoder() override { stop(); }

private:
  const std::shared_ptr<Calls> calls;
  const bool seekable;
  const std::optional<uint16_t> failAt;
  uint16_t next = 0;

  std::string name() const override { return "Fake"; }
  StreamInfo open() override {
    return {{RATE, 1, 16, PCM16_LE}, StreamType::FRAMES, TOTAL_FRAMES};
  }
  bool decodeNext() override {
    if (next == TOTAL_FRAMES)
      return false;
    if (failAt && next >= *failAt)
      throw SourceFailed{{StreamErrorSource::HTTP_STREAM, "Source went away"}};
    std::array<uint8_t, 14> block;
    size_t size = 0;
    for (; size < block.size() && next < TOTAL_FRAMES; ++next) {
      block[size++] = next & 0xff;
      block[size++] = next >> 8;
    }
    write(std::span(block.data(), size));
    return true;
  }
  void seekSource(uint64_t frame) override {
    calls->lastSeek = long(frame);
    next = uint16_t(frame);
  }
  bool canSeek() const override { return seekable; }
  void close() override { ++calls->closed; }
};

std::vector<uint16_t> drain(BufferedStreamDecoder &node) {
  std::vector<uint16_t> samples;
  std::array<uint8_t, 33> data;
  const auto deadline = std::chrono::steady_clock::now() + 3s;
  while (std::chrono::steady_clock::now() < deadline) {
    const auto state = node.getState().state;
    if (state == AudioGraphNodeState::ERROR ||
        state == AudioGraphNodeState::FINISHED)
      break;
    if (!node.waitForDataFor({}, 10ms, 1))
      continue;
    const auto count = node.read(data.data(), data.size());
    for (size_t i = 0; i + 1 < count; i += 2)
      samples.push_back(data[i] | data[i + 1] << 8);
  }
  return samples;
}

std::vector<uint16_t> countFrom(uint16_t first) {
  std::vector<uint16_t> samples;
  for (auto sample = first; sample < TOTAL_FRAMES; ++sample)
    samples.push_back(sample);
  return samples;
}

void waitStreaming(BufferedStreamDecoder &node) {
  const auto deadline = std::chrono::steady_clock::now() + 2s;
  while (node.getState().state == AudioGraphNodeState::PREPARING &&
         std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
}
} // namespace

TEST(BufferedStreamDecoder, PlaysWholeFramesUntilTheSourceEnds) {
  auto calls = std::make_shared<Calls>();
  FakeDecoder node(calls);
  EXPECT_EQ(drain(node), countFrom(0));
  EXPECT_EQ(node.getState().state, AudioGraphNodeState::FINISHED);
  EXPECT_EQ(node.streamReadPosition(), long(TOTAL_FRAMES));
}

TEST(BufferedStreamDecoder, ClosesTheSourceOnceTheWorkerStops) {
  auto calls = std::make_shared<Calls>();
  {
    FakeDecoder node(calls);
    drain(node);
    EXPECT_EQ(calls->closed, 0);
  }
  EXPECT_EQ(calls->closed, 1);
  {
    FakeDecoder failing(calls, 0, true, 10);
    drain(failing);
  }
  EXPECT_EQ(calls->closed, 2);
}

TEST(BufferedStreamDecoder, StartOffsetSeeksTheSource) {
  auto calls = std::make_shared<Calls>();
  FakeDecoder node(calls, 50);
  EXPECT_EQ(drain(node), countFrom(50));
  EXPECT_EQ(calls->lastSeek, 50);
}

TEST(BufferedStreamDecoder, SeekRestartsFromTheRequestedFrame) {
  auto calls = std::make_shared<Calls>();
  FakeDecoder node(calls);
  waitStreaming(node);
  ASSERT_EQ(node.seekTo(120), 120u);
  EXPECT_EQ(node.streamReadPosition(), 120);
  EXPECT_EQ(drain(node), countFrom(120));
}

TEST(BufferedStreamDecoder, SeekPastTheEndFinishes) {
  auto calls = std::make_shared<Calls>();
  FakeDecoder node(calls);
  waitStreaming(node);
  ASSERT_EQ(node.seekTo(TOTAL_FRAMES + 50), size_t(TOTAL_FRAMES));
  EXPECT_TRUE(drain(node).empty());
  EXPECT_EQ(node.getState().state, AudioGraphNodeState::FINISHED);
}

TEST(BufferedStreamDecoder, AnUnseekableSourceRefusesSeeksAndOffsets) {
  auto calls = std::make_shared<Calls>();
  FakeDecoder node(calls, 0, false);
  waitStreaming(node);
  EXPECT_EQ(node.seekTo(10), size_t(-1));
  EXPECT_EQ(drain(node), countFrom(0));

  FakeDecoder offset(calls, 10, false);
  drain(offset);
  ASSERT_TRUE(offset.getState().error);
  EXPECT_EQ(offset.getState().error->message,
            "Fake start offset requires a seekable stream");
  EXPECT_EQ(calls->lastSeek, -1);
}

TEST(BufferedStreamDecoder, ASourceFailureCarriesTheSourceError) {
  auto calls = std::make_shared<Calls>();
  FakeDecoder node(calls, 0, true, 100);
  drain(node);
  const auto state = node.getState();
  ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
  ASSERT_TRUE(state.error);
  EXPECT_EQ(state.error->source, StreamErrorSource::HTTP_STREAM);
  EXPECT_EQ(state.error->message, "Source went away");
  EXPECT_EQ(node.seekTo(0), size_t(-1));
}
