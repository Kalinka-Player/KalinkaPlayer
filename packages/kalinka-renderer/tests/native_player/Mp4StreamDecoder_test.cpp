#include "AacTestData.h"
#include "AudioGraphHttpStream.h"
#include "ContainerTestSupport.h"
#include "DashTestData.h"
#include "FileInputNode.h"
#include "LocalHttpServer.h"
#include "Mp4StreamDecoder.h"

#include <gtest/gtest.h>

#include <thread>

namespace {
using container_test::Bytes;
using container_test::BytesInput;
using container_test::drain;
using namespace std::chrono_literals;

constexpr size_t BUFFER_SIZE = 16384;
const char *const LAYOUTS[] = {"tones.m4a", "tones-index-last.m4a",
                               "tones-fragmented.m4a",
                               "tones-fragmented-sidx.m4a"};

Bytes decodeFile(const std::string &name, size_t startOffsetMs = 0) {
  Mp4StreamDecoder decoder(1, BUFFER_SIZE, startOffsetMs);
  decoder.connectTo(std::make_shared<FileInputNode>(1, aacFixturePath(name)));
  return drain(decoder);
}

void waitStreaming(AudioGraphOutputNode &node) {
  const auto deadline = std::chrono::steady_clock::now() + 3s;
  while (node.getState().state == AudioGraphNodeState::PREPARING &&
         std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  ASSERT_EQ(node.getState().state, AudioGraphNodeState::STREAMING);
}

const Bytes &reference() {
  static const auto pcm = decodeFile("tones.m4a");
  return pcm;
}
} // namespace

TEST(Mp4StreamDecoder, EveryLayoutPlaysTheSameAudio) {
  ASSERT_EQ(reference().size(), TONES_FRAMES * 4);
  EXPECT_GT(tonesSnr(reference()), 25);
  for (const auto *name : LAYOUTS) {
    SCOPED_TRACE(name);
    Mp4StreamDecoder decoder(1, BUFFER_SIZE);
    decoder.connectTo(std::make_shared<FileInputNode>(1, aacFixturePath(name)));
    waitStreaming(decoder);
    const auto info = decoder.getState().streamInfo;
    ASSERT_TRUE(info);
    EXPECT_EQ(info->format.sampleRate, 44100u);
    EXPECT_EQ(info->format.channels, 2u);
    EXPECT_EQ(info->format.sampleFormat, PCM16_LE);
    EXPECT_EQ(drain(decoder), reference());
    EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
    EXPECT_EQ(decoder.streamReadPosition(), long(TONES_FRAMES));
  }
}

TEST(Mp4StreamDecoder, KnowsTheLengthOfEveryLayout) {
  for (const auto *name : LAYOUTS) {
    SCOPED_TRACE(name);
    Mp4StreamDecoder decoder(1, BUFFER_SIZE);
    decoder.connectTo(std::make_shared<FileInputNode>(1, aacFixturePath(name)));
    waitStreaming(decoder);
    EXPECT_EQ(decoder.getState().streamInfo->streamSize, TONES_FRAMES);
  }
}

TEST(Mp4StreamDecoder, PlaysFragmentsWithoutAnIndexButCannotSeek) {
  auto file = aacFixture("tones-fragmented.m4a");
  const auto mfra = mp4::boxes(file).back();
  ASSERT_EQ(mfra.type, "mfra");
  file.resize(mfra.offset);
  Mp4StreamDecoder decoder(1, BUFFER_SIZE);
  decoder.connectTo(std::make_shared<BytesInput>(file));
  waitStreaming(decoder);
  EXPECT_EQ(decoder.getState().streamInfo->streamSize, 0u);
  EXPECT_EQ(decoder.seekTo(1000), size_t(-1));
  EXPECT_EQ(drain(decoder), reference());
}

TEST(Mp4StreamDecoder, PlaysFlacInMp4BitExact) {
  EXPECT_EQ(decodeFile("ramp-flac.mp4"), rampPcm(16));
  EXPECT_EQ(decodeFile("ramp-flac.mp4", 500), rampPcm(16, 22050));
}

TEST(Mp4StreamDecoder, SeeksInEveryLayout) {
  for (const auto *name : LAYOUTS) {
    for (size_t target : {size_t(0), size_t(1000), size_t(30000), size_t(43008),
                          TONES_FRAMES}) {
      SCOPED_TRACE(std::string(name) + " " + std::to_string(target));
      Mp4StreamDecoder decoder(1, BUFFER_SIZE);
      decoder.connectTo(
          std::make_shared<FileInputNode>(1, aacFixturePath(name)));
      waitStreaming(decoder);
      ASSERT_EQ(decoder.seekTo(target), target);
      const auto pcm = drain(decoder);
      ASSERT_EQ(pcm.size(), (TONES_FRAMES - target) * 4);
      if (!pcm.empty())
        EXPECT_GT(tonesSnr(pcm, target), 22);
      EXPECT_EQ(decoder.streamReadPosition(), long(TONES_FRAMES));
    }
  }
}

TEST(Mp4StreamDecoder, StartsAtAnOffset) {
  for (const auto *name : LAYOUTS) {
    SCOPED_TRACE(name);
    const auto pcm = decodeFile(name, 500);
    ASSERT_EQ(pcm.size(), (TONES_FRAMES - 22050) * 4);
    EXPECT_GT(tonesSnr(pcm, 22050), 22);
  }
}

TEST(Mp4StreamDecoder, PlaysWhatASequentialSourceAllows) {
  for (const auto *name : {"tones.m4a", "tones-fragmented.m4a"}) {
    SCOPED_TRACE(name);
    Mp4StreamDecoder decoder(1, BUFFER_SIZE);
    decoder.connectTo(
        std::make_shared<BytesInput>(aacFixture(name), false, false));
    waitStreaming(decoder);
    EXPECT_EQ(decoder.seekTo(1000), size_t(-1));
    EXPECT_EQ(drain(decoder), reference());
  }
  Mp4StreamDecoder late(1, BUFFER_SIZE);
  late.connectTo(
      std::make_shared<BytesInput>(aacFixture("tones-index-last.m4a"), false));
  EXPECT_TRUE(drain(late).empty());
  ASSERT_TRUE(late.getState().error);
  EXPECT_EQ(late.getState().error->message,
            "This MP4 file has its index after the audio and needs a "
            "seekable source");
}

TEST(Mp4StreamDecoder, SeeksOverHttpOnlyWhereTheServerHonoursRanges) {
  LocalHttpServer server(aacFixturePath("tones.m4a"));
  for (const auto &[path, seekable] :
       {std::pair{"/ranged", true}, std::pair{"/whole", false}}) {
    SCOPED_TRACE(path);
    Mp4StreamDecoder decoder(1, BUFFER_SIZE);
    decoder.connectTo(
        std::make_shared<AudioGraphHttpStream>(1, server.url(path), 65536));
    waitStreaming(decoder);
    EXPECT_EQ(decoder.seekTo(30000), seekable ? 30000 : size_t(-1));
    EXPECT_EQ(drain(decoder).size(),
              (TONES_FRAMES - (seekable ? 30000 : 0)) * 4);
  }
}

TEST(Mp4StreamDecoder, RefusesWhatItCannotPlay) {
  const auto whole = aacFixture("tones.m4a");
  const Bytes half(whole.begin(), whole.begin() + whole.size() / 2);
  const std::tuple<Bytes, bool, const char *> cases[] = {
      {aacFixture("alac.m4a"), true, "Unsupported or invalid MP4 audio"},
      // Of known length, the audio box overruns the file; of unknown length,
      // the audio runs out.
      {half, true, "Unsupported or invalid MP4 audio"},
      {half, false, "Truncated MP4 file"},
      {Bytes(64, 0x41), true, "Unsupported or invalid MP4 audio"},
      {Bytes(), true, "The MP4 file has no movie index"},
  };
  for (const auto &[bytes, knownLength, message] : cases) {
    SCOPED_TRACE(message);
    Mp4StreamDecoder decoder(1, BUFFER_SIZE);
    decoder.connectTo(std::make_shared<BytesInput>(bytes, true, knownLength));
    drain(decoder);
    const auto state = decoder.getState();
    ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
    EXPECT_EQ(state.error->message, message);
  }
}
