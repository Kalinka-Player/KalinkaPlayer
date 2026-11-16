#include <gtest/gtest.h>

#include "AudioStreamSwitcher.h"
#include "Config.h"
#include "DelayLine.h"
#include "DspStage.h"
#include "StateMonitor.h"
#include "StreamState.h"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <future>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {
StreamState
waitForStatus(AudioGraphNode &node, AudioGraphNodeState status,
              std::optional<std::chrono::milliseconds> timeout = std::nullopt) {
  StateChangeWaitLock lock(std::stop_token(), node, status, timeout);

  return lock.state();
}

// A decoder that has decoded its whole file, seeked: every state it reported
// from just before the seek until it streamed again.
std::vector<AudioGraphNodeState>
seekOnceDecodedToTheEnd(AudioGraphOutputNode &decoder, size_t frame) {
  const auto streaming = waitForStatus(decoder, AudioGraphNodeState::STREAMING,
                                       std::chrono::seconds(5));
  EXPECT_TRUE(streaming.streamInfo.has_value());
  if (!streaming.streamInfo) {
    return {};
  }
  const auto &format = streaming.streamInfo->format;
  const size_t whole = streaming.streamInfo->streamSize * format.channels *
                       sampleSize(format.sampleFormat);
  EXPECT_GE(decoder.waitForDataFor(std::stop_token(), std::chrono::seconds(10),
                                   whole),
            whole)
      << "never decoded to the end";
  // The end is marked just after the last frame is written.
  std::this_thread::sleep_for(std::chrono::milliseconds(100));

  std::mutex mutex;
  std::vector<AudioGraphNodeState> seen;
  const int callback = decoder.onStateChange(
      [&mutex, &seen](AudioGraphNode *, StreamState state) {
        std::lock_guard lock(mutex);
        seen.push_back(state.state);
        return true;
      });
  EXPECT_EQ(decoder.seekTo(frame), frame);
  waitForStatus(decoder, AudioGraphNodeState::STREAMING,
                std::chrono::seconds(5));
  decoder.removeStateChangeCallback(callback);
  std::lock_guard lock(mutex);
  return seen;
}

// Fixtures live beside the tests, and are found from there rather than from
// whichever directory ctest happens to run in.
std::string testFile(const std::string &name) {
  return std::string(KALINKA_TEST_FILES_DIR) + "/" + name;
}

// Discards everything written to it, so a run is silent and needs no sound
// card. Set KALINKA_TEST_ALSA_DEVICE to play the tests somewhere real; the
// fixtures set the volume to zero, so that is silent as well. Software volume
// first raises the device's mixer to 100 %; KALINKA_LEAVE_MIXER=1 stops that.
std::string testDevice() {
  const char *configured = std::getenv("KALINKA_TEST_ALSA_DEVICE");
  return configured != nullptr ? configured : "null";
}

// Set KALINKA_TEST_DSP_DELAY_MS to send playback through a DSP stage that
// holds the audio back that long; 0 is the stage with nothing in it.
std::optional<int> testDspDelayMs() {
  const char *configured = std::getenv("KALINKA_TEST_DSP_DELAY_MS");
  return configured != nullptr ? std::optional<int>(std::atoi(configured))
                               : std::nullopt;
}

Config withTestDsp(Config config) {
  if (const auto delay = testDspDelayMs()) {
    config["dsp.delay_ms"] = std::to_string(*delay);
  }
  return config;
}

// The node a sink reads: @p source, behind the DSP stage when one is asked for.
std::shared_ptr<AudioGraphOutputNode>
throughTestDsp(std::shared_ptr<AudioGraphOutputNode> source) {
  const auto delay = testDspDelayMs();
  if (!delay) {
    return source;
  }
  auto stage = std::make_shared<DspStage>(
      *delay > 0 ? std::make_unique<DelayLine>(std::chrono::milliseconds(*delay))
                 : nullptr);
  stage->connectTo(std::move(source));
  return stage;
}

// Removes @p node from @p switcher as AudioPlayer does, dropping whatever the
// stage in front of the sink still holds of it.
void removeStream(AudioStreamSwitcher &switcher,
                  const std::shared_ptr<AudioGraphOutputNode> &node,
                  const std::shared_ptr<AudioGraphOutputNode> &output) {
  switcher.disconnect(node);
  if (auto stage = std::dynamic_pointer_cast<DspStage>(output);
      stage && node->streamId()) {
    stage->discard(*node->streamId());
  }
}

// Whether call() returns within the timeout. On a timeout the call is left
// running detached, so a hang fails the test rather than the whole suite.
template <typename Call>
bool returnsWithin(Call call, std::chrono::milliseconds timeout) {
  auto done = std::make_shared<std::promise<void>>();
  auto future = done->get_future();
  std::thread([call, done]() mutable {
    call();
    done->set_value();
  }).detach();
  return future.wait_for(timeout) == std::future_status::ready;
}

std::vector<StreamState> drainStates(StateMonitor &monitor) {
  std::vector<StreamState> seen;
  while (monitor.hasData()) {
    seen.push_back(monitor.waitState());
  }
  return seen;
}

// Each stream took over and played, in this order. What falls between them is
// not part of the claim: refilling an emptied buffer is a preparing of its
// own, and the sink prepares as often as it needs to.
void expectPlayedInTurn(const std::vector<StreamState> &seen,
                        const std::vector<StreamId> &streams) {
  auto at = seen.begin();
  for (StreamId stream : streams) {
    auto tookOver = std::find_if(at, seen.end(), [stream](const StreamState &s) {
      return s.state == AudioGraphNodeState::SOURCE_CHANGED &&
             s.streamId == stream;
    });
    ASSERT_NE(tookOver, seen.end()) << "stream " << stream << " never took over";
    at = std::find_if(tookOver, seen.end(), [stream](const StreamState &s) {
      return s.state == AudioGraphNodeState::STREAMING && s.streamId == stream;
    });
    ASSERT_NE(at, seen.end()) << "stream " << stream << " never played";
  }
}

bool reported(const std::vector<StreamState> &seen, AudioGraphNodeState state) {
  return std::any_of(seen.begin(), seen.end(),
                     [state](const StreamState &s) { return s.state == state; });
}

// The null device takes frames as fast as they are written, so a test whose
// subject is what happens while a track plays has nothing to watch.
#define SKIP_UNLESS_PLAYED_IN_REAL_TIME()                                      \
  if (testDevice() == "null") {                                                \
    GTEST_SKIP() << "needs a device that plays in real time: set "             \
                    "KALINKA_TEST_ALSA_DEVICE";                                \
  }
} // namespace
