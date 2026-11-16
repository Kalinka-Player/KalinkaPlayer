#include "dsp/output/OutputStage.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <mutex>
#include <random>
#include <vector>

namespace {
constexpr unsigned kRate = 48000;

/// Interleaved PCM of one format, read as a stream that can start again.
class PcmSource : public AudioGraphOutputNode {
public:
  PcmSource(StreamAudioFormat format, std::vector<uint8_t> bytes)
      : AudioGraphNode(StreamId(1)), format(format), bytes(std::move(bytes)),
        frameBytes(sampleSize(format.sampleFormat) * format.channels) {
    restart();
  }

  size_t read(void *data, size_t size) override {
    std::lock_guard lock(mutex);
    const size_t n =
        std::min(size, bytes.size() - at) / frameBytes * frameBytes;
    std::memcpy(data, bytes.data() + at, n);
    at += n;
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
    at = std::min(frame * frameBytes, bytes.size());
    return frame;
  }
  std::optional<long> streamReadPosition() const override {
    std::lock_guard lock(mutex);
    return long(at / frameBytes);
  }

  void finish() { setState(StreamState(AudioGraphNodeState::FINISHED)); }
  void restart() {
    {
      std::lock_guard lock(mutex);
      at = 0;
    }
    setState({AudioGraphNodeState::STREAMING, 0,
              StreamInfo{.format = format,
                         .streamType = StreamType::FRAMES,
                         .streamSize = bytes.size() / frameBytes}});
  }

private:
  const StreamAudioFormat format;
  const std::vector<uint8_t> bytes;
  const size_t frameBytes;
  mutable std::mutex mutex;
  size_t at = 0;
};

StreamAudioFormat pcm(AudioSampleFormat format, unsigned channels = 2) {
  return {kRate, channels, unsigned(sampleBits(format)), format, 0};
}

std::vector<uint8_t> randomBytes(size_t count) {
  std::mt19937 random(1);
  std::vector<uint8_t> bytes(count);
  for (auto &byte : bytes)
    byte = uint8_t(random());
  return bytes;
}

template <class T> std::vector<uint8_t> asBytes(const std::vector<T> &values) {
  std::vector<uint8_t> bytes(values.size() * sizeof(T));
  std::memcpy(bytes.data(), values.data(), bytes.size());
  return bytes;
}

/// Everything @p stage gives, read @p chunk bytes at a time.
std::vector<uint8_t> readAll(OutputStage &stage, size_t chunk) {
  std::vector<uint8_t> out, buffer(chunk);
  while (const size_t n = stage.read(buffer.data(), chunk))
    out.insert(out.end(), buffer.begin(), buffer.begin() + long(n));
  return out;
}

template <class T>
std::vector<T> readSamples(OutputStage &stage, size_t count) {
  std::vector<T> out(count);
  out.resize(stage.read(out.data(), count * sizeof(T)) / sizeof(T));
  return out;
}

/// A device that keeps 24 bits of S32, at kRate, in stereo.
const std::vector<StreamAudioFormat> k24In32{{kRate, 2, 24, PCM32_LE, 0}};

struct Stage {
  Stage(StreamAudioFormat format, std::vector<uint8_t> bytes,
        std::vector<StreamAudioFormat> device = {})
      : source(std::make_shared<PcmSource>(format, std::move(bytes))),
        stage(std::make_shared<OutputStage>(std::move(device), 1)) {
    stage->connectTo(source);
  }
  std::shared_ptr<PcmSource> source;
  std::shared_ptr<OutputStage> stage;
};
} // namespace

TEST(OutputStage, AtUnityTheBytesAreUpstreams) {
  const auto bytes = randomBytes(4000 * 4);
  Stage s(pcm(PCM16_LE), bytes);
  EXPECT_EQ(readAll(*s.stage, 1000), bytes);
}

TEST(OutputStage, AtUnityAWiderContainerIsPresentedAndExact) {
  const auto bytes = randomBytes(4000 * 6);
  Stage s(pcm(PCM24_3LE), bytes, k24In32);
  const auto shown = s.stage->getState().streamInfo;
  ASSERT_TRUE(shown);
  EXPECT_EQ(shown->format.sampleFormat, PCM32_LE);
  EXPECT_EQ(shown->format.bitsPerSample, 24u) << "the source's bits stay";
  std::vector<uint8_t> expected(4000 * 8);
  convertSampleFormat(bytes.data(), PCM24_3LE, 8000, expected.data(), PCM32_LE,
                      expected.size());
  EXPECT_EQ(readAll(*s.stage, 800), expected);
}

TEST(OutputStage, WaitsAndReadsCountInThePresentedFormat) {
  Stage s(pcm(PCM24_3LE), randomBytes(100 * 6), k24In32);
  EXPECT_EQ(s.stage->waitForData(std::stop_token(), 800), 800u);
  std::vector<uint8_t> buffer(400);
  EXPECT_EQ(s.stage->read(buffer.data(), 400), 400u);
  EXPECT_EQ(s.source->streamReadPosition(), 50);
}

