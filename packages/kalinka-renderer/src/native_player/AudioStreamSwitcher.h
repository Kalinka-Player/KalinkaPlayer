#ifndef AUDIO_GRAPH_CONTROLLER_H
#define AUDIO_GRAPH_CONTROLLER_H

#include "AudioGraphNode.h"

#include <list>
#include <utility>

class AudioStreamSwitcher : public AudioGraphOutputNode,
                            public AudioGraphInputNode {
public:
  AudioStreamSwitcher();
  virtual ~AudioStreamSwitcher() = default;

  virtual void
  connectTo(std::shared_ptr<AudioGraphOutputNode> inputNode) override;

  virtual void
  disconnect(std::shared_ptr<AudioGraphOutputNode> inputNode) override;

  virtual size_t read(void *data, size_t size) override;

  virtual size_t waitForData(std::stop_token stopToken = std::stop_token(),
                             size_t size = 1) override;

  virtual size_t waitForDataFor(std::stop_token stopToken,
                                std::chrono::milliseconds timeout,
                                size_t size) override;
  virtual size_t seekTo(size_t absolutePosition) override;

  /**
   * @brief Seeks @p stream, which may have finished here while it is still
   * heard downstream.
   *
   * A finished stream becomes current again, and every stream that followed it
   * goes back to the front of the queue, wound back to where it began.
   * @return the frame reached, or size_t(-1) for a stream this switcher does
   * not hold.
   * @note A finished stream is held until it is disconnected.
   */
  size_t seekStream(StreamId stream, size_t position) override;
  std::optional<StreamInfo> seekStreamInfo(std::optional<StreamId> stream,
                                           bool waitForHeader = false) override;

  virtual std::optional<long> streamReadPosition() const override;
  std::optional<uint64_t> liveSourceFrame() const override;

  virtual void acceptSourceChange() override;

private:
  /// A source held here, its stream named once so the lock never asks it.
  struct Source {
    std::shared_ptr<AudioGraphOutputNode> node;
    std::optional<StreamId> id;
    /// Where the first frame read from it was, once it has played.
    std::optional<long> beganAt;
    /// Its state callback while hooked, else -1.
    int callbackId = -1;
    std::optional<StreamInfo> info;
  };

  std::optional<StreamId> nextStreamId();
  std::list<Source>::iterator latestFinished(StreamId stream);
  /// The current source, taken under the lock so it is called outside it.
  std::shared_ptr<AudioGraphOutputNode> current() const;
  /// The current source, and a token that stops once it is replaced.
  std::pair<std::shared_ptr<AudioGraphOutputNode>, std::stop_token>
  currentUntilReplaced() const;

  std::list<Source> inputNodes;
  std::shared_ptr<AudioGraphOutputNode> currentInputNode = nullptr;
  std::optional<StreamId> currentId;
  std::optional<long> currentBeganAt;
  std::optional<StreamInfo> currentInfo;
  /// Sources that finished here, oldest first.
  std::list<Source> finished;

  mutable std::mutex mutex;
  std::stop_source stopSource;

  int stateCallbackId = -1;

  void switchToNextSource();
  /// Takes @p node's states as this switcher's, once it is current.
  void follow(const std::shared_ptr<AudioGraphOutputNode> &node);
  /// Moves the current source to the finished ones; the lock must be held.
  void retireCurrent(bool callbackUnhooked);
};

#endif
