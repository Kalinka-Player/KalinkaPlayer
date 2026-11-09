#ifndef AUDIO_GRAPH_CONTROLLER_H
#define AUDIO_GRAPH_CONTROLLER_H

#include "AudioGraphNode.h"

#include <list>

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

  virtual std::optional<long> streamReadPosition() const override;
  std::optional<uint64_t> liveSourceFrame() const override;

  virtual void acceptSourceChange() override;

private:
  std::optional<StreamId> nextStreamId();

  std::list<std::shared_ptr<AudioGraphOutputNode>> inputNodes;
  std::shared_ptr<AudioGraphOutputNode> currentInputNode = nullptr;

  mutable std::mutex mutex;
  std::stop_source stopSource;

  int stateCallbackId = -1;

  void switchToNextSource();
};

#endif
