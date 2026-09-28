#ifndef STATE_MONITOR_H
#define STATE_MONITOR_H

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <queue>

#include "AudioGraphNode.h"
#include "StreamState.h"

class StateMonitor {
public:
  friend class AudioGraphNode;

  StateMonitor(AudioGraphNode *ptr);
  StateMonitor(const StateMonitor &) = delete;
  StateMonitor &operator=(const StateMonitor &) = delete;
  ~StateMonitor();

  StreamState waitState();

  bool hasData();
  bool isRunning() const;
  void stop();

protected:
  std::mutex mutex;
  std::condition_variable_any cv;
  std::queue<StreamState> queue_;
  AudioGraphNode *ptr;
  int subscriptionId;

  std::atomic<bool> stopped = false;
};

class StateChangeWaitLock {
public:
  StateChangeWaitLock(
      std::stop_token token, AudioGraphNode &node,
      AudioGraphNodeState nextState,
      std::optional<std::chrono::milliseconds> timeout = std::nullopt);

  StateChangeWaitLock(
      std::stop_token token, AudioGraphNode &node, unsigned long long timestamp,
      std::optional<std::chrono::milliseconds> timeout = std::nullopt);

  const StreamState &state() { return lastState; }

private:
  StreamState lastState;
  std::condition_variable_any cv;
  std::mutex mutex;
};

#endif