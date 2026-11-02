#include "AudioStreamSwitcher.h"
#include "Log.h"
#include "Utils.h"

namespace {
StreamState about(AudioGraphNodeState state, std::optional<StreamId> id) {
  StreamState stamped{state};
  stamped.streamId = id;
  return stamped;
}
} // namespace

AudioStreamSwitcher::AudioStreamSwitcher() {}

std::optional<StreamId> AudioStreamSwitcher::nextStreamId() {
  return inputNodes.empty() ? std::nullopt : inputNodes.front()->streamId();
}

void AudioStreamSwitcher::connectTo(
    std::shared_ptr<AudioGraphOutputNode> inputNode) {
  if (inputNode == nullptr) {
    throw std::runtime_error("Input node cannot be nullptr");
  }

  std::lock_guard lock(mutex);
  inputNodes.push_back(inputNode);

  if (currentInputNode == nullptr ||
      getState().state == AudioGraphNodeState::FINISHED) {
    if (currentInputNode != nullptr) {
      currentInputNode = nullptr;
    }
    stopSource.request_stop();
    stopSource = std::stop_source();
    setState(about(AudioGraphNodeState::SOURCE_CHANGED, nextStreamId()));
  }
}

void AudioStreamSwitcher::disconnect(
    std::shared_ptr<AudioGraphOutputNode> inputNode) {

  if (inputNode == nullptr) {
    throw std::runtime_error("Input node cannot be nullptr");
  }
  std::unique_lock lock(mutex);
  if (inputNode == currentInputNode) {
    const int callbackId = stateCallbackId;
    currentInputNode = nullptr;
    stopSource.request_stop();
    stopSource = std::stop_source();
    if (inputNodes.empty()) {
      spdlog::debug("Removed current node, no more input nodes available, "
                    "setting state to FINISHED");
    }
    // Set before clearing the id, so FINISHED names the stream that ended.
    setState(inputNodes.empty()
                 ? StreamState(AudioGraphNodeState::FINISHED)
                 : about(AudioGraphNodeState::SOURCE_CHANGED, nextStreamId()));
    setStreamId(std::nullopt);
    // Renderer delta: unhook outside the lock, because the callback takes it.
    lock.unlock();
    inputNode->removeStateChangeCallback(callbackId);
  } else {
    inputNodes.remove(inputNode);
    if (currentInputNode == nullptr && inputNodes.empty()) {
      spdlog::debug("Removed other node, no more input nodes available, "
                    "setting state to FINISHED");
      setState(StreamState(AudioGraphNodeState::FINISHED));
    }
  }
}

void AudioStreamSwitcher::switchToNextSource() {
  std::unique_lock lock(mutex);
  if (currentInputNode != nullptr ||
      getState().state != AudioGraphNodeState::SOURCE_CHANGED) {
    spdlog::warn("Not in SOURCE_CHANGED state or currentInputNode is not null");
    return;
  }
  auto inputNode = inputNodes.front();
  currentInputNode = inputNode;
  inputNodes.pop_front();
  setStreamId(inputNode->streamId());
  lock.unlock();

  auto id = inputNode->onStateChange([this](AudioGraphNode *node,
                                            StreamState state) -> bool {
    std::lock_guard lock(mutex);
    if (node != currentInputNode.get()) {
      return false;
    }
    if (state.state == AudioGraphNodeState::FINISHED && !inputNodes.empty()) {
      currentInputNode = nullptr;
      setState(about(AudioGraphNodeState::SOURCE_CHANGED, nextStreamId()));
      setStreamId(std::nullopt);
      return false;
    }

    // Restamp the state to avoid time jumping backwards
    state.timestamp = getTimestampNs();
    setState(state);

    return true;
  });
  lock.lock();
  if (currentInputNode == inputNode) {
    stateCallbackId = id;
  }
}

size_t AudioStreamSwitcher::read(void *data, size_t size) {
  std::unique_lock lock(mutex);
  auto currentInput = currentInputNode;
  lock.unlock();
  if (currentInput == nullptr) {
    return 0;
  }

  return currentInput->read(data, size);
}

size_t AudioStreamSwitcher::waitForData(std::stop_token stopToken,
                                        size_t size) {
  std::unique_lock lock(mutex);
  auto currentNode = currentInputNode;
  lock.unlock();
  if (currentNode == nullptr) {
    return 0;
  }

  auto combinedToken = combineStopTokens(stopToken, stopSource.get_token());
  return currentNode->waitForData(combinedToken.get_token(), size);
}

size_t AudioStreamSwitcher::waitForDataFor(std::stop_token stopToken,
                                           std::chrono::milliseconds timeout,
                                           size_t size) {
  std::unique_lock lock(mutex);
  auto currentNode = currentInputNode;
  lock.unlock();
  if (currentNode == nullptr) {
    return 0;
  }

  auto combinedToken = combineStopTokens(stopToken, stopSource.get_token());
  return currentNode->waitForDataFor(combinedToken.get_token(), timeout, size);
}

void AudioStreamSwitcher::acceptSourceChange() { switchToNextSource(); }

std::optional<uint64_t> AudioStreamSwitcher::liveSourceFrame() const {
  std::lock_guard lock(mutex);
  return currentInputNode ? currentInputNode->liveSourceFrame() : std::nullopt;
}

std::optional<long> AudioStreamSwitcher::streamReadPosition() const {
  // Passed through, never cached: the source owns its own timeline.
  std::lock_guard lock(mutex);
  return currentInputNode == nullptr ? std::nullopt
                                     : currentInputNode->streamReadPosition();
}

size_t AudioStreamSwitcher::seekTo(size_t absolutePosition) {
  std::unique_lock lock(mutex);
  auto currentNode = currentInputNode;
  lock.unlock();

  if (currentNode == nullptr) {
    return -1;
  }
  return currentNode->seekTo(absolutePosition);
}
