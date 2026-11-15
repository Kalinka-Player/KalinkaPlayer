#include "AudioStreamSwitcher.h"
#include "DelayLine.h"
#include "DspStage.h"
#include "SineWaveNode.h"

#include <gtest/gtest.h>

#include <cstring>
#include <thread>

#include "CountingNode.h"
#include "TestHelpers.h"

namespace {

constexpr unsigned kRate = 48000;

/// No processor at all, a processor with no latency, and a long one.
enum class Setup { PassThrough, ZeroLatency, Delayed };

std::string setupName(Setup setup) {
  switch (setup) {
  case Setup::PassThrough:
    return "PassThrough";
  case Setup::ZeroLatency:
    return "ZeroLatency";
  case Setup::Delayed:
    return "Delayed300ms";
  }
  return "Unknown";
}

// Keeps ctest's names readable rather than "4-byte object <...>".
void PrintTo(Setup setup, std::ostream *out) { *out << setupName(setup); }

std::unique_ptr<DspProcessor> processorFor(Setup setup) {
  switch (setup) {
  case Setup::PassThrough:
    return nullptr;
  case Setup::ZeroLatency:
    return std::make_unique<DelayLine>(std::chrono::milliseconds(0));
  case Setup::Delayed:
    return std::make_unique<DelayLine>(std::chrono::milliseconds(300));
  }
  return nullptr;
}

/// What a sink saw: every left sample in order, and where each state landed.
struct Played {
  std::vector<int32_t> left;
  struct Event {
    size_t at;
    AudioGraphNodeState state;
    std::optional<StreamId> id;
    std::optional<long> position;
  };
  std::vector<Event> events;

  std::vector<size_t> changesAt() const {
    std::vector<size_t> at;
    for (const auto &event : events) {
      if (event.state == AudioGraphNodeState::SOURCE_CHANGED) {
        at.push_back(event.at);
      }
    }
    return at;
  }
};

/// Pulls as the ALSA sink does: reads, waits, takes every source change, and
/// stops at the end of the queue.
Played pull(AudioGraphOutputNode &node, size_t chunkFrames = 1024,
            size_t maxFrames = std::numeric_limits<size_t>::max()) {
  Played played;
  std::vector<int32_t> buffer(chunkFrames * 2);
  for (int idle = 0; idle < 20 && played.left.size() < maxFrames;) {
    const auto state = node.getState();
    if (state.state == AudioGraphNodeState::SOURCE_CHANGED) {
      node.acceptSourceChange();
      played.events.push_back({played.left.size(), state.state, state.streamId,
                               node.streamReadPosition()});
      continue;
    }
    if (state.state == AudioGraphNodeState::FINISHED ||
        state.state == AudioGraphNodeState::ERROR) {
      played.events.push_back(
          {played.left.size(), state.state, state.streamId, std::nullopt});
      break;
    }
    const size_t want = std::min(chunkFrames, maxFrames - played.left.size());
    const size_t bytes = node.read(buffer.data(), want * 8);
    for (size_t i = 0; i < bytes / 8; ++i) {
      played.left.push_back(buffer[2 * i]);
      EXPECT_EQ(buffer[2 * i + 1], -buffer[2 * i]) << "a channel slipped";
    }
    if (bytes == 0 &&
        node.waitForDataFor(std::stop_token(), std::chrono::milliseconds(10),
                            want * 8) == 0 &&
        node.getState().state == state.state) {
      ++idle;
    }
  }
  return played;
}

std::vector<int32_t> counting(const CountingNode &node, size_t from,
                              size_t to) {
  std::vector<int32_t> values;
  for (size_t frame = from; frame < to; ++frame) {
    values.push_back(node.value(frame));
  }
  return values;
}

std::vector<int32_t> joined(std::initializer_list<std::vector<int32_t>> parts) {
  std::vector<int32_t> all;
  for (const auto &part : parts) {
    all.insert(all.end(), part.begin(), part.end());
  }
  return all;
}

class DspStageTest : public testing::TestWithParam<Setup> {
protected:
  std::shared_ptr<AudioStreamSwitcher> switcher =
      std::make_shared<AudioStreamSwitcher>();
  std::shared_ptr<DspStage> stage =
      std::make_shared<DspStage>(processorFor(GetParam()));

  void SetUp() override { stage->connectTo(switcher); }

