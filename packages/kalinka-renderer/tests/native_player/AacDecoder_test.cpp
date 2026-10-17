#include "AacDecoder.h"
#include "AacTestData.h"
#include "DashTestData.h"
#include "FlacFrameDecoder.h"
#include "Mp4SampleTable.h"
#include "TrackDecoder.h"

#include <gtest/gtest.h>

namespace {
using Bytes = std::vector<uint8_t>;

/// The raw access units of the ADTS fixture, headers stripped.
std::vector<Bytes> adtsUnits() {
  const auto file = aacFixture("tones.aac");
  std::vector<Bytes> units;
  for (size_t at = 0; at + 7 <= file.size();) {
    const size_t length =
        (file[at + 3] & 3) << 11 | file[at + 4] << 3 | file[at + 5] >> 5;
    const size_t header = file[at + 1] & 1 ? 7 : 9;
    units.emplace_back(file.begin() + at + header, file.begin() + at + length);
    at += length;
  }
  return units;
}

const Bytes LC_STEREO_44100{0x12, 0x10};

/// Frames counting up from each unit's first frame, `delay` held back.
class FakeCodec final : public SampleDecoder {
public:
  explicit FakeCodec(uint64_t delay, bool padding = true)
      : held(delay), padding(padding) {}
  StreamAudioFormat format() const override { return {1000, 1, 16, PCM16_LE}; }
  uint64_t delay() const override { return held; }
  bool padded() const override { return padding; }
  void reset() override { priming = held > 0; }
  std::span<const uint8_t> decode(std::span<const uint8_t> unit) override {
    if (std::exchange(priming, false))
      return {};
    pcm.clear();
    for (size_t i = 0; i < unit[1]; ++i) {
      pcm.push_back(uint8_t(unit[0] + i));
      pcm.push_back(0);
    }
    return pcm;
  }

private:
  const uint64_t held;
  const bool padding;
  bool priming = false;
  Bytes pcm;
};

/// The first byte of each frame `pcm` holds, frame by frame.
Bytes firsts(std::span<const uint8_t> pcm) {
  Bytes result;
  for (size_t i = 0; i < pcm.size(); i += 2)
    result.push_back(pcm[i]);
  return result;
}
} // namespace

TEST(AacDecoder, DecodesEveryUnitAfterTheFirstToStereo16Bit) {
  AacDecoder decoder(LC_STEREO_44100);
  EXPECT_EQ(decoder.format().sampleRate, 44100u);
  EXPECT_EQ(decoder.format().channels, 2u);
  EXPECT_EQ(decoder.format().sampleFormat, PCM16_LE);
  EXPECT_EQ(decoder.delay(), 1024u);
  const auto units = adtsUnits();
  ASSERT_EQ(units.size(), 55u);
  Bytes pcm;
  EXPECT_TRUE(decoder.decode(units[0]).empty());
  for (size_t i = 1; i < units.size(); ++i) {
    const auto frames = decoder.decode(units[i]);
    ASSERT_EQ(frames.size(), 1024u * 4);
    pcm.insert(pcm.end(), frames.begin(), frames.end());
  }
  pcm.resize(TONES_FRAMES * 4);
  EXPECT_GT(tonesSnr(pcm), 25);
}

TEST(AacDecoder, PrimesAgainAfterAReset) {
  AacDecoder decoder(LC_STEREO_44100);
  const auto units = adtsUnits();
  decoder.decode(units[0]);
  EXPECT_FALSE(decoder.decode(units[1]).empty());
  decoder.reset();
  EXPECT_TRUE(decoder.decode(units[20]).empty());
  EXPECT_EQ(decoder.decode(units[21]).size(), 1024u * 4);
}

TEST(AacDecoder, KnowsHowLongAUnitPlaysWithAndWithoutSbr) {
  const struct {
    Bytes config;
    unsigned rate;
    uint64_t frames;
  } cases[] = {
      {{0x12, 0x10}, 44100, 1024},                   // AAC-LC
      {{0x13, 0x10, 0x56, 0xe5, 0x98}, 48000, 2048}, // HE-AAC, 24 kHz core
      {{0x11, 0x90, 0x56, 0xe5, 0x98}, 48000, 1024}, // downsampled SBR
      {{0x13, 0x90}, 44100, 2048}, // 22.05 kHz LC, upsampled for SBR
      {{0x2b, 0x92, 0x08, 0x00}, 44100, 2048}, // explicit HE-AAC
  };
  for (const auto &test : cases) {
    AacDecoder decoder(test.config);
    EXPECT_EQ(decoder.format().sampleRate, test.rate);
    EXPECT_EQ(decoder.delay(), test.frames);
  }
}

