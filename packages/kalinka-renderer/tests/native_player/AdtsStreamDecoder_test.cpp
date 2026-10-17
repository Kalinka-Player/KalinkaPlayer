#include "AacTestData.h"
#include "AdtsStreamDecoder.h"
#include "AudioGraphHttpStream.h"
#include "ContainerTestSupport.h"
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
/// ADTS has no length of its own, so the encoder's padding plays out: 54
/// units of 1024 frames after the first, which only primes the decoder.
constexpr size_t ADTS_FRAMES = 54 * 1024;

Bytes decode(std::shared_ptr<AudioGraphOutputNode> input,
             size_t startOffsetMs = 0) {
  AdtsStreamDecoder decoder(1, BUFFER_SIZE, startOffsetMs);
  decoder.connectTo(std::move(input));
  auto pcm = drain(decoder);
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
  return pcm;
}

void waitStreaming(AudioGraphOutputNode &node) {
  const auto deadline = std::chrono::steady_clock::now() + 3s;
  while (node.getState().state == AudioGraphNodeState::PREPARING &&
         std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  ASSERT_EQ(node.getState().state, AudioGraphNodeState::STREAMING);
}

/// An ID3v2.4 tag of `size` bytes of payload.
Bytes id3(size_t size) {
  Bytes tag{'I', 'D', '3', 4, 0, 0};
  for (int shift = 21; shift >= 0; shift -= 7)
    tag.push_back((size >> shift) & 0x7f);
  tag.resize(tag.size() + size, 'x');
  return tag;
}
} // namespace

TEST(AdtsStreamDecoder, PlaysWhatTheSameUnitsPlayInMp4) {
  Mp4StreamDecoder mp4(1, BUFFER_SIZE);
  mp4.connectTo(
      std::make_shared<FileInputNode>(1, aacFixturePath("tones.m4a")));
  const auto reference = drain(mp4);
  auto pcm =
      decode(std::make_shared<FileInputNode>(1, aacFixturePath("tones.aac")));
  ASSERT_EQ(pcm.size(), ADTS_FRAMES * 4);
  pcm.resize(reference.size());
  EXPECT_EQ(pcm, reference);
}

TEST(AdtsStreamDecoder, EstimatesTheLengthOfAFileOfKnownSize) {
  AdtsStreamDecoder decoder(1, BUFFER_SIZE);
  decoder.connectTo(
      std::make_shared<FileInputNode>(1, aacFixturePath("tones.aac")));
  waitStreaming(decoder);
  const auto length = decoder.getState().streamInfo->streamSize;
  EXPECT_NEAR(double(length), double(ADTS_FRAMES), 2048);
}

TEST(AdtsStreamDecoder, SkipsTagsAndJunkBeforeTheFirstFrame) {
  const auto plain =
      decode(std::make_shared<BytesInput>(aacFixture("tones.aac")));
  auto prefixed = id3(300);
  const Bytes junk{0xff, 0xf1, 0x00, 0x12, 0x34, 0xff, 0xff, 0x00, 0x01};
  prefixed.insert(prefixed.end(), junk.begin(), junk.end());
  const auto tones = aacFixture("tones.aac");
  prefixed.insert(prefixed.end(), tones.begin(), tones.end());
  EXPECT_EQ(decode(std::make_shared<BytesInput>(prefixed)), plain);
}

TEST(AdtsStreamDecoder, SeeksNearItsTarget) {
  for (size_t target : {size_t(1000), size_t(30000)}) {
    SCOPED_TRACE(target);
    AdtsStreamDecoder decoder(1, BUFFER_SIZE);
    decoder.connectTo(
        std::make_shared<FileInputNode>(1, aacFixturePath("tones.aac")));
    waitStreaming(decoder);
    ASSERT_EQ(decoder.seekTo(target), target);
    const auto pcm = drain(decoder);
    EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
    EXPECT_NEAR(double(pcm.size() / 4), double(ADTS_FRAMES - target), 2048);
  }
}

TEST(AdtsStreamDecoder, StartsNearAnOffset) {
  const auto pcm = decode(
      std::make_shared<FileInputNode>(1, aacFixturePath("tones.aac")), 500);
  EXPECT_NEAR(double(pcm.size() / 4), double(ADTS_FRAMES - 22050), 2048);
}

TEST(AdtsStreamDecoder, PlaysAStreamOfUnknownLengthWithoutSeeking) {
  AdtsStreamDecoder decoder(1, BUFFER_SIZE);
  decoder.connectTo(
      std::make_shared<BytesInput>(aacFixture("tones.aac"), false, false));
  waitStreaming(decoder);
  EXPECT_EQ(decoder.getState().streamInfo->streamSize, 0u);
  EXPECT_EQ(decoder.seekTo(1000), size_t(-1));
  EXPECT_EQ(drain(decoder).size(), ADTS_FRAMES * 4);
}

TEST(AdtsStreamDecoder, SeeksOverHttpOnlyWhereTheServerHonoursRanges) {
  LocalHttpServer server(aacFixturePath("tones.aac"));
  for (const auto &[path, seekable] :
       {std::pair{"/ranged", true}, std::pair{"/whole", false}}) {
    SCOPED_TRACE(path);
    AdtsStreamDecoder decoder(1, BUFFER_SIZE);
    decoder.connectTo(
        std::make_shared<AudioGraphHttpStream>(1, server.url(path), 65536));
    waitStreaming(decoder);
    EXPECT_EQ(decoder.seekTo(30000), seekable ? 30000 : size_t(-1));
    const auto frames = drain(decoder).size() / 4;
    if (seekable)
      EXPECT_NEAR(double(frames), double(ADTS_FRAMES - 30000), 2048);
    else
      EXPECT_EQ(frames, ADTS_FRAMES);
  }
}

TEST(AdtsStreamDecoder, AStreamCutMidFrameJustEnds) {
  auto tones = aacFixture("tones.aac");
  tones.resize(tones.size() - 100);
  EXPECT_EQ(decode(std::make_shared<BytesInput>(tones)).size(),
            (ADTS_FRAMES - 1024) * 4);
}

TEST(AdtsStreamDecoder, RefusesWhatIsNotAdts) {
  for (const auto &bytes : {Bytes(), Bytes(5000, 0x41), id3(20)}) {
    AdtsStreamDecoder decoder(1, BUFFER_SIZE);
    decoder.connectTo(std::make_shared<BytesInput>(bytes));
    EXPECT_TRUE(drain(decoder).empty());
    ASSERT_EQ(decoder.getState().state, AudioGraphNodeState::ERROR);
    EXPECT_EQ(decoder.getState().error->message, "No ADTS audio found");
  }
}
