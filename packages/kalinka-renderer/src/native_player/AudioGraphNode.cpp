#include "AudioGraphNode.h"
#include "Log.h"

StreamState AudioGraphNode::getState() {
  std::lock_guard lock(mutex);
  return state;
}

int AudioGraphNode::onStateChange(
    std::function<bool(AudioGraphNode *, StreamState)> callback) {
  std::lock_guard lock(mutex);
  stateChangeCallbacks.insert({++callbackId, callback});
  callback(this, state);
  return callbackId;
}

void AudioGraphNode::removeStateChangeCallback(int id) {
  if (id < 0) {
    return;
  }

  std::lock_guard lock(mutex);
  stateChangeCallbacks.erase(id);
}

AudioGraphNode::~AudioGraphNode() {}

void AudioGraphNode::setStreamId(std::optional<StreamId> id) {
  std::lock_guard lock(mutex);
  boundStreamId = id;
}

std::optional<StreamId> AudioGraphNode::streamId() {
  std::lock_guard lock(mutex);
  return boundStreamId;
}

void AudioGraphNode::setState(const StreamState &newState) {
  std::lock_guard lock(mutex);
  StreamState stamped = newState;
  if (!stamped.streamId.has_value()) {
    stamped.streamId = boundStreamId;
  }
  // Live readiness releases Core's PCM gate, and frame reports bound backlog.
  // Both can change without a playback-state or source transition.
  if (state.state == stamped.state && state.streamId == stamped.streamId &&
      state.liveReady == stamped.liveReady &&
      state.liveSourceFrame == stamped.liveSourceFrame) {
    return;
  }
  state = stamped;
  for (auto it = stateChangeCallbacks.begin();
       it != stateChangeCallbacks.end();) {
    auto stateChangeCallback = it->second;
    if (!stateChangeCallback(this, state)) {
      it = stateChangeCallbacks.erase(it);
    } else {
      ++it;
    }
  }
}