TEST(AacDecoder, RefusesConfigurationsItCannotDecode) {
  EXPECT_THROW(AacDecoder(Bytes{}), std::runtime_error);
  EXPECT_THROW(AacDecoder(Bytes{0x00, 0x00}), std::runtime_error);
  EXPECT_THROW(AacDecoder(Bytes{0x12}), std::runtime_error);
}

TEST(FlacFrameDecoder, DecodesMp4FramesBitExact) {
  const auto file = aacFixture("ramp-flac.mp4");
  const auto table = Mp4SampleTable::parse(file);
  FlacFrameDecoder decoder(table.track.config);
  EXPECT_EQ(decoder.delay(), 0u);
  EXPECT_EQ(decoder.format().bitsPerSample, 16u);
  Bytes pcm;
  for (size_t i = 0; i < table.sizes.size(); ++i) {
    const auto frames = decoder.decode(
        std::span(file).subspan(table.offsets[i], table.sizes[i]));
    pcm.insert(pcm.end(), frames.begin(), frames.end());
  }
  EXPECT_EQ(pcm, rampPcm(16));
}

TEST(FlacFrameDecoder, RefusesACorruptFrameAndCarriesOn) {
  const auto file = aacFixture("ramp-flac.mp4");
  const auto table = Mp4SampleTable::parse(file);
  FlacFrameDecoder decoder(table.track.config);
  Bytes corrupt(file.begin() + table.offsets[0],
                file.begin() + table.offsets[0] + table.sizes[0]);
  corrupt[corrupt.size() / 2] ^= 0xff;
  EXPECT_THROW(decoder.decode(corrupt), std::runtime_error);
  EXPECT_THROW(decoder.decode(std::span(corrupt).first(10)),
               std::runtime_error);
  EXPECT_FALSE(
      decoder.decode(std::span(file).subspan(table.offsets[1], table.sizes[1]))
          .empty());
}

TEST(FlacFrameDecoder, RefusesUnsupportedStreamInfo) {
  const auto table = Mp4SampleTable::parse(aacFixture("ramp-flac.mp4"));
  EXPECT_THROW(FlacFrameDecoder(std::span(table.track.config).first(33)),
               std::runtime_error);
  auto surround = table.track.config;
  surround[12] = (surround[12] & 0xf1) | (5 << 1);
  EXPECT_THROW(FlacFrameDecoder{surround}, std::runtime_error);
}

TEST(TrackDecoder, KeepsFramesFromTheTargetOn) {
  TrackDecoder track(std::make_unique<FakeCodec>(0));
  track.restart(15);
  EXPECT_TRUE(track.decode(Bytes{0, 10}, 0, 10).empty());
  EXPECT_EQ(firsts(track.decode(Bytes{10, 10}, 10, 10)),
            (Bytes{15, 16, 17, 18, 19}));
  EXPECT_EQ(firsts(track.decode(Bytes{20, 10}, 20, 10)).front(), 20);
}

TEST(TrackDecoder, ADelayedCodecPrimesOnTheFirstUnit) {
  TrackDecoder track(std::make_unique<FakeCodec>(10));
  track.restart(15);
  EXPECT_TRUE(track.decode(Bytes{0, 10}, 0, 10).empty());
  EXPECT_EQ(firsts(track.decode(Bytes{10, 10}, 10, 10)).front(), 15);
}

TEST(TrackDecoder, AShortUnitTrimsItsPaddingAndALongOneFails) {
  TrackDecoder track(std::make_unique<FakeCodec>(0));
  track.restart(0);
  EXPECT_EQ(firsts(track.decode(Bytes{0, 10}, 0, 4)), (Bytes{0, 1, 2, 3}));
  EXPECT_THROW(track.decode(Bytes{4, 10}, 4, 11), std::runtime_error);
}

TEST(TrackDecoder, AnUnpaddedCodecMustFillEachUnitExactly) {
  TrackDecoder track(std::make_unique<FakeCodec>(0, false));
  track.restart(0);
  EXPECT_EQ(firsts(track.decode(Bytes{0, 4}, 0, 4)), (Bytes{0, 1, 2, 3}));
  EXPECT_THROW(track.decode(Bytes{4, 10}, 4, 4), std::runtime_error);
}

TEST(TrackDecoder, AGapInTheTimelineFails) {
  TrackDecoder track(std::make_unique<FakeCodec>(0));
  track.restart(0);
  track.decode(Bytes{0, 10}, 0, 10);
  EXPECT_THROW(track.decode(Bytes{20, 10}, 20, 10), std::runtime_error);
}
