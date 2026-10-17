#include "DashStreamDecoder.h"
#include "DashTestData.h"
#include "LocalHttpServer.h"

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

Bytes expected(int bits, size_t first = 0) {
  const size_t rate = bits == 16 ? 44100 : 96000;
  Bytes bytes;
  for (size_t i = first; i < rate * 5 / 4; ++i)
    for (int channel = 0; channel < 2; ++channel) {
      int32_t value = (int(i % 503) - 251) * 57 * (channel ? -1 : 1);
      if (bits == 24)
        value = (value * 256 + int(i % 127) - 63) & 0xffffff;
      for (int b = 0; b < (bits == 16 ? 2 : 4); ++b)
        bytes.push_back(uint32_t(value) >> (8 * b));
    }
  return bytes;
}

Bytes drain(DashStreamDecoder &node) {
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
  ADD_FAILURE() << "DASH decoder did not finish";
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
    EXPECT_EQ(actual, expected(bits));
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
    EXPECT_EQ(drain(node), expected(bits, rate / 2));
    for (size_t target : {rate / 4 + 13, rate + 7, size_t(0)}) {
      ASSERT_EQ(node.seekTo(target), target);
      EXPECT_EQ(drain(node), expected(bits, target));
    }
    EXPECT_EQ(node.seekTo(rate * 3), rate * 5 / 4);
    EXPECT_TRUE(drain(node).empty());
  }
}

TEST(DashDecoder, SeekInterruptsAFullBuffer) {
  LocalHttpServer server(dashFixtures(24));
  DashStreamDecoder node(3, server.url("/manifest.mpd"), 128);
  waitReady(node);
  ASSERT_EQ(node.seekTo(96000), 96000u);
  EXPECT_EQ(drain(node), expected(24, 96000));
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
  EXPECT_EQ(drain(node), expected(24));
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
