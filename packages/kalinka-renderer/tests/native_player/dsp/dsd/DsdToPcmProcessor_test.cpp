#include "dsp/dsd/DsdToPcmProcessor.h"

#include "dsp/DspStage.h"

#include <gtest/gtest.h>

#include <cstring>
#include <mutex>
#include <random>
#include <vector>

namespace {
/// Raw DSD as DsdContainer hands it in `pcm` mode.
StreamAudioFormat raw(unsigned dsdRate, unsigned channels = 2) {
  return {dsdRate / 8, channels, 1, DSD_U8, dsdRate};
}

/// A device taking S32 in stereo at each of @p rates.
std::vector<StreamAudioFormat> takes(std::initializer_list<unsigned> rates) {
  std::vector<StreamAudioFormat> device;
  for (const unsigned rate : rates)
    device.push_back({rate, 2, 24, PCM32_LE, 0});
  return device;
}

std::vector<uint8_t> randomBytes(size_t count) {
  std::mt19937 random(1);
  std::vector<uint8_t> bytes(count);
  for (auto &byte : bytes)
    byte = uint8_t(random());
  return bytes;
}

/// Raw DSD that can be sought, a byte a channel to a frame.
class DsdBytes : public AudioGraphOutputNode {
public:
  DsdBytes(StreamAudioFormat format, std::vector<uint8_t> bytes)
      : AudioGraphNode(StreamId(1)), format(format), bytes(std::move(bytes)) {
    seekTo(0);
  }
  size_t read(void *data, size_t size) override {
    std::lock_guard lock(mutex);
    const size_t n =
        std::min(size, bytes.size() - at) / format.channels * format.channels;
    std::memcpy(data, bytes.data() + at, n);
    at += n;
    if (at == bytes.size())
      setState(StreamState(AudioGraphNodeState::FINISHED));
    return n;
  }
  size_t waitForData(std::stop_token = std::stop_token(),
                     size_t size = 1) override {
    std::lock_guard lock(mutex);
    return std::min(size, bytes.size() - at);
  }
  size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds,
                        size_t size) override {
    return waitForData(token, size);
  }
  size_t seekTo(size_t frame) override {
    std::lock_guard lock(mutex);
    at = std::min(frame * format.channels, bytes.size());
    setState({AudioGraphNodeState::STREAMING, long(at / format.channels),
              StreamInfo{.format = format,
                         .streamType = StreamType::FRAMES,
                         .streamSize = bytes.size() / format.channels}});
    return at / format.channels;
  }
  std::optional<long> streamReadPosition() const override {
    std::lock_guard lock(mutex);
    return long(at / format.channels);
  }

private:
  const StreamAudioFormat format;
  const std::vector<uint8_t> bytes;
  mutable std::mutex mutex;
  size_t at = 0;
};

/// The left samples of every frame @p stage hands on from here.
std::vector<double> pullLeft(DspStage &stage) {
  std::vector<double> left, buffer(2 * 512);
  while (true) {
    if (stage.getState().state == AudioGraphNodeState::SOURCE_CHANGED) {
      stage.acceptSourceChange();
      continue;
    }
    const size_t bytes = stage.read(buffer.data(), buffer.size() * 8);
    if (bytes == 0)
      return left;
    for (size_t i = 0; i < bytes / 16; ++i)
      left.push_back(buffer[2 * i]);
  }
}
} // namespace

TEST(DsdToPcmProcessor, TakesRawDsdAndNothingElse) {
  const DsdToPcmProcessor processor({}, 1.0);
  EXPECT_TRUE(processor.handles(raw(2822400)));
  EXPECT_TRUE(processor.handles(raw(3072000, 1)));
  EXPECT_FALSE(processor.handles({352800, 2, 32, DSD_U32_LE, 2822400}));
  EXPECT_FALSE(processor.handles({44100, 2, 16, PCM16_LE, 0}));
  EXPECT_FALSE(processor.handles(raw(2822400 / 2))) << "below DSD64";
}

TEST(DsdToPcmProcessor, ItConvertsToARateTheDeviceTakes) {
  const auto rate = [](std::vector<StreamAudioFormat> device,
                       std::optional<unsigned> multiple = std::nullopt) {
    return DsdToPcmProcessor(std::move(device), 1.0, multiple)
        .output(raw(2822400))
        .format.sampleRate;
  };
  EXPECT_EQ(rate(takes({88200, 176400, 352800})), 176400u);
  EXPECT_EQ(rate(takes({44100, 48000})), 44100u) << "nothing higher taken";
  EXPECT_EQ(rate(takes({88200, 176400}), 2), 88200u) << "as asked";
  EXPECT_EQ(rate({}), 176400u) << "nothing known";
}

