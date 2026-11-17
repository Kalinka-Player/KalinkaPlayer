#include "AudioStreamSwitcher.h"
#include "dsp/DspStage.h"

#include <gtest/gtest.h>

#include <atomic>
#include <cstring>
#include <deque>
#include <thread>

#include "CountingNode.h"

namespace {
constexpr unsigned kRate = 48000;
constexpr size_t kLatency = 5;

/**
 * Stereo S32 in, float64 out at half the rate: each pair of input frames
 * makes one output once complete, the first frame of the pair, kLatency
 * output frames late, so output frame n is input frame 2n.
 */
class HalvingProcessor final : public DspProcessor {
public:
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
  size_t start(const StreamAudioFormat &) override {
    fed = 0;
    held.assign(kLatency, {0, 0});
    return kLatency;
  }
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
      held.push_back(pairStart);
      to[2 * made] = held.front().first;
      to[2 * made + 1] = held.front().second;
      held.pop_front();
      ++made;
    }
    return made;
  }

private:
  uint64_t fed = 0;
  std::pair<double, double> pairStart;
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
      : CountingNode(id, base, frames) {
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

TEST_F(Converting, ASeekInOutputFramesReachesUpstreamInItsOwn) {
  auto node = queue(1, 1000);
  stage->acceptSourceChange();
  std::vector<double> buffer(2 * 50);
  stage->read(buffer.data(), buffer.size() * 8);
  EXPECT_EQ(stage->seekTo(100), 100u);
  EXPECT_EQ(node->streamReadPosition(), std::optional<long>(200));
  EXPECT_EQ(pullAll(*stage), evenFrames(*node, 200, 1000));
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
