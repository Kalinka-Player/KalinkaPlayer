#include "AudioStreamSwitcher.h"
#include "dsp/DspStage.h"

#include <gtest/gtest.h>

#include <array>
#include <atomic>
#include <cstring>
#include <deque>
#include <future>
#include <thread>

#include "CountingNode.h"

namespace {
constexpr unsigned kRate = 48000;
constexpr size_t kLatency = 5;

/**
 * Stereo S32 in, float64 out at half the rate: each pair of input frames
 * makes one output once complete, the first frame of the pair, kLatency
 * output frames late, so output frame n is input frame 2n. One that
 * remembers adds the first frame of the pair before, input frame 2n - 2, so
 * it reaches back one output frame.
 */
class HalvingProcessor final : public DspProcessor {
public:
  explicit HalvingProcessor(bool remembers = false) : remembers(remembers) {}

  bool handles(const StreamAudioFormat &format) const override {
    return format.sampleFormat == PCM32_LE && format.channels == 2;
  }
  Output output(const StreamAudioFormat &format) const override {
    auto out = format;
    out.sampleFormat = PCM_FLOAT64_LE;
    out.bitsPerSample = 64;
    out.sampleRate /= 2;
    return {out, 2};
  }
  SignalPath describe(const StreamAudioFormat &) const override {
    return {{{"halving", "Halving", "every other frame"}}, 0};
  }
  size_t start(const StreamAudioFormat &) override {
    fed = 0;
    previous = {0, 0};
    held.assign(kLatency, {0, 0});
    return kLatency;
  }
  size_t history() const override { return remembers ? 1 : 0; }
  size_t process(const uint8_t *in, uint8_t *out, size_t frames) override {
    auto *to = reinterpret_cast<double *>(out);
    size_t made = 0;
    for (size_t i = 0; i < frames; ++i, ++fed) {
      if (fed % 2 == 0) {
        int32_t frame[2] = {0, 0};
        if (in)
          std::memcpy(frame, in + i * 8, 8);
        pairStart = {double(frame[0]), double(frame[1])};
        continue;
      }
      if (remembers) {
        held.push_back({pairStart.first + previous.first,
                        pairStart.second + previous.second});
        previous = pairStart;
      } else {
        held.push_back(pairStart);
      }
      to[2 * made] = held.front().first;
      to[2 * made + 1] = held.front().second;
      held.pop_front();
      ++made;
    }
    return made;
  }

private:
  const bool remembers;
  uint64_t fed = 0;
  std::pair<double, double> pairStart;
  std::pair<double, double> previous;
  std::deque<std::pair<double, double>> held;
};

/// Every left sample a sink would read, taking each source change.
std::vector<double> pullAll(AudioGraphOutputNode &node,
                            std::vector<size_t> *changes = nullptr) {
  std::vector<double> left, buffer(2 * 300);
  for (int idle = 0; idle < 20;) {
    const auto state = node.getState();
    if (state.state == AudioGraphNodeState::SOURCE_CHANGED) {
      if (changes)
        changes->push_back(left.size());
      node.acceptSourceChange();
      continue;
    }
    if (state.state == AudioGraphNodeState::FINISHED ||
        state.state == AudioGraphNodeState::ERROR)
      break;
    const size_t bytes = node.read(buffer.data(), buffer.size() * 8);
    for (size_t i = 0; i < bytes / 16; ++i) {
      left.push_back(buffer[2 * i]);
      EXPECT_EQ(buffer[2 * i + 1], -buffer[2 * i]) << "a channel slipped";
    }
    if (bytes == 0 &&
        node.waitForDataFor(std::stop_token(), std::chrono::milliseconds(10),
                            buffer.size() * 8) == 0)
      ++idle;
  }
  return left;
}

/// The left samples of @p node's even frames in [from, to), as doubles.
std::vector<double> evenFrames(const CountingNode &node, size_t from,
                               size_t to) {
  std::vector<double> out;
  for (const int32_t v : node.values(from, to))
    out.push_back(double(v));
  std::vector<double> even;
  for (size_t i = 0; i < out.size(); i += 2)
    even.push_back(out[i]);
  return even;
}

/// A counting source that buffers before it knows its format, as a decoder
/// still reading its header does.
class LateNode : public CountingNode {
public:
  LateNode(StreamId id, int32_t base, size_t frames)
      : AudioGraphNode(id), CountingNode(id, base, frames) {
    setState(StreamState(AudioGraphNodeState::PREPARING));
  }
  void ready() {
    isReady = true;
    seekTo(0);
  }
  size_t read(void *data, size_t size) override {
    return isReady ? CountingNode::read(data, size) : 0;
  }
  size_t waitForData(std::stop_token token, size_t size) override {
    return isReady ? CountingNode::waitForData(token, size) : 0;
  }

private:
  std::atomic<bool> isReady = false;
};

struct Converting : testing::Test {
  std::shared_ptr<AudioStreamSwitcher> switcher =
      std::make_shared<AudioStreamSwitcher>();
  std::shared_ptr<DspStage> stage =
      std::make_shared<DspStage>(std::make_unique<HalvingProcessor>());

