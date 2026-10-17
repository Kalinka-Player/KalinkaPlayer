#include "AacTestData.h"
#include "DashStreamDecoder.h"
#include "DashTestData.h"
#include "FileInputNode.h"
#include "LocalHttpServer.h"
#include "Mp4StreamDecoder.h"

#include <gtest/gtest.h>

#include <array>
#include <filesystem>
#include <fstream>
#include <future>
#include <iterator>
#include <map>
#include <vector>

namespace {
using namespace std::chrono_literals;
using Bytes = std::vector<uint8_t>;

Bytes drain(AudioGraphOutputNode &node) {
  Bytes result;
  std::array<uint8_t, 2401> block;
  const auto deadline = std::chrono::steady_clock::now() + 5s;
  while (std::chrono::steady_clock::now() < deadline) {
    if (node.waitForDataFor({}, 50ms, 1)) {
      const auto count = node.read(block.data(), block.size());
      result.insert(result.end(), block.begin(), block.begin() + count);
    } else if (node.getState().state == AudioGraphNodeState::FINISHED ||
               node.getState().state == AudioGraphNodeState::ERROR)
      return result;
  }
  ADD_FAILURE() << "decoder did not finish";
  return result;
}

void waitReady(DashStreamDecoder &node) {
  const auto deadline = std::chrono::steady_clock::now() + 3s;
  while (node.getState().state == AudioGraphNodeState::PREPARING &&
         std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(5ms);
  ASSERT_EQ(node.getState().state, AudioGraphNodeState::STREAMING);
}
} // namespace

TEST(DashDecoder, PreservesEverySampleAcrossSegments) {
  for (int bits : {16, 24}) {
    SCOPED_TRACE(bits);
    LocalHttpServer server(dashFixtures(bits));
    DashStreamDecoder node(7, server.url("/manifest.mpd"), 8192);
    const auto actual = drain(node);
    EXPECT_EQ(node.getState().state, AudioGraphNodeState::FINISHED)
        << node.getState().toString();
    EXPECT_EQ(actual, rampPcm(bits));
    const auto state = node.getState();
    ASSERT_TRUE(state.streamInfo);
    EXPECT_EQ(state.streamInfo->format.bitsPerSample, bits);
    EXPECT_EQ(state.streamInfo->format.sampleRate,
              bits == 16 ? 44100u : 96000u);
    EXPECT_EQ(state.streamId, 7);
    EXPECT_EQ(node.streamReadPosition(), bits == 16 ? 55125 : 120000);
    EXPECT_GT(server.requestsTo("/chunk-stream0-00002.m4s"), 0u);
  }
}

TEST(DashDecoder, StartOffsetAndSeeksAreSampleAccurate) {
  for (int bits : {16, 24}) {
    SCOPED_TRACE(bits);
    const size_t rate = bits == 16 ? 44100 : 96000;
    LocalHttpServer server(dashFixtures(bits));
    DashStreamDecoder node(3, server.url("/manifest.mpd"), 8192, 500);
    EXPECT_EQ(drain(node), rampPcm(bits, rate / 2));
    for (size_t target : {rate / 4 + 13, rate + 7, size_t(0)}) {
      ASSERT_EQ(node.seekTo(target), target);
      EXPECT_EQ(drain(node), rampPcm(bits, target));
    }
    EXPECT_EQ(node.seekTo(rate * 3), rate * 5 / 4);
    EXPECT_TRUE(drain(node).empty());
  }
}

TEST(DashDecoder, PlaysAacAsTheSameUnitsPlayInMp4) {
  const auto mp4 = [](size_t target) {
    Mp4StreamDecoder decoder(1, 16384);
    decoder.connectTo(
        std::make_shared<FileInputNode>(1, aacFixturePath("tones.m4a")));
    EXPECT_EQ(decoder.seekTo(target), target);
    return drain(decoder);
  };
  LocalHttpServer server(dashFixtures("aac"));
  DashStreamDecoder node(3, server.url("/manifest.mpd"), 8192);
  waitReady(node);
  EXPECT_EQ(node.getState().streamInfo->streamSize, TONES_FRAMES);
  EXPECT_EQ(drain(node), mp4(0));
  // 10240 and 11264 put the unit that primes the decoder in the segment
  // before the one that plays. DASH restarts at a segment's first unit and
  // MP4 at the unit before its target, so their noise substitution, which
  // is random, may differ slightly after a seek.
  for (size_t target : {size_t(10240), size_t(10241), size_t(11264),
                        size_t(30000), size_t(0)}) {
    SCOPED_TRACE(target);
    ASSERT_EQ(node.seekTo(target), target);
    EXPECT_LE(maxDifference(drain(node), mp4(target)), 64);
  }
  EXPECT_EQ(node.seekTo(TONES_FRAMES + 5), TONES_FRAMES);
  EXPECT_TRUE(drain(node).empty());
}

TEST(DashDecoder, PlaysWhatTheInitializationsEditSelects) {
  auto bodies = dashFixtures("aac");
  auto &init = bodies.at("/init-stream0.m4s");
  // Media time 2112, Apple's priming, where the fixture's encoder used 1024.
  const auto elst = init.find("elst");
  ASSERT_NE(elst, std::string::npos);
  init[elst + 18] = 0x08;
  init[elst + 19] = 0x40;
  LocalHttpServer server(std::move(bodies));
  DashStreamDecoder node(3, server.url("/manifest.mpd"), 8192);
  waitReady(node);
  EXPECT_EQ(node.getState().streamInfo->streamSize, TONES_FRAMES - 1088);
  Mp4StreamDecoder mp4(1, 16384);
  mp4.connectTo(
      std::make_shared<FileInputNode>(1, aacFixturePath("tones.m4a")));
  const auto reference = drain(mp4);
  EXPECT_EQ(drain(node), Bytes(reference.begin() + 1088 * 4, reference.end()));
}

TEST(DashDecoder, SeekInterruptsAFullBuffer) {
  LocalHttpServer server(dashFixtures(24));
  DashStreamDecoder node(3, server.url("/manifest.mpd"), 128);
  waitReady(node);
  ASSERT_EQ(node.seekTo(96000), 96000u);
  EXPECT_EQ(drain(node), rampPcm(24, 96000));
}

TEST(DashDecoder, FetchesTheNextSegmentWhileTheCurrentOnePlays) {
  LocalHttpServer server(dashFixtures(24));
  DashStreamDecoder node(3, server.url("/manifest.mpd"), 128);
  waitReady(node);
  const auto deadline = std::chrono::steady_clock::now() + 2s;
  while (!server.requestsTo("/chunk-stream0-00002.m4s") &&
         std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(5ms);
  EXPECT_GT(server.requestsTo("/chunk-stream0-00002.m4s"), 0u);
  EXPECT_EQ(server.requestsTo("/chunk-stream0-00003.m4s"), 0u);
  EXPECT_EQ(drain(node), rampPcm(24));
}

TEST(DashDecoder, MissingSegmentIsAnError) {
  auto bodies = dashFixtures(16);
  bodies.erase("/chunk-stream0-00002.m4s");
  LocalHttpServer server(std::move(bodies));
  DashStreamDecoder node(5, server.url("/manifest.mpd"), 8192);
  drain(node);
  EXPECT_EQ(node.getState().state, AudioGraphNodeState::ERROR);
  EXPECT_EQ(node.seekTo(0), size_t(-1));
}

TEST(DashDecoder, InvalidManifestDoesNotExposeItsUrl) {
  LocalHttpServer server(std::map<std::string, std::string, std::less<>>{
      {"/manifest.mpd?token=private-token", "not an MPD"}});
  DashStreamDecoder node(5, server.url("/manifest.mpd?token=private-token"),
                         8192);
  drain(node);
  const auto state = node.getState();
  EXPECT_EQ(state.state, AudioGraphNodeState::ERROR);
  EXPECT_EQ(state.toString().find("private-token"), std::string::npos);
}

TEST(DashDecoder, RejectsLocalFiles) {
  DashStreamDecoder node(5, "file:///etc/passwd", 8192);
  drain(node);
  EXPECT_EQ(node.getState().state, AudioGraphNodeState::ERROR);
}

TEST(DashDecoder, CancellationInterruptsNetworkWait) {
  LocalHttpServer server(std::map<std::string, std::string, std::less<>>{});
  auto node =
      std::make_unique<DashStreamDecoder>(5, server.url("/silent-once"), 8192);
  const auto deadline = std::chrono::steady_clock::now() + 2s;
  while (!server.requestsTo("/silent-once") &&
         std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(5ms);
  ASSERT_GT(server.requestsTo("/silent-once"), 0u);
  const auto start = std::chrono::steady_clock::now();
  node.reset();
  EXPECT_LT(std::chrono::steady_clock::now() - start, 1500ms);
}

TEST(DashDecoder, NetworkWaitHasATimeLimit) {
  LocalHttpServer server(std::map<std::string, std::string, std::less<>>{});
  DashStreamDecoder node(5, server.url("/silent-once"), 8192, 0, 200ms);
  drain(node);
  EXPECT_EQ(node.getState().state, AudioGraphNodeState::ERROR);
}
