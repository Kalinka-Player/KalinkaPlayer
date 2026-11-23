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
  return inputNodes.empty() ? std::nullopt : inputNodes.front().id;
}

void AudioStreamSwitcher::connectTo(
    std::shared_ptr<AudioGraphOutputNode> inputNode) {
  if (inputNode == nullptr) {
    throw std::runtime_error("Input node cannot be nullptr");
  }

  const auto id = inputNode->streamId();
  std::lock_guard lock(mutex);
  inputNodes.push_back({inputNode, id});

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
    currentInfo.reset();
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
    inputNodes.remove_if([&inputNode](const Source &queued) {
      return queued.node == inputNode;
    });
    int callbackId = -1;
    finished.remove_if([&inputNode, &callbackId](const Source &played) {
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
                      callbackUnhooked ? -1 : stateCallbackId, currentInfo});
  currentInputNode = nullptr;
  currentId.reset();
  currentBeganAt.reset();
  currentInfo.reset();
  stateCallbackId = -1;
}

void AudioStreamSwitcher::switchToNextSource() {
  std::unique_lock lock(mutex);
  if (currentInputNode != nullptr ||
      getState().state != AudioGraphNodeState::SOURCE_CHANGED) {
    spdlog::warn("Not in SOURCE_CHANGED state or currentInputNode is not null");
    return;
  }
  const auto next = inputNodes.front().node;
  currentInputNode = next;
  currentId = inputNodes.front().id;
  currentInfo = inputNodes.front().info;
  currentBeganAt.reset();
  inputNodes.pop_front();
  setStreamId(currentId);
  lock.unlock();
  follow(next);
}

void AudioStreamSwitcher::follow(
    const std::shared_ptr<AudioGraphOutputNode> &inputNode) {
  auto id = inputNode->onStateChange([this](AudioGraphNode *node,
                                            StreamState state) -> bool {
    std::lock_guard lock(mutex);
    if (node != currentInputNode.get()) {
      return false;
    }
    if (state.streamInfo)
      currentInfo = state.streamInfo;
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
    for (Source &played : finished) {
      if (played.node == currentInput && !played.beganAt) {
        played.beganAt = begin;
      }
    }
  }
  return bytes;
}

size_t AudioStreamSwitcher::waitForData(std::stop_token stopToken,
                                        size_t size) {
  const auto [currentNode, replaced] = currentUntilReplaced();
  if (currentNode == nullptr) {
    return 0;
  }

  auto combinedToken = combineStopTokens(stopToken, replaced);
  return currentNode->waitForData(combinedToken.get_token(), size);
}

size_t AudioStreamSwitcher::waitForDataFor(std::stop_token stopToken,
                                           std::chrono::milliseconds timeout,
                                           size_t size) {
  const auto [currentNode, replaced] = currentUntilReplaced();
  if (currentNode == nullptr) {
    return 0;
  }

  auto combinedToken = combineStopTokens(stopToken, replaced);
  return currentNode->waitForDataFor(combinedToken.get_token(), timeout, size);
}

void AudioStreamSwitcher::acceptSourceChange() { switchToNextSource(); }

std::shared_ptr<AudioGraphOutputNode> AudioStreamSwitcher::current() const {
  std::lock_guard lock(mutex);
  return currentInputNode;
}

std::pair<std::shared_ptr<AudioGraphOutputNode>, std::stop_token>
AudioStreamSwitcher::currentUntilReplaced() const {
  std::lock_guard lock(mutex);
  return {currentInputNode, stopSource.get_token()};
}

std::optional<uint64_t> AudioStreamSwitcher::liveSourceFrame() const {
  const auto source = current();
  return source ? source->liveSourceFrame() : std::nullopt;
}

std::optional<long> AudioStreamSwitcher::streamReadPosition() const {
  // Passed through, never cached: the source owns its own timeline.
  const auto source = current();
  return source ? source->streamReadPosition() : std::nullopt;
}

std::list<AudioStreamSwitcher::Source>::iterator
AudioStreamSwitcher::latestFinished(StreamId stream) {
  // The latest of a stream queued twice is the one still heard.
  const auto latest = std::find_if(
      finished.rbegin(), finished.rend(),
      [stream](const Source &source) { return source.id == stream; });
  return latest == finished.rend() ? finished.end() : std::prev(latest.base());
}

std::optional<StreamInfo>
AudioStreamSwitcher::seekStreamInfo(std::optional<StreamId> stream,
                                    bool waitForHeader) {
  std::unique_lock lock(mutex);
  if (stream && currentId != stream) {
    const auto found = latestFinished(*stream);
    return found == finished.end() ? std::nullopt : found->info;
  }
  if (currentInfo || !waitForHeader || !currentInputNode)
    return currentInfo;
  const auto id = currentId;
  lock.unlock();
  return waitForSeekHeader(id);
}

size_t AudioStreamSwitcher::seekStream(StreamId stream, size_t position) {
  std::unique_lock lock(mutex);
  if (currentInputNode != nullptr && currentId == stream) {
    auto current = currentInputNode;
    lock.unlock();
    return current->seekTo(position);
  }
  const auto found = latestFinished(stream);
  if (found == finished.end())
    return size_t(-1);
  if (currentInputNode != nullptr) {
    retireCurrent(false);
  }
  const Source again = *found;
  std::vector<Source> requeued(std::next(found), finished.end());
  finished.erase(found, finished.end());
  for (auto it = requeued.rbegin(); it != requeued.rend(); ++it) {
    inputNodes.push_front({it->node, it->id, std::nullopt, -1, it->info});
  }
  currentInputNode = again.node;
  currentId = again.id;
  currentBeganAt = again.beganAt;
  currentInfo = again.info;
  stateCallbackId = -1;
  stopSource.request_stop();
  stopSource = std::stop_source();
  setStreamId(stream);
  lock.unlock();

  for (const Source &played : requeued) {
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
  const auto currentNode = current();
  if (currentNode == nullptr) {
    return -1;
  }
  return currentNode->seekTo(absolutePosition);
}