  void SetUp() override { stage->connectTo(switcher); }

  std::shared_ptr<CountingNode> queue(StreamId id, size_t frames,
                                      CountingNode::Options options = {}) {
    auto node = std::make_shared<CountingNode>(id, int32_t(id) * 1000000,
                                               frames, options);
    switcher->connectTo(node);
    return node;
  }
};
} // namespace

TEST_F(Converting, ThePresentedStreamIsTheProcessorsOutput) {
  queue(1, 1000);
  stage->acceptSourceChange();
  const auto info = stage->getState().streamInfo;
  ASSERT_TRUE(info);
  EXPECT_EQ(info->format.sampleFormat, PCM_FLOAT64_LE);
  EXPECT_EQ(info->format.sampleRate, kRate / 2);
  EXPECT_EQ(info->streamSize, 500u);
  ASSERT_TRUE(info->decodedFormat);
  EXPECT_EQ(info->decodedFormat->sampleFormat, PCM32_LE);
  EXPECT_EQ(info->decodedFormat->sampleRate, kRate);
}

TEST_F(Converting, OutputFrameNIsInputFrameTwoNWithTheLatencyHidden) {
  auto node = queue(1, 1000);
  EXPECT_EQ(pullAll(*stage), evenFrames(*node, 0, 1000));
}

TEST_F(Converting, PositionsAreInOutputFrames) {
  queue(1, 1000, {.first = 200});
  stage->acceptSourceChange();
  std::vector<double> buffer(2 * 10);
  ASSERT_EQ(stage->read(buffer.data(), buffer.size() * 8),
            buffer.size() * 8);
  EXPECT_EQ(stage->streamReadPosition(), std::optional<long>(110));
}

TEST_F(Converting, ASourceBeginningPartwayIsThereInOutputFramesBeforeARead) {
  queue(1, 1000, {.first = 200});
  stage->acceptSourceChange();
  EXPECT_EQ(stage->streamReadPosition(), std::optional<long>(100));
}

TEST_F(Converting, AfterASeekTheReadPositionIsInOutputFramesBeforeARead) {
  queue(1, 1000);
  stage->acceptSourceChange();
  std::vector<double> buffer(2 * 50);
  stage->read(buffer.data(), buffer.size() * 8);
  ASSERT_EQ(stage->seekTo(100), 100u);
  EXPECT_EQ(stage->streamReadPosition(), std::optional<long>(100))
      << "where the emitter re-bases its clock after a seek";
}

TEST_F(Converting, ASeekInOutputFramesReachesUpstreamInItsOwn) {
  auto node = queue(1, 1000);
  stage->acceptSourceChange();
  std::vector<double> buffer(2 * 50);
  stage->read(buffer.data(), buffer.size() * 8);
  EXPECT_EQ(stage->seekTo(100), 100u);
  EXPECT_EQ(node->streamReadPosition(), std::optional<long>(200));
  EXPECT_EQ(pullAll(*stage), evenFrames(*node, 200, 1000));
}

