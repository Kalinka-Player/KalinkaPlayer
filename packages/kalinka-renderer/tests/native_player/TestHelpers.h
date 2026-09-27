#include <gtest/gtest.h>

#include "StateMonitor.h"
#include "StreamState.h"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <future>
#include <memory>
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

// Fixtures live beside the tests, and are found from there rather than from
// whichever directory ctest happens to run in.
std::string testFile(const std::string &name) {
  return std::string(KALINKA_TEST_FILES_DIR) + "/" + name;
}

// Discards everything written to it, so a run is silent and needs no sound
// card. Set KALINKA_TEST_ALSA_DEVICE to play the tests somewhere real; the
// fixtures set the volume to zero, so that is silent as well.
std::string testDevice() {
  const char *configured = std::getenv("KALINKA_TEST_ALSA_DEVICE");
  return configured != nullptr ? configured : "null";
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
