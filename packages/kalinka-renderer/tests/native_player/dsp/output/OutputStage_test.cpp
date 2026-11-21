#include "dsp/output/OutputStage.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <mutex>
#include <numbers>
#include <random>
#include <string>
#include <thread>
#include <vector>

namespace {
constexpr unsigned kRate = 48000;

/// Interleaved PCM of one format, read as a stream that can start again.
class PcmSource : public AudioGraphOutputNode {
public:
  /// @param decoded What a stage made the samples out of, if any.
  PcmSource(StreamAudioFormat format, std::vector<uint8_t> bytes,
            std::optional<StreamAudioFormat> decoded = std::nullopt)
      : AudioGraphNode(StreamId(1)), format(format), bytes(std::move(bytes)),
        frameBytes(sampleSize(format.sampleFormat) * format.channels),
        decoded(decoded) {
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
                         .streamSize = bytes.size() / frameBytes,
                         .decodedFormat = decoded}});
  }

private:
  const StreamAudioFormat format;
  const std::vector<uint8_t> bytes;
  const size_t frameBytes;
  const std::optional<StreamAudioFormat> decoded;
  mutable std::mutex mutex;
  size_t at = 0;
};

/// DSD64 as DsdContainer hands it to the stage for conversion.
const StreamAudioFormat kRawDsd{352800, 2, 1, DSD_RAW, 2822400};

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
        std::vector<StreamAudioFormat> device = {},
        std::optional<StreamAudioFormat> decoded = std::nullopt)
      : source(std::make_shared<PcmSource>(format, std::move(bytes), decoded)),
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
  EXPECT_FALSE(shown->decodedFormat) << "widened exactly, still as decoded";
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

TEST(OutputStage, TheDsdGainRaisesOnlyDsdConvertedToPcm) {
  const auto quarter = asBytes(std::vector<double>(2 * 1000, 0.25));
  const auto level = [&](bool fromDsd) {
    Stage s(pcm(PCM_FLOAT64_LE), quarter, k24In32,
            fromDsd ? std::optional(kRawDsd) : std::nullopt);
    s.stage->setDsdGain(2.0);
    return readSamples<int32_t>(*s.stage, 2 * 1000);
  };
  for (const int32_t v : level(true))
    ASSERT_LE(std::abs(double(v / 256) - double(1 << 22)), 1.5);
  for (const int32_t v : level(false))
    ASSERT_LE(std::abs(double(v / 256) - double(1 << 21)), 1.5);
}