TEST_F(Converting, NamedSeekBeforeHeaderTranslatesTheTargetInOutputFrames) {
  auto node = std::make_shared<LateNode>(1, 1000000, 1000);
  switcher->connectTo(node);
  stage->acceptSourceChange();
  ASSERT_EQ(stage->getState().state, AudioGraphNodeState::PREPARING);
  std::jthread header([&] {
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    node->ready();
  });
  ASSERT_EQ(stage->seekStream(1, 100), 100u);
  EXPECT_EQ(node->streamReadPosition(), 200);
  EXPECT_EQ(pullAll(*stage), evenFrames(*node, 200, 1000));
}

TEST_F(Converting, AMissingStreamDoesNotWaitForTheCurrentHeader) {
  auto node = std::make_shared<LateNode>(1, 1000000, 1000);
  switcher->connectTo(node);
  stage->acceptSourceChange();
  auto seek = std::async(std::launch::async,
                         [&] { return stage->seekStream(2, 100); });
  EXPECT_EQ(seek.wait_for(std::chrono::seconds(1)), std::future_status::ready);
  node->ready();
  EXPECT_EQ(seek.get(), size_t(-1));
  EXPECT_EQ(node->streamReadPosition(), 0);
}

TEST_F(Converting, RemovingTheSourceReleasesASeekWaitingForItsHeader) {
  auto node = std::make_shared<LateNode>(1, 1000000, 1000);
  switcher->connectTo(node);
  stage->acceptSourceChange();
  auto seek = std::async(std::launch::async,
                         [&] { return stage->seekStream(1, 100); });
  EXPECT_EQ(seek.wait_for(std::chrono::milliseconds(20)),
            std::future_status::timeout);
  switcher->disconnect(node);
  EXPECT_EQ(seek.wait_for(std::chrono::seconds(1)), std::future_status::ready);
  node->ready();
  EXPECT_EQ(seek.get(), size_t(-1));
}

TEST_F(Converting, AStalledHeaderBoundsTheSeekEvenToFrameZero) {
  auto node = std::make_shared<LateNode>(1, 1000000, 1000);
  switcher->connectTo(node);
  stage->acceptSourceChange();
  auto seek = std::async(std::launch::async,
                         [&] { return stage->seekStream(1, 0); });
  EXPECT_EQ(seek.wait_for(std::chrono::seconds(3)), std::future_status::ready);
  node->ready(); // Release the worker even if the deadline regresses.
  EXPECT_EQ(seek.get(), size_t(-1));
}

TEST_F(Converting, DirectSourceAcceptsANamedSeekWithADifferentId) {
  auto node = std::make_shared<CountingNode>(1, 1000000, 400);
  stage->disconnect(switcher);
  stage->connectTo(node);
  EXPECT_EQ(node->seekStreamInfo(42), node->getState().streamInfo);
  ASSERT_EQ(stage->seekStream(42, 20), 20u);
  EXPECT_EQ(node->streamReadPosition(), 40);
  EXPECT_EQ(pullAll(*stage), evenFrames(*node, 40, 400));
}

TEST_F(Converting, DirectSourceCanBeSoughtAgainAfterItsLastSegmentDrains) {
  auto node = std::make_shared<CountingNode>(1, 1000000, 400);
  stage->disconnect(switcher);
  stage->connectTo(node);
  ASSERT_EQ(pullAll(*stage), evenFrames(*node, 0, 400));
  ASSERT_FALSE(node->getState().streamInfo);
  ASSERT_EQ(stage->seekStream(1, 20), 20u);
  EXPECT_EQ(pullAll(*stage), evenFrames(*node, 40, 400));
}