  std::shared_ptr<CountingNode> queue(StreamId id, size_t frames,
                                      CountingNode::Options options = {}) {
    auto node = std::make_shared<CountingNode>(id, int32_t(id) * 1000000,
                                               frames, options);
    switcher->connectTo(node);
    return node;
  }
};

TEST_P(DspStageTest, AnnouncesTheFirstSourceAndTakesItWhenAccepted) {
  queue(1, 4800);
  EXPECT_EQ(stage->getState().state, AudioGraphNodeState::SOURCE_CHANGED);
  EXPECT_EQ(stage->getState().streamId, std::optional<StreamId>(1));
  stage->acceptSourceChange();
  EXPECT_EQ(stage->getState().state, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(stage->getState().streamId, std::optional<StreamId>(1));
}

TEST_P(DspStageTest, JoinsSameFormatSourcesFrameExactlyAndInOrder) {
  auto first = queue(1, 24000);
  auto second = queue(2, 9600);

  const auto played = pull(*stage);

  EXPECT_EQ(played.left,
            joined({counting(*first, 0, 24000), counting(*second, 0, 9600)}));
  ASSERT_EQ(played.changesAt(), (std::vector<size_t>{0, 24000}))
      << "the incoming source is announced on its first frame, not earlier";
  EXPECT_EQ(played.events[1].id, std::optional<StreamId>(2));
  EXPECT_EQ(played.events[1].position, std::optional<long>(0));
  EXPECT_EQ(played.events.back().state, AudioGraphNodeState::FINISHED);
}

TEST_P(DspStageTest, SourcesShorterThanTheLatencyStillJoinInOrder) {
  auto a = queue(1, 2400);
  auto b = queue(2, 1200);
  auto c = queue(3, 3000);

  const auto played = pull(*stage, 700);

  EXPECT_EQ(played.left, joined({counting(*a, 0, 2400), counting(*b, 0, 1200),
                                 counting(*c, 0, 3000)}));
  EXPECT_EQ(played.changesAt(), (std::vector<size_t>{0, 2400, 3600}));
  EXPECT_EQ(played.events[1].id, std::optional<StreamId>(2));
  EXPECT_EQ(played.events[2].id, std::optional<StreamId>(3));
}

TEST_P(DspStageTest, AJoinBetweenFormatsPlaysTheTailOutFirst) {
  auto first = queue(1, 6000);
  auto second = queue(2, 4410, {.rate = 44100});

  const auto played = pull(*stage);

  EXPECT_EQ(played.left,
            joined({counting(*first, 0, 6000), counting(*second, 0, 4410)}));
  EXPECT_EQ(played.changesAt(), (std::vector<size_t>{0, 6000}));
}

TEST_P(DspStageTest, TheNewSourceShowsItsOwnFormatOnceAccepted) {
  queue(1, 6000);
  queue(2, 4410, {.rate = 44100});
  const auto played = pull(*stage, 1024, 6000);
  ASSERT_EQ(played.left.size(), 6000u);
  std::vector<int32_t> buffer(16);
  ASSERT_EQ(stage->read(buffer.data(), buffer.size() * 4), 0u)
      << "nothing of the next source before it is accepted";
  ASSERT_EQ(stage->getState().state, AudioGraphNodeState::SOURCE_CHANGED);
  stage->acceptSourceChange();
  const auto state = stage->getState();
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(state.streamInfo.has_value());
  EXPECT_EQ(state.streamInfo->format.sampleRate, 44100u);
}

TEST_P(DspStageTest, AStartOffsetIsWhereTheReadPositionBegins) {
  auto node = queue(1, 48000, {.first = 12000});
  stage->acceptSourceChange();
  EXPECT_EQ(stage->streamReadPosition(), std::optional<long>(12000));

  const auto played = pull(*stage, 1000, 6000);

  EXPECT_EQ(played.left, counting(*node, 12000, 18000));
  EXPECT_EQ(stage->streamReadPosition(), std::optional<long>(18000))
      << "what has been handed on, not what has been read ahead";
}

TEST_P(DspStageTest, TheReadPositionFollowsWhatIsHandedOnAcrossAJoin) {
  queue(1, 6000);
  queue(2, 6000, {.first = 3000});
  pull(*stage, 1000, 6000);
  std::vector<int32_t> buffer(16);
  ASSERT_EQ(stage->read(buffer.data(), buffer.size() * 4), 0u);
  ASSERT_EQ(stage->getState().state, AudioGraphNodeState::SOURCE_CHANGED);
  stage->acceptSourceChange();
  EXPECT_EQ(stage->streamReadPosition(), std::optional<long>(3000));
}

TEST_P(DspStageTest, TheEndOfTheQueueComesAfterItsLastFrame) {
  auto only = queue(1, 30000);

  const auto played = pull(*stage, 4096);

  EXPECT_EQ(played.left, counting(*only, 0, 30000));
  ASSERT_FALSE(played.events.empty());
  EXPECT_EQ(played.events.back().state, AudioGraphNodeState::FINISHED);
  EXPECT_EQ(played.events.back().at, 30000u);
}

TEST_P(DspStageTest, AnErrorComesAfterTheFramesBeforeIt) {
  auto broken = queue(1, 30000, {.errorAt = 20000});

  const auto played = pull(*stage, 4096);

  EXPECT_EQ(played.left, counting(*broken, 0, 20000));
  ASSERT_FALSE(played.events.empty());
  EXPECT_EQ(played.events.back().state, AudioGraphNodeState::ERROR);
  EXPECT_EQ(played.events.back().id, std::optional<StreamId>(1));
}

TEST_P(DspStageTest, ASeekStartsFromTheTargetWithNothingHeldOver) {
  auto node = queue(1, 96000);
  pull(*stage, 1000, 10000);

  EXPECT_EQ(stage->seekTo(48000), 48000u);
  EXPECT_EQ(stage->streamReadPosition(), std::optional<long>(48000));
  const auto played = pull(*stage, 1000, 5000);

  EXPECT_EQ(played.left, counting(*node, 48000, 53000));
}

TEST_P(DspStageTest, ASeekAfterTheEndPlaysAgain) {
  auto node = queue(1, 9600);
  pull(*stage);
  ASSERT_EQ(stage->getState().state, AudioGraphNodeState::FINISHED);

  EXPECT_EQ(stage->seekTo(4800), 4800u);
  const auto played = pull(*stage);

  EXPECT_EQ(played.left, counting(*node, 4800, 9600));
}

TEST_P(DspStageTest, RemovingWhatPlaysHandsOverToTheNextAtOnce) {
  auto first = queue(1, 96000);
  auto second = queue(2, 4800);
  pull(*stage, 1000, 8000);

  switcher->disconnect(first);
  stage->discard(1);
  EXPECT_EQ(stage->getState().state, AudioGraphNodeState::SOURCE_CHANGED);
  EXPECT_EQ(stage->getState().streamId, std::optional<StreamId>(2));

  const auto played = pull(*stage);
  EXPECT_EQ(played.left, counting(*second, 0, 4800));
}

TEST_P(DspStageTest, RemovingTheNextSourceKeepsWhatPlays) {
  auto first = queue(1, 6000);
  auto second = queue(2, 48000);
  auto third = queue(3, 2400);
  // Far enough that the second has started upstream behind the first.
  pull(*stage, 500, 5500);

  switcher->disconnect(second);
  stage->discard(2);
  const auto played = pull(*stage);

  EXPECT_EQ(played.left,
            joined({counting(*first, 5500, 6000), counting(*third, 0, 2400)}));
  ASSERT_EQ(played.changesAt().size(), 1u);
  EXPECT_EQ(played.events[0].id, std::optional<StreamId>(3));
}

TEST_P(DspStageTest, ClearingEverythingLeavesNothingToPlay) {
  auto first = queue(1, 96000);
  pull(*stage, 1000, 8000);

  switcher->disconnect(first);
  stage->discardAll();

  EXPECT_EQ(stage->getState().state, AudioGraphNodeState::FINISHED);
  std::vector<int32_t> buffer(2048);
  EXPECT_EQ(stage->read(buffer.data(), buffer.size() * 4), 0u);

  auto next = queue(4, 4800);
  const auto played = pull(*stage);
  EXPECT_EQ(played.left, counting(*next, 0, 4800));
}

TEST_P(DspStageTest, ARemovalWhileNothingReadsStillLetsTheNextSourceIn) {
  auto first = queue(1, 96000);
  pull(*stage, 1000, 8000);

  switcher->disconnect(first);
  stage->discard(1);
  EXPECT_EQ(stage->getState().state, AudioGraphNodeState::FINISHED);
  auto next = queue(2, 4800);
  ASSERT_EQ(stage->getState().state, AudioGraphNodeState::SOURCE_CHANGED);
  stage->acceptSourceChange();
  EXPECT_EQ(stage->getState().state, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(stage->getState().streamId, std::optional<StreamId>(2));

  const auto played = pull(*stage);
  EXPECT_EQ(played.left, counting(*next, 0, 4800));
}

TEST_P(DspStageTest, AWaitReturnsWhenTheCurrentSourceIsRemoved) {
  class Waiting : public AudioGraphOutputNode {
  public:
    Waiting() : AudioGraphNode(StreamId(9)) {}
    size_t read(void *, size_t) override { return 0; }
    size_t waitForData(std::stop_token token, size_t) override {
      while (!token.stop_requested()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
      }
      return 0;
    }
    size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds,
                          size_t size) override {
      return waitForData(token, size);
    }
  };
  auto waiting = std::make_shared<Waiting>();
  switcher->connectTo(waiting);
  stage->acceptSourceChange();

  std::thread remover([this, waiting] {
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    switcher->disconnect(waiting);
    stage->discard(9);
  });
  EXPECT_EQ(stage->waitForData(std::stop_token(), 4), 0u);
  remover.join();
}

TEST_P(DspStageTest, DsdPassesUntouchedWhateverTheProcessor) {
  auto dsd = queue(1, 30000, {.rate = 88200, .format = DSD_U32_BE});
  auto pcm = queue(2, 4800);

  const auto played = pull(*stage, 2000);

  EXPECT_EQ(played.left,
            joined({counting(*dsd, 0, 30000), counting(*pcm, 0, 4800)}));
  EXPECT_EQ(played.changesAt(), (std::vector<size_t>{0, 30000}));
}

INSTANTIATE_TEST_SUITE_P(, DspStageTest,
                         testing::Values(Setup::PassThrough,
                                         Setup::ZeroLatency, Setup::Delayed),
                         [](const testing::TestParamInfo<Setup> &info) {
                           return setupName(info.param);
                         });

TEST(DspStageDelayTest, ReadsTheLatencyAheadOfWhatItHandsOn) {
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  DspStage stage(std::make_unique<DelayLine>(std::chrono::milliseconds(300)));
  stage.connectTo(switcher);
  auto node = std::make_shared<CountingNode>(1, 0, 96000);
  switcher->connectTo(node);
  stage.acceptSourceChange();

  std::vector<int32_t> buffer(2000);
  ASSERT_EQ(stage.read(buffer.data(), buffer.size() * 4), buffer.size() * 4);

  EXPECT_EQ(node->streamReadPosition(), std::optional<long>(1000 + 14400));
  EXPECT_EQ(stage.streamReadPosition(), std::optional<long>(1000));
}

TEST(DspStageDelayTest, ASeekIsRefusedOnceUpstreamHasMovedOn) {
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  DspStage stage(std::make_unique<DelayLine>(std::chrono::milliseconds(300)));
  stage.connectTo(switcher);
  switcher->connectTo(std::make_shared<CountingNode>(1, 0, 4800));
  switcher->connectTo(std::make_shared<CountingNode>(2, 0, 96000));
  stage.acceptSourceChange();

  std::vector<int32_t> buffer(2000);
  ASSERT_EQ(stage.read(buffer.data(), buffer.size() * 4), buffer.size() * 4);
  ASSERT_EQ(switcher->streamId(), std::optional<StreamId>(2))
      << "the second source is already being read behind the first";

  EXPECT_EQ(stage.seekTo(0), size_t(-1))
      << "upstream would seek the second while the first still plays";
}

TEST(DspStageDelayTest, ASourceWithoutASwitcherPlaysToItsEnd) {
  auto source = std::make_shared<SineWaveNode>(1, 440, 1000);
  DspStage stage(std::make_unique<DelayLine>(std::chrono::milliseconds(300)));
  stage.connectTo(source);

  std::vector<int16_t> buffer(48000 * 2);
  size_t total = 0;
  while (const size_t bytes = stage.read(buffer.data(), buffer.size() * 2)) {
    total += bytes;
  }
  EXPECT_EQ(total / 4, 48000u)
      << "the end of the stream plays the held frames out";
  EXPECT_EQ(stage.getState().state, AudioGraphNodeState::FINISHED);
}

/// A source the test feeds by hand; it buffers while it has nothing.
class FedNode : public AudioGraphOutputNode {
public:
  FedNode() : AudioGraphNode(StreamId(1)) {
    setState({AudioGraphNodeState::STREAMING, 0, info});
  }
  void feed(size_t frames, bool last = false) {
    std::vector<int32_t> samples(frames * 2);
    for (size_t i = 0; i < frames; ++i) {
      samples[2 * i] = int32_t(++fed);
      samples[2 * i + 1] = -samples[2 * i];
    }
    buffer.write(reinterpret_cast<uint8_t *>(samples.data()), frames * 8);
    ended = last;
    setState({AudioGraphNodeState::STREAMING, 0, info});
  }
  size_t read(void *data, size_t size) override {
    const size_t bytes = buffer.read(static_cast<uint8_t *>(data), size);
    if (buffer.size() == 0) {
      setState(ended ? StreamState(AudioGraphNodeState::FINISHED)
                     : StreamState(AudioGraphNodeState::PREPARING));
    }
    return bytes;
  }
  size_t waitForData(std::stop_token, size_t) override { return buffer.size(); }
  size_t waitForDataFor(std::stop_token, std::chrono::milliseconds,
                        size_t) override {
    return buffer.size();
  }

private:
  const StreamInfo info{.format = {.sampleRate = kRate,
                                   .channels = 2,
                                   .bitsPerSample = 32,
                                   .sampleFormat = PCM32_LE},
                        .streamType = StreamType::FRAMES};
  Buffer<uint8_t> buffer{1 << 20};
  int32_t fed = 0;
  std::atomic<bool> ended = false;
};

TEST(DspStageDelayTest, ARunDryHoldsTheLatencyBackUntilMoreArrives) {
  auto source = std::make_shared<FedNode>();
  DspStage stage(std::make_unique<DelayLine>(std::chrono::milliseconds(300)));
  stage.connectTo(source);
  std::vector<int32_t> buffer(2 * 48000);
  const auto readAll = [&] {
    std::vector<int32_t> left;
    while (const size_t bytes = stage.read(buffer.data(), buffer.size() * 4)) {
      for (size_t i = 0; i < bytes / 8; ++i) {
        left.push_back(buffer[2 * i]);
      }
    }
    return left;
  };

  source->feed(10000);
  EXPECT_TRUE(readAll().empty()) << "less than the latency is all priming";
  source->feed(20000);
  const auto before = readAll();
  ASSERT_EQ(before.size(), 30000u - 14400u);
  EXPECT_EQ(before.back(), 15600);
  EXPECT_EQ(stage.getState().state, AudioGraphNodeState::PREPARING);

  source->feed(6000, true);
  const auto after = readAll();
  ASSERT_EQ(after.size(), 36000u - 15600u) << "nothing lost to the stall";
  EXPECT_EQ(after.front(), 15601);
  EXPECT_EQ(after.back(), 36000);
  EXPECT_EQ(stage.getState().state, AudioGraphNodeState::FINISHED);
}

TEST(DspStageDelayTest, ASourceStillBufferingIsProcessedAlready) {
  class Buffering : public AudioGraphOutputNode {
  public:
    Buffering() : AudioGraphNode(StreamId(1)) {
      setState({AudioGraphNodeState::PREPARING, 0,
                StreamInfo{.format = {.sampleRate = kRate,
                                      .channels = 2,
                                      .bitsPerSample = 32,
                                      .sampleFormat = PCM32_LE},
                           .streamType = StreamType::FRAMES}});
    }
    size_t read(void *data, size_t size) override {
      size = std::min(size, left);
      std::memset(data, 1, size);
      left -= size;
      return size;
    }
    size_t waitForData(std::stop_token, size_t) override { return left; }
    size_t waitForDataFor(std::stop_token, std::chrono::milliseconds,
                          size_t) override {
      return left;
    }

  private:
    size_t left = 4800 * 8;
  };
  auto source = std::make_shared<Buffering>();
  DspStage stage(std::make_unique<DelayLine>(std::chrono::milliseconds(300)));
  stage.connectTo(source);

  std::vector<int32_t> buffer(2 * 4800);
  EXPECT_EQ(stage.read(buffer.data(), buffer.size() * 4), 0u)
      << "a tenth of a second is still priming 300 ms; untouched it would "
         "have come straight through";
}

TEST(DspStageDelayTest, AStageWithNothingUpstreamHasNothingToGive) {
  DspStage stage(std::make_unique<DelayLine>(std::chrono::milliseconds(300)));
  std::vector<int32_t> buffer(64);
  EXPECT_EQ(stage.read(buffer.data(), buffer.size() * 4), 0u);
  EXPECT_EQ(stage.waitForData(std::stop_token(), 8), 0u);
}

TEST(DspStageDelayTest, AnotherThreadFeedingAndDiscardingKeepsTheFramesInOrder) {
  auto source = std::make_shared<FedNode>();
  DspStage stage(std::make_unique<DelayLine>(std::chrono::milliseconds(50)));
  stage.connectTo(source);
  constexpr int kFeeds = 200;
  constexpr size_t kFrames = 480;
  std::thread feeder([&] {
    for (int i = 0; i < kFeeds; ++i) {
      source->feed(kFrames, i + 1 == kFeeds);
      stage.discard(StreamId(99));
      std::this_thread::sleep_for(std::chrono::microseconds(200));
    }
  });
  std::vector<int32_t> left, buffer(2 * 1024);
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
  while (stage.getState().state != AudioGraphNodeState::FINISHED &&
         std::chrono::steady_clock::now() < deadline) {
    const size_t bytes = stage.read(buffer.data(), buffer.size() * 4);
    for (size_t i = 0; i < bytes / 8; ++i) {
      left.push_back(buffer[2 * i]);
    }
    if (bytes == 0) {
      stage.waitForDataFor(std::stop_token(), std::chrono::milliseconds(2),
                           buffer.size() * 4);
    }
  }
  feeder.join();
  ASSERT_EQ(left.size(), size_t(kFeeds) * kFrames);
  for (size_t i = 0; i < left.size(); ++i) {
    ASSERT_EQ(left[i], int32_t(i + 1)) << "frame " << i;
  }
}

TEST(DelayLineTest, OutputIsTheInputLate) {
  DelayLine delay(std::chrono::milliseconds(1));
  const StreamAudioFormat format{.sampleRate = 8000,
                                 .channels = 1,
                                 .bitsPerSample = 16,
                                 .sampleFormat = PCM16_LE};
  ASSERT_EQ(delay.start(format), 8u);
  std::vector<int16_t> in(20), out(20);
  for (size_t i = 0; i < in.size(); ++i) {
    in[i] = int16_t(i + 1);
  }
  delay.process(reinterpret_cast<const uint8_t *>(in.data()),
                reinterpret_cast<uint8_t *>(out.data()), 13);
  delay.process(reinterpret_cast<const uint8_t *>(in.data() + 13),
                reinterpret_cast<uint8_t *>(out.data() + 13), 7);
  for (size_t i = 0; i < out.size(); ++i) {
    EXPECT_EQ(out[i], i < 8 ? 0 : in[i - 8]) << "frame " << i;
  }
}

TEST(DelayLineTest, SilenceFlushesTheTail) {
  DelayLine delay(std::chrono::milliseconds(1));
  const StreamAudioFormat format{.sampleRate = 4000,
                                 .channels = 2,
                                 .bitsPerSample = 16,
                                 .sampleFormat = PCM16_LE};
  ASSERT_EQ(delay.start(format), 4u);
  std::vector<int16_t> in{1, -1, 2, -2, 3, -3}, out(8 * 2);
  delay.process(reinterpret_cast<const uint8_t *>(in.data()),
                reinterpret_cast<uint8_t *>(out.data()), 3);
  delay.process(nullptr, reinterpret_cast<uint8_t *>(out.data() + 6), 5);
  EXPECT_EQ(out, (std::vector<int16_t>{0, 0, 0, 0, 0, 0, 0, 0, 1, -1, 2, -2,
                                       3, -3, 0, 0}));
}

TEST(DelayLineTest, AStartForgetsEverything) {
  DelayLine delay(std::chrono::milliseconds(1));
  const StreamAudioFormat format{.sampleRate = 2000,
                                 .channels = 1,
                                 .bitsPerSample = 16,
                                 .sampleFormat = PCM16_LE};
  delay.start(format);
  std::vector<int16_t> in{7, 8}, out(2);
  delay.process(reinterpret_cast<const uint8_t *>(in.data()),
                reinterpret_cast<uint8_t *>(out.data()), 2);
  delay.start(format);
  delay.process(nullptr, reinterpret_cast<uint8_t *>(out.data()), 2);
  EXPECT_EQ(out, (std::vector<int16_t>{0, 0}));
}

TEST(DelayLineTest, LeavesDsdAlone) {
  DelayLine delay(std::chrono::milliseconds(100));
  EXPECT_FALSE(delay.handles({.sampleRate = 88200,
                              .channels = 2,
                              .bitsPerSample = 1,
                              .sampleFormat = DSD_U32_BE}));
  EXPECT_TRUE(delay.handles({.sampleRate = 44100,
                             .channels = 2,
                             .bitsPerSample = 16,
                             .sampleFormat = PCM16_LE}));
}

} // namespace