TEST(OutputStage, SoftwareVolumeRampsOverTwentyFiveMillisecondsThenHolds) {
  constexpr int32_t kHalf = 1 << 30;
  Stage s(pcm(PCM32_LE), asBytes(std::vector<int32_t>(2 * 4000, kHalf)),
          k24In32);
  readSamples<int32_t>(*s.stage, 2 * 100);
  s.stage->setGain(0.5);
  const auto out = readSamples<int32_t>(*s.stage, 2 * 2400);
  ASSERT_EQ(out.size(), 2u * 2400);
  for (size_t i = 0; i < 2400; ++i) {
    const double gain = i < 1200 ? 1.0 - 0.5 * double(i + 1) / 1200.0 : 0.5;
    const double expected = 0.5 * gain * double(1 << 23);
    for (unsigned c = 0; c < 2; ++c)
      ASSERT_LE(std::abs(double(out[2 * i + c] / 256) - expected), 1.5)
          << "frame " << i;
  }
}

TEST(OutputStage, BackAtUnityTheBytesAreUpstreamsAgain) {
  const auto bytes = randomBytes(8000 * 4);
  Stage s(pcm(PCM16_LE), bytes);
  readSamples<int16_t>(*s.stage, 2 * 10);
  s.stage->setGain(0.5);
  readSamples<int16_t>(*s.stage, 2 * 2000);
  s.stage->setGain(1.0);
  readSamples<int16_t>(*s.stage, 2 * 1200);
  const auto rest = readAll(*s.stage, 1000);
  const size_t from = (10 + 2000 + 1200) * 4;
  EXPECT_EQ(rest,
            std::vector<uint8_t>(bytes.begin() + long(from), bytes.end()));
}

TEST(OutputStage, ASeekSettlesTheGainAtOnce) {
  Stage s(pcm(PCM16_LE), asBytes(std::vector<int16_t>(2 * 4000, 1 << 14)));
  readSamples<int16_t>(*s.stage, 2 * 100);
  s.stage->setGain(0.5);
  ASSERT_EQ(s.stage->seekTo(0), 0u);
  for (const int16_t v : readSamples<int16_t>(*s.stage, 2 * 10))
    EXPECT_LE(std::abs(v - (1 << 13)), 1);
}

TEST(OutputStage, OnceNothingPlaysTheNextSourceStartsAtTheGain) {
  Stage s(pcm(PCM16_LE), asBytes(std::vector<int16_t>(2 * 4000, 1 << 14)));
  readSamples<int16_t>(*s.stage, 2 * 100);
  s.source->finish();
  s.stage->setGain(0.5);
  s.source->restart();
  for (const int16_t v : readSamples<int16_t>(*s.stage, 2 * 10))
    EXPECT_LE(std::abs(v - (1 << 13)), 1);
}

TEST(OutputStage, ZeroGainIsDigitalSilence) {
  Stage s(pcm(PCM16_LE), randomBytes(1000 * 4));
  s.stage->setGain(0.0);
  const auto out = readAll(*s.stage, 1000);
  EXPECT_EQ(out.size(), 4000u);
  EXPECT_TRUE(std::ranges::all_of(out, [](uint8_t b) { return b == 0; }));
}

TEST(OutputStage, DsdPassesUntouchedAndRefusesAnyGainButUnity) {
  const auto bytes = randomBytes(1000 * 2);
  const StreamAudioFormat dsd{352800, 2, 1, DSD_U8, 2822400};
  Stage s(dsd, bytes, k24In32);
  EXPECT_EQ(s.stage->getState().streamInfo->format.sampleFormat, DSD_U8);
  std::vector<uint8_t> half(1000);
  ASSERT_EQ(s.stage->read(half.data(), half.size()), half.size());
  EXPECT_TRUE(std::equal(half.begin(), half.end(), bytes.begin()));
  s.stage->setGain(0.5);
  EXPECT_THROW(s.stage->read(half.data(), half.size()), std::runtime_error);
}

TEST(OutputStage, ClampedSamplesAreCounted) {
  Stage s(pcm(PCM16_LE), asBytes(std::vector<int16_t>(2 * 20000, 32767)));
  s.stage->setGain(0.99999);
  readAll(*s.stage, 4000);
  EXPECT_GT(s.stage->clippedSamples(), 0u);
}

TEST(OutputStage, FloatOntoAnIntegerDeviceIsQuantisedAtUnityToo) {
  Stage s(pcm(PCM_FLOAT64_LE), asBytes(std::vector<double>(2 * 1000, 0.5)),
          k24In32);
  const auto shown = s.stage->getState().streamInfo->format;
  EXPECT_EQ(shown.sampleFormat, PCM32_LE);
  EXPECT_EQ(shown.bitsPerSample, 24u) << "the bits it is dithered at";
  for (const int32_t v : readSamples<int32_t>(*s.stage, 2 * 1000)) {
    EXPECT_LE(std::abs(double(v / 256) - double(1 << 22)), 1.5);
    EXPECT_EQ(v % 256, 0) << "24 bits at the top of the container";
  }
}

TEST(OutputStage, MonoStaysMonoForTheSinkToSpread) {
  Stage s(pcm(PCM16_LE, 1), randomBytes(100 * 2), k24In32);
  const auto shown = s.stage->getState().streamInfo;
  ASSERT_TRUE(shown);
  EXPECT_EQ(shown->format.channels, 1u);
  EXPECT_EQ(shown->format.sampleFormat, PCM32_LE);
}