TEST_F(Converting, AGaplessJoinOfOneFormatCarriesTheRunOn) {
  auto first = queue(1, 400);
  auto second = queue(2, 400);
  std::vector<size_t> changes;
  const auto left = pullAll(*stage, &changes);
  auto expected = evenFrames(*first, 0, 400);
  const auto rest = evenFrames(*second, 0, 400);
  expected.insert(expected.end(), rest.begin(), rest.end());
  EXPECT_EQ(left, expected);
  ASSERT_EQ(changes.size(), 2u) << "the first source, then the join";
  EXPECT_EQ(changes[1], 200u);
}

TEST_F(Converting, AJoinBetweenTwoOutputFramesLosesAndRepeatsNothing) {
  auto first = queue(1, 401);
  auto second = queue(2, 400);
  const auto left = pullAll(*stage);
  // One output per completed pair of the run: the first source's 0, 2 ...
  // 400, the last pairing with the second's frame 0, then the second's 1,
  // 3 ... 397; its frame 399 completes no pair.
  auto expected = evenFrames(*first, 0, 401);
  for (size_t frame = 1; frame < 399; frame += 2)
    expected.push_back(double(second->value(frame)));
  EXPECT_EQ(left.size(), (401u + 400u) / 2);
  EXPECT_EQ(left, expected);
}

TEST_F(Converting, CuttingAfterTheLastWholePairDiscardsTheUnplayedHalfPair) {
  queue(1, 401);
  const auto second = queue(2, 400);
  stage->acceptSourceChange();
  std::array<double, 2 * 200> first{};
  ASSERT_EQ(stage->read(first.data(), sizeof(first)), sizeof(first));
  ASSERT_EQ(switcher->streamId(), 2);
  stage->discard(1);
  EXPECT_EQ(pullAll(*stage), evenFrames(*second, 0, 400));
}

TEST_F(Converting, AJoinWaitsForTheNextSourcesFormatRatherThanEndTheRun) {
  auto first = queue(1, 401);
  auto second = std::make_shared<LateNode>(2, 2000000, 400);
  switcher->connectTo(second);
  std::thread later([&] {
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    second->ready();
  });
  const auto left = pullAll(*stage);
  later.join();
  auto expected = evenFrames(*first, 0, 401);
  for (size_t frame = 1; frame < 399; frame += 2)
    expected.push_back(double(second->value(frame)));
  EXPECT_EQ(left, expected) << "one run across the join, nothing flushed";
}

namespace {
/// What a remembering processor makes of @p node's frames [from, to) in one
/// run under way, each output adding the even frame before its own.
std::vector<double> remembered(const CountingNode &node, size_t from,
                               size_t to) {
  std::vector<double> out;
  for (size_t frame = from; frame < to; frame += 2)
    out.push_back(double(node.value(frame)) +
                  (frame >= 2 ? double(node.value(frame - 2)) : 0.0));
  return out;
}

struct Remembering : Converting {
  Remembering() {
    stage =
        std::make_shared<DspStage>(std::make_unique<HalvingProcessor>(true));
  }
};
} // namespace

TEST_F(Remembering, ASeekStartsEarlySoItsFirstFrameSoundsAsInARunUnderWay) {
  auto node = queue(1, 1000);
  stage->acceptSourceChange();
  std::vector<double> buffer(2 * 50);
  stage->read(buffer.data(), buffer.size() * 8);
  ASSERT_EQ(stage->seekTo(100), 100u);
  EXPECT_EQ(node->streamReadPosition(), std::optional<long>(198))
      << "one output frame early, in input frames";
  EXPECT_EQ(stage->streamReadPosition(), std::optional<long>(100));
  EXPECT_EQ(pullAll(*stage), remembered(*node, 200, 1000));
}

