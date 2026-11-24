#include "AudioGraphNode.h"
#include "StateMonitor.h"

StreamState AudioGraphNode::getState() {
  std::lock_guard lock(mutex);
  return state;
}

StreamState AudioGraphNode::stateForSeek() {
  std::lock_guard lock(mutex);
  auto snapshot = state;
  if (snapshot.state == AudioGraphNodeState::FINISHED && !snapshot.streamInfo)
    snapshot.streamInfo = lastStreamInfo;
  return snapshot;
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
  if (boundStreamId != id)
    lastStreamInfo.reset();
  boundStreamId = id;
}

std::optional<StreamId> AudioGraphNode::streamId() {
  std::lock_guard lock(mutex);
  return boundStreamId;
}

void AudioGraphNode::setState(const StreamState &newState) {
  std::lock_guard lock(mutex);
  apply(newState);
}

void AudioGraphNode::apply(const StreamState &newState) {
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
  if (stamped.streamId != state.streamId ||
      stamped.state == AudioGraphNodeState::SOURCE_CHANGED ||
      stamped.state == AudioGraphNodeState::STOPPED)
    lastStreamInfo.reset();
  if (stamped.streamInfo)
    lastStreamInfo = stamped.streamInfo;
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

std::optional<StreamInfo>
AudioGraphOutputNode::seekStreamInfo(std::optional<StreamId>,
                                     bool waitForHeader) {
  // Like seekStream(), the single-source default always targets this node.
  const auto now = stateForSeek();
  if (waitForHeader && !now.streamInfo &&
      now.state == AudioGraphNodeState::PREPARING)
    return waitForSeekHeader(now.streamId);
  return now.streamInfo;
}

std::optional<StreamInfo>
AudioGraphOutputNode::waitForSeekHeader(std::optional<StreamId> id) {
  // Shutdown joins the pulling thread before it disconnects the source.
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(1);
  StateMonitor changes(this);
  while (const auto state = changes.waitStateUntil(deadline)) {
    if (state->streamId != id)
      break;
    if (state->streamInfo || state->state != AudioGraphNodeState::PREPARING)
      return state->streamInfo;
  }
  return std::nullopt;
}