TEST(DsdToPcmProcessor, DsdOfAFamilyTheDeviceTakesNoRateOfIsNotHandled) {
  const DsdToPcmProcessor processor(takes({48000, 96000}), 1.0);
  EXPECT_FALSE(processor.handles(raw(2822400)));
  EXPECT_TRUE(processor.handles(raw(3072000)));
}

TEST(DsdToPcmProcessor, TheOutputIsFloatAtTheRateWithTheRatioOfFrames) {
  const auto out = DsdToPcmProcessor({}, 1.0).output(raw(5644800));
  EXPECT_EQ(out.format.sampleFormat, PCM_FLOAT64_LE);
  EXPECT_EQ(out.format.channels, 2u);
  EXPECT_EQ(out.format.sampleRate, 176400u);
  EXPECT_EQ(out.ratio, 4u) << "four DSD128 bytes a channel to a frame";
}

TEST(DsdToPcmProcessor, ItConvertsAsTheConverterDoesTimesTheGain) {
  constexpr double kGain = 1.25;
  // DSD128 takes the 50 kHz filter, and the latency is the converter's.
  DsdToPcm reference({5644800, 176400, 2, DsdFilter::Cut50k,
                      DsdBitOrder::MsbFirst});
  DsdToPcmProcessor processor({}, kGain);
  EXPECT_EQ(processor.start(raw(5644800)), reference.latency());

  const auto in = randomBytes(2 * 6000);
  std::vector<double> left(reference.maxFrames(6000)), right(left.size());
  double *planes[] = {left.data(), right.data()};
  const size_t expected = reference.process(in.data(), 6000, planes);
  std::vector<double> out(2 * (expected + 1));
  ASSERT_EQ(processor.process(in.data(), reinterpret_cast<uint8_t *>(out.data()),
                              6000),
            expected);
  for (size_t i = 0; i < expected; ++i) {
    EXPECT_EQ(out[2 * i], left[i] * kGain);
    EXPECT_EQ(out[2 * i + 1], right[i] * kGain);
  }
}

TEST(DsdToPcmProcessor, FedSilenceSettlesToZero) {
  DsdToPcmProcessor processor({}, 1.0);
  processor.start(raw(2822400));
  const auto in = randomBytes(2 * 2000);
  std::vector<double> out(2 * 2000);
  auto *bytes = reinterpret_cast<uint8_t *>(out.data());
  processor.process(in.data(), bytes, 2000);
  processor.process(nullptr, bytes, 2000);
  const size_t made = processor.process(nullptr, bytes, 2000);
  ASSERT_EQ(made, 1000u);
  for (size_t i = 0; i < 2 * made; ++i)
    EXPECT_EQ(out[i], 0.0);
}

TEST(DsdToPcmProcessor, ItsHistoryIsTheConvertersReachOnceStarted) {
  DsdToPcmProcessor processor({}, 1.0);
  EXPECT_EQ(processor.history(), 0u) << "nothing started";
  const size_t latency = processor.start(raw(5644800));
  EXPECT_EQ(processor.history(), latency + 1);
}

TEST(DsdToPcmProcessor, ASeekThroughTheStageSoundsAsARunUnderWayDoes) {
  const auto format = raw(2822400);
  const auto bytes = randomBytes(2 * 40000);
  const auto play = [&](std::optional<size_t> seek) {
    auto source = std::make_shared<DsdBytes>(format, bytes);
    DspStage stage(std::make_unique<DsdToPcmProcessor>(
        std::vector<StreamAudioFormat>{}, 1.0));
    stage.connectTo(source);
    if (seek) {
      std::vector<double> buffer(2 * 1000);
      stage.read(buffer.data(), buffer.size() * 8);
      EXPECT_EQ(stage.seekTo(*seek), *seek);
    }
    auto left = pullLeft(stage);
    stage.disconnect(source);
    return left;
  };
  const auto whole = play(std::nullopt);
  constexpr size_t kTarget = 12345;
  const auto sought = play(kTarget);
  ASSERT_EQ(whole.size(), 20000u);
  ASSERT_EQ(sought.size(), whole.size() - kTarget);
  for (size_t i = 0; i < sought.size(); ++i)
    ASSERT_NEAR(sought[i], whole[kTarget + i], 1e-12)
        << "output frame " << kTarget + i << " after the seek";
}