TEST_F(Remembering, ReadingOnFromAnEarlySeekCountsFromTheTarget) {
  queue(1, 1000);
  stage->acceptSourceChange();
  ASSERT_EQ(stage->seekTo(100), 100u);
  std::vector<double> buffer(2 * 10);
  ASSERT_EQ(stage->read(buffer.data(), buffer.size() * 8), buffer.size() * 8);
  EXPECT_EQ(stage->streamReadPosition(), std::optional<long>(110));
}

TEST_F(Remembering, ASeekToTheStartHasNoHistoryToRunThrough) {
  auto node = queue(1, 1000);
  stage->acceptSourceChange();
  ASSERT_EQ(stage->seekTo(0), 0u);
  EXPECT_EQ(node->streamReadPosition(), std::optional<long>(0));
  EXPECT_EQ(pullAll(*stage), remembered(*node, 0, 1000));
}

TEST_F(Remembering, ASourceEndingBeforeTheTargetHandsTheNextOneOverWhole) {
  queue(1, 999, {.knownLength = false});
  auto second = queue(2, 400);
  stage->acceptSourceChange();
  std::vector<double> buffer(2 * 50);
  stage->read(buffer.data(), buffer.size() * 8);
  // Its last output frame is 498: frame 499 never completes, so the history
  // run up to it ends with the source.
  ASSERT_EQ(stage->seekTo(500), 500u);
  EXPECT_EQ(pullAll(*stage), remembered(*second, 0, 400))
      << "the next source starts a run of its own, nothing passed over";
}

TEST_F(Remembering, SeekingPastAKnownPartialPairStartsTheSuccessorCleanly) {
  const auto first = queue(1, 999);
  const auto second = queue(2, 400);
  stage->acceptSourceChange();
  ASSERT_EQ(stage->seekStream(1, 500), 499u);
  EXPECT_EQ(first->streamReadPosition(), 999);
  EXPECT_EQ(pullAll(*stage), remembered(*second, 0, 400));
}

TEST_F(Remembering, ATrackAppendedAfterASeekToEndGetsItsOwnClock) {
  queue(1, 1000);
  stage->acceptSourceChange();
  ASSERT_EQ(stage->seekStream(1, 500), 500u);
  ASSERT_EQ(stage->streamReadPosition(), 500);
  auto next = queue(2, 400, {.first = 20});
  stage->acceptSourceChange();
  EXPECT_EQ(stage->streamReadPosition(), 10);
  std::array<double, 2> frame;
  ASSERT_EQ(stage->read(frame.data(), sizeof(frame)), sizeof(frame));
  EXPECT_EQ(frame[0], next->value(20));
}

namespace {
/// A counting source that seeks to a whole hundred input frames, back from
/// where it is asked to.
class CoarseNode : public CountingNode {
public:
  using CountingNode::CountingNode;
  size_t seekTo(size_t frame) override {
    return CountingNode::seekTo(frame / 100 * 100);
  }
};
} // namespace

TEST_F(Remembering, AnUpstreamLandingFurtherBackPlaysOnFromWhereItLanded) {
  auto node = std::make_shared<CoarseNode>(1, 1000000, 1000);
  switcher->connectTo(node);
  stage->acceptSourceChange();
  std::vector<double> buffer(2 * 50);
  stage->read(buffer.data(), buffer.size() * 8);
  ASSERT_EQ(stage->seekTo(100), 50u) << "input frame 198 asked, 100 reached";
  EXPECT_EQ(stage->streamReadPosition(), std::optional<long>(50));
  auto expected = remembered(*node, 100, 1000);
  expected.front() -= double(node->value(98));
  EXPECT_EQ(pullAll(*stage), expected) << "nothing before frame 100 run in";
}

TEST_F(Remembering, ASeekInAFormatNoRunHasStartedInIsNotEarly) {
  queue(1, 400);
  pullAll(*stage);
  auto node = queue(2, 1000, {.rate = kRate * 2});
  stage->acceptSourceChange();
  ASSERT_EQ(stage->seekStream(2, 100), 100u);
  EXPECT_EQ(node->streamReadPosition(), std::optional<long>(200))
      << "the history known is another format's";
}
