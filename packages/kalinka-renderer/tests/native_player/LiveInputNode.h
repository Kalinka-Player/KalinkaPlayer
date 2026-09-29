#ifndef LIVE_INPUT_NODE_H
#define LIVE_INPUT_NODE_H

#include "AudioGraphNode.h"
#include "Buffer.h"

/**
 * @brief Bytes of unknown length, as a live producer sends them.
 *
 * Never finishes: a producer pacing on playback holds its next bytes until
 * what it sent has played. Tests write into `bytes` directly.
 */
class LiveInputNode : public AudioGraphOutputNode {
public:
  explicit LiveInputNode(size_t capacity = 16384) : bytes(capacity) {
    setState({AudioGraphNodeState::STREAMING, 0,
              StreamInfo{.streamType = StreamType::BYTES}});
  }
  size_t read(void *out, size_t count) override {
    return bytes.read(static_cast<uint8_t *>(out), count);
  }
  size_t waitForData(std::stop_token token, size_t count) override {
    return bytes.waitForData(token, count);
  }
  size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds timeout,
                        size_t count) override {
    return bytes.waitForDataFor(token, timeout, count);
  }

  Buffer<uint8_t> bytes;
};

#endif