TEST(OutputStage, ADsdGainChangeRampsAsTheVolumeDoes) {
  Stage s(pcm(PCM_FLOAT64_LE), asBytes(std::vector<double>(2 * 4000, 0.5)),
          k24In32, kRawDsd);
  readSamples<int32_t>(*s.stage, 2 * 100);
  s.stage->setDsdGain(0.5);
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

TEST(OutputStage, TheDsdGainMultipliesTheVolume) {
  Stage s(pcm(PCM_FLOAT64_LE), asBytes(std::vector<double>(2 * 1000, 0.25)),
          k24In32, kRawDsd);
  s.stage->setGain(0.5);
  s.stage->setDsdGain(4.0);
  for (const int32_t v : readSamples<int32_t>(*s.stage, 2 * 1000))
    ASSERT_LE(std::abs(double(v / 256) - double(1 << 22)), 1.5);
}

TEST(OutputStage, ItDescribesTheAppliedFiltersGainsAndDither) {
  OutputStage stage(k24In32, 1);
  const StreamAudioFormat converted{176400, 2, 64, PCM_FLOAT64_LE, 0};
  const StreamAudioFormat s32{176400, 2, 24, PCM32_LE, 0};
  const auto kinds = [&stage](const StreamAudioFormat &decoded,
                              const StreamAudioFormat &source,
                              const StreamAudioFormat &output) {
    std::vector<std::string> kinds;
    for (const auto &step : stage.describe(decoded, source, output))
      kinds.push_back(step.kind + ": " + step.summary);
    return kinds;
  };
  stage.setDsdGain(std::pow(10.0, 2.8 / 20));
  EXPECT_EQ(kinds(kRawDsd, converted, s32),
            (std::vector<std::string>{"dsd_gain: +2.8 dB",
                                      "dither: TPDF at 24 bits"}));
  EXPECT_EQ(kinds(kRawDsd, converted, converted),
            std::vector<std::string>{"dsd_gain: +2.8 dB"})
      << "a device that takes float";
  EXPECT_EQ(kinds(converted, converted, s32),
            std::vector<std::string>{"dither: TPDF at 24 bits"})
      << "float PCM, raised by no DSD gain";
  EXPECT_TRUE(kinds(pcm(PCM16_LE), pcm(PCM16_LE), pcm(PCM32_LE)).empty())
      << "a wider container changes no sample";
  stage.setDsdGain(1.0);
  EXPECT_EQ(kinds(kRawDsd, converted, s32),
            std::vector<std::string>{"dither: TPDF at 24 bits"})
      << "no gain at 0 dB";
  auto profile = std::make_shared<peq::Profile>();
  profile->name = "Room";
  profile->right.push_back({peq::Type::Peak, 500, 3.5, 1});
  stage.setRoomEq({profile, std::nullopt});
  const auto mono = pcm(PCM16_LE, 1);
  EXPECT_EQ(kinds(mono, mono, pcm(PCM32_LE)),
            (std::vector<std::string>{"peq: Room", "preamp: -3.50 dB",
                                      "dither: TPDF at 24 bits"}));
  EXPECT_TRUE(kinds(kRawDsd, kRawDsd, kRawDsd).empty());
  stage.setRoomEq({});
  EXPECT_TRUE(kinds(mono, mono, pcm(PCM32_LE)).empty());
}

TEST(OutputStage, ClippingIsPostedWithTheCountSinceTheSourceBegan) {
  Stage s(pcm(PCM_FLOAT64_LE), asBytes(std::vector<double>(2 * 4000, 2.0)),
          k24In32);
  const auto events = std::make_shared<PlaybackEvents>();
  s.stage->reportTo(events);
  readSamples<int32_t>(*s.stage, 2 * 1000);
  EXPECT_EQ(events->take({}),
            (std::vector<PlaybackEvent>{{StreamId(1), Clipping{2000}}}));
  readSamples<int32_t>(*s.stage, 2 * 500);
  readSamples<int32_t>(*s.stage, 2 * 500);
  EXPECT_EQ(events->take({}),
            (std::vector<PlaybackEvent>{{StreamId(1), Clipping{4000}}}))
      << "the latest of a run";

  s.stage->acceptSourceChange();
  readSamples<int32_t>(*s.stage, 2 * 500);
  EXPECT_EQ(events->take({}),
            (std::vector<PlaybackEvent>{{StreamId(1), Clipping{1000}}}))
      << "counted from the next source's start";
  EXPECT_EQ(s.stage->clippedSamples(), 5000u) << "the total runs on";
}

TEST(OutputStage, NothingIsPostedWhileNothingClips) {
  Stage s(pcm(PCM_FLOAT64_LE), asBytes(std::vector<double>(2 * 4000, 0.5)),
          k24In32);
  const auto events = std::make_shared<PlaybackEvents>();
  s.stage->reportTo(events);
  readAll(*s.stage, 4000);
  std::stop_source stop;
  std::jthread stopper([&stop] {
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    stop.request_stop();
  });
  EXPECT_TRUE(events->take(stop.get_token()).empty());
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
  const auto info = s.stage->getState().streamInfo;
  const auto shown = info->format;
  EXPECT_EQ(shown.sampleFormat, PCM32_LE);
  EXPECT_EQ(shown.bitsPerSample, 24u) << "the bits it is dithered at";
  ASSERT_TRUE(info->decodedFormat);
  EXPECT_EQ(info->decodedFormat->sampleFormat, PCM_FLOAT64_LE);
  for (const int32_t v : readSamples<int32_t>(*s.stage, 2 * 1000)) {
    EXPECT_LE(std::abs(double(v / 256) - double(1 << 22)), 1.5);
    EXPECT_EQ(v % 256, 0) << "24 bits at the top of the container";
  }
}

TEST(OutputStage, MonoReachesBothSpeakersExactlyAndKeepsItsDecodedFormat) {
  const auto bytes = randomBytes(203 * 2);
  for (const auto out : {PCM16_LE, PCM32_LE}) {
    const auto width = sampleSize(out);
    Stage s(pcm(PCM16_LE, 1), bytes, {pcm(out)});
    const auto shown = s.stage->getState().streamInfo;
    ASSERT_TRUE(shown);
    EXPECT_EQ(shown->format.channels, 2u);
    EXPECT_EQ(shown->format.sampleFormat, out);
    ASSERT_TRUE(shown->decodedFormat);
    EXPECT_EQ(shown->decodedFormat->channels, 1u);
    EXPECT_EQ(s.stage->waitForData({}, 204 * width * 2), 203 * width * 2);
    std::vector<uint8_t> mono(203 * width), stereo;
    convertSampleFormat(bytes.data(), PCM16_LE, 203, mono.data(), out,
                        mono.size());
    for (size_t i = 0; i < mono.size(); i += width)
      for (unsigned c = 0; c < 2; ++c)
        stereo.insert(stereo.end(), mono.begin() + i, mono.begin() + i + width);
    EXPECT_EQ(readAll(*s.stage, 17 * width * 2 + 1), stereo);
  }
}

TEST(OutputStage, IndependentSpeakerCorrectionAppliesToMonoDuringPlayback) {
  std::vector<double> mono(2000), stereo;
  for (size_t i = 0; i < mono.size(); ++i) {
    mono[i] = 0.25 * std::sin(2 * std::numbers::pi * 500 * i / kRate);
    stereo.insert(stereo.end(), 2, mono[i]);
  }
  Stage a(pcm(PCM_FLOAT64_LE, 1), asBytes(mono));
  Stage b(pcm(PCM_FLOAT64_LE), asBytes(stereo));
  EXPECT_EQ(readSamples<double>(*a.stage, 200),
            readSamples<double>(*b.stage, 200));
  auto profile = std::make_shared<peq::Profile>();
  profile->right.push_back({peq::Type::Peak, 500, -12, 1});
  a.stage->setRoomEq({profile, 0});
  b.stage->setRoomEq({profile, 0});
  auto corrected = readSamples<double>(*a.stage, 2000);
  ASSERT_EQ(corrected, readSamples<double>(*b.stage, 2000));
  EXPECT_GT(std::abs(corrected[1998] - corrected[1999]), 0.05);
  a.stage->setRoomEq({});
  readSamples<double>(*a.stage, 2 * (480 - 1));
  const auto firstDry = 2 * (100 + 1000 + 480 - 1);
  EXPECT_EQ(
      readAll(*a.stage, 400),
      asBytes(std::vector<double>(stereo.begin() + firstDry, stereo.end())));
}

namespace {
// 10 ms at kRate.
constexpr size_t kFadeFrames = 480;
} // namespace

TEST(OutputStage, AFadeOutFallsToSilenceOverTenMillisecondsThenReadsSilence) {
  constexpr int32_t kHalf = 1 << 30;
  Stage s(pcm(PCM32_LE), asBytes(std::vector<int32_t>(2 * 4000, kHalf)),
          k24In32);
  readSamples<int32_t>(*s.stage, 2 * 100);
  s.stage->fadeOut();
  const auto faded = readSamples<int32_t>(*s.stage, 2 * kFadeFrames);
  ASSERT_EQ(faded.size(), 2 * kFadeFrames);
  for (size_t i = 0; i < kFadeFrames; ++i) {
    const double gain = 1.0 - double(i + 1) / double(kFadeFrames);
    ASSERT_LE(std::abs(double(faded[2 * i] / 256) - gain * double(1 << 22)),
              1.5)
        << "frame " << i;
  }
  for (const int32_t v : readSamples<int32_t>(*s.stage, 2 * 100))
    ASSERT_EQ(v, 0) << "digital silence once faded out";
  EXPECT_EQ(s.source->streamReadPosition(), 100 + long(kFadeFrames) + 100)
      << "a fade reads the frames it fades, and no others";
}

TEST(OutputStage, AFadeInRisesFromSilenceThenTheBytesAreUpstreamsAgain) {
  const auto bytes = randomBytes(4000 * 4);
  Stage s(pcm(PCM16_LE), bytes);
  s.stage->settle(0.0);
  s.stage->fadeIn();
  const auto faded = readSamples<int16_t>(*s.stage, 2 * kFadeFrames);
  std::vector<int16_t> source(2 * kFadeFrames);
  std::memcpy(source.data(), bytes.data(), source.size() * 2);
  for (size_t i = 0; i < kFadeFrames; ++i) {
    const double gain = double(i + 1) / double(kFadeFrames);
    ASSERT_LE(std::abs(double(faded[2 * i]) - gain * source[2 * i]), 1.5)
        << "frame " << i;
  }
  EXPECT_EQ(
      readAll(*s.stage, 1000),
      std::vector<uint8_t>(bytes.begin() + long(kFadeFrames * 4), bytes.end()));
}

TEST(OutputStage, AFadeMultipliesTheVolume) {
  constexpr int16_t kLevel = 1 << 14;
  Stage s(pcm(PCM16_LE), asBytes(std::vector<int16_t>(2 * 4000, kLevel)));
  s.stage->setGain(0.5);
  readSamples<int16_t>(*s.stage, 2 * 10);
  s.stage->fadeOut();
  const auto faded = readSamples<int16_t>(*s.stage, 2 * kFadeFrames);
  for (size_t i = 0; i < kFadeFrames; ++i) {
    const double gain = 0.5 * (1.0 - double(i + 1) / double(kFadeFrames));
    ASSERT_LE(std::abs(double(faded[2 * i]) - gain * kLevel), 1.5)
        << "frame " << i;
  }
}

TEST(OutputStage, AFadeInMidFadeOutTurnsBackFromTheLevelReached) {
  constexpr int16_t kLevel = 1 << 14;
  Stage s(pcm(PCM16_LE), asBytes(std::vector<int16_t>(2 * 4000, kLevel)));
  readSamples<int16_t>(*s.stage, 2 * 10);
  s.stage->fadeOut();
  readSamples<int16_t>(*s.stage, 2 * kFadeFrames / 2);
  s.stage->fadeIn();
  const auto back = readSamples<int16_t>(*s.stage, 2 * kFadeFrames);
  for (size_t i = 0; i < kFadeFrames; ++i) {
    const double gain = 0.5 + 0.5 * double(i + 1) / double(kFadeFrames);
    ASSERT_LE(std::abs(double(back[2 * i]) - gain * kLevel), 1.5)
        << "frame " << i;
  }
}

TEST(OutputStage, IsSilentOnlyOnceAFadeOutHasRunItsCourse) {
  constexpr int16_t kLevel = 1 << 14;
  Stage s(pcm(PCM16_LE), asBytes(std::vector<int16_t>(2 * 4000, kLevel)));
  EXPECT_FALSE(s.stage->silent());
  readSamples<int16_t>(*s.stage, 2 * 10);
  s.stage->fadeOut();
  EXPECT_FALSE(s.stage->silent()) << "the fade is still to be read";
  readSamples<int16_t>(*s.stage, 2 * kFadeFrames / 2);
  EXPECT_FALSE(s.stage->silent()) << "halfway down";
  readSamples<int16_t>(*s.stage, 2 * kFadeFrames / 2);
  EXPECT_TRUE(s.stage->silent());
  s.stage->fadeIn();
  EXPECT_FALSE(s.stage->silent());
  s.stage->fadeOut();
  EXPECT_TRUE(s.stage->silent()) << "the fade in read nothing";
  s.stage->settle(1.0);
  EXPECT_FALSE(s.stage->silent());
  s.stage->settle(0.0);
  EXPECT_TRUE(s.stage->silent());
  s.stage->fadeIn();
  EXPECT_FALSE(s.stage->silent());
}

TEST(OutputStage, DsdIsNotFaded) {
  const auto bytes = randomBytes(1000 * 2);
  Stage s({352800, 2, 1, DSD_U8, 2822400}, bytes, k24In32);
  s.stage->fadeOut();
  EXPECT_EQ(readAll(*s.stage, 500), bytes);
}

TEST(OutputStage, RoomEqCrossfadesToDryAndThenPassesEveryByte) {
  const auto bytes = randomBytes(4096 * 4);
  Stage s(pcm(PCM16_LE), bytes);
  auto profile = std::make_shared<peq::Profile>();
  profile->shared.push_back({peq::Type::Peak, 110, -9, 3});
  s.stage->setRoomEq({profile, 0});
  auto wet = readSamples<int16_t>(*s.stage, 2 * 500);
  EXPECT_NE(asBytes(wet),
            std::vector<uint8_t>(bytes.begin(), bytes.begin() + 2000));
  s.stage->setRoomEq({});
  readSamples<int16_t>(*s.stage, 2 * (480 - 1));
  auto rest = readAll(*s.stage, 732);
  EXPECT_EQ(rest, std::vector<uint8_t>(bytes.begin() + 4 * (500 + 480 - 1),
                                       bytes.end()));
}

TEST(OutputStage, ProfileSwitchBlendsIndependentChannelResponses) {
  std::vector<double> tone(2 * 3000);
  for (size_t i = 0; i < tone.size(); ++i)
    tone[i] = 0.5 * std::sin(2 * std::numbers::pi * 1000 * (i / 2) / kRate);
  const auto make = [](double left, double right) {
    auto p = std::make_shared<peq::Profile>();
    p->left.push_back({peq::Type::Peak, 1000, left, 1});
    p->right.push_back({peq::Type::Peak, 1000, right, 1});
    return peq::Settings{p, 0};
  };
  Stage switching(pcm(PCM_FLOAT64_LE), asBytes(tone));
  Stage old(pcm(PCM_FLOAT64_LE), asBytes(tone));
  Stage fresh(pcm(PCM_FLOAT64_LE), asBytes(tone));
  for (auto *s : {&switching, &old}) {
    s->stage->setRoomEq(make(6, -6));
    readSamples<double>(*s->stage, 2000);
  }
  fresh.stage->seekTo(1000);
  fresh.stage->setRoomEq(make(-3, 4));
  switching.stage->setRoomEq(make(-3, 4));
  const auto a = readSamples<double>(*old.stage, 1000);
  const auto b = readSamples<double>(*fresh.stage, 1000);
  const auto mixed = readSamples<double>(*switching.stage, 1000);
  ASSERT_EQ(mixed.size(), 1000u);
  for (size_t i = 0; i < mixed.size(); ++i)
    EXPECT_NEAR(mixed[i],
                std::lerp(a[i], b[i], std::min(1.0, double(i / 2 + 1) / 480)),
                1e-13);
}

TEST(OutputStage, FractionalPreampRampsThroughTheExistingGainStage) {
  Stage s(pcm(PCM_FLOAT64_LE), asBytes(std::vector<double>(4000, 0.5)));
  readSamples<double>(*s.stage, 200);
  s.stage->setRoomEq({nullptr, -3.5});
  const auto out = readSamples<double>(*s.stage, 2600);
  const double target = std::pow(10.0, -3.5 / 20);
  for (size_t i = 0; i < out.size(); ++i)
    EXPECT_NEAR(
        out[i],
        0.5 * std::lerp(1.0, target, std::min(1.0, double(i / 2 + 1) / 1200)),
        1e-14);
}

TEST(OutputStage, SimultaneousVolumeAndPreampRampsMultiplyAtEachFrame) {
  constexpr size_t frames = kRate / 40;
  const auto signal = asBytes(std::vector<double>(2 * (frames + 1), 1.0));
  for (const size_t chunk : {size_t(37), frames}) {
    Stage s(pcm(PCM_FLOAT64_LE), signal);
    readSamples<double>(*s.stage, 2);
    s.stage->setGain(0.25);
    s.stage->setRoomEq({nullptr, 20 * std::log10(0.5)});
    const auto result = readAll(*s.stage, chunk * 2 * sizeof(double));
    ASSERT_EQ(result.size(), frames * 2 * sizeof(double));
    for (size_t i = 0; i < frames; ++i) {
      double sample;
      std::memcpy(&sample, result.data() + 2 * i * sizeof(double),
                  sizeof(sample));
      const double progress = double(i + 1) / frames;
      ASSERT_NEAR(sample, (1 - 0.75 * progress) * (1 - 0.5 * progress), 1e-13)
          << i;
    }
  }
}
