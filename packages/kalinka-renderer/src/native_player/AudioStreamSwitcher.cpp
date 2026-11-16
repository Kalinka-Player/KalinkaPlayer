#include "AudioStreamSwitcher.h"
#include "Log.h"
#include "Utils.h"

#include <algorithm>
#include <vector>

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
      retireCurrent(false);
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
    currentId.reset();
    currentBeganAt.reset();
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
    int callbackId = -1;
    finished.remove_if([&inputNode, &callbackId](const Played &played) {
      if (played.node != inputNode) {
        return false;
      }
      callbackId = played.callbackId;
      return true;
    });
    if (currentInputNode == nullptr && inputNodes.empty()) {
      spdlog::debug("Removed other node, no more input nodes available, "
                    "setting state to FINISHED");
      setState(StreamState(AudioGraphNodeState::FINISHED));
    }
    lock.unlock();
    inputNode->removeStateChangeCallback(callbackId);
  }
}

void AudioStreamSwitcher::retireCurrent(bool callbackUnhooked) {
  finished.push_back({currentInputNode, currentId, currentBeganAt,
                      callbackUnhooked ? -1 : stateCallbackId});
  currentInputNode = nullptr;
  currentId.reset();
  currentBeganAt.reset();
  stateCallbackId = -1;
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
  currentId = inputNode->streamId();
  currentBeganAt.reset();
  inputNodes.pop_front();
  setStreamId(currentId);
  lock.unlock();
  follow(inputNode);
}

void AudioStreamSwitcher::follow(
    const std::shared_ptr<AudioGraphOutputNode> &inputNode) {
  auto id = inputNode->onStateChange([this](AudioGraphNode *node,
                                            StreamState state) -> bool {
    std::lock_guard lock(mutex);
    if (node != currentInputNode.get()) {
      return false;
    }
    if (state.state == AudioGraphNodeState::FINISHED && !inputNodes.empty()) {
      retireCurrent(true);
      setState(about(AudioGraphNodeState::SOURCE_CHANGED, nextStreamId()));
      setStreamId(std::nullopt);
      return false;
    }

    // Restamp the state to avoid time jumping backwards
    state.timestamp = getTimestampNs();
    setState(state);

    return true;
  });
  std::unique_lock lock(mutex);
  if (currentInputNode == inputNode) {
    stateCallbackId = id;
    return;
  }
  lock.unlock();
  // Retired before the id was known, maybe by the very call that hooked it.
  inputNode->removeStateChangeCallback(id);
}

size_t AudioStreamSwitcher::read(void *data, size_t size) {
  std::unique_lock lock(mutex);
  auto currentInput = currentInputNode;
  const bool first = currentInput != nullptr && !currentBeganAt;
  lock.unlock();
  if (currentInput == nullptr) {
    return 0;
  }
  // Only a read that yields frames shows where the source settled to begin.
  const auto begin = first ? currentInput->streamReadPosition() : std::nullopt;
  const size_t bytes = currentInput->read(data, size);
  if (begin && bytes > 0) {
    lock.lock();
    if (currentInputNode == currentInput) {
      currentBeganAt = currentBeganAt.value_or(*begin);
    }
    for (Played &played : finished) {
      if (played.node == currentInput && !played.beganAt) {
        played.beganAt = begin;
      }
    }
  }
  return bytes;
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

size_t AudioStreamSwitcher::seekStream(StreamId stream, size_t position) {
  std::unique_lock lock(mutex);
  if (currentInputNode != nullptr && currentId == stream) {
    auto current = currentInputNode;
    lock.unlock();
    return current->seekTo(position);
  }
  // The latest of a stream queued twice is the one still heard.
  const auto latest =
      std::find_if(finished.rbegin(), finished.rend(),
                   [stream](const Played &p) { return p.id == stream; });
  if (latest == finished.rend()) {
    return size_t(-1);
  }
  const auto found = std::prev(latest.base());
  if (currentInputNode != nullptr) {
    retireCurrent(false);
  }
  const Played again = *found;
  std::vector<Played> requeued(std::next(found), finished.end());
  finished.erase(found, finished.end());
  for (auto it = requeued.rbegin(); it != requeued.rend(); ++it) {
    inputNodes.push_front(it->node);
  }
  currentInputNode = again.node;
  currentId = again.id;
  currentBeganAt = again.beganAt;
  stateCallbackId = -1;
  stopSource.request_stop();
  stopSource = std::stop_source();
  setStreamId(stream);
  lock.unlock();

  for (const Played &played : requeued) {
    played.node->removeStateChangeCallback(played.callbackId);
    if (played.beganAt) {
      played.node->seekTo(size_t(*played.beganAt));
    }
  }
  again.node->removeStateChangeCallback(again.callbackId);
  // Seeked before it is followed, or its FINISHED would retire it again.
  const size_t reached = again.node->seekTo(position);
  follow(again.node);
  return reached;
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
