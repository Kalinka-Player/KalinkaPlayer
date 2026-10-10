#pragma once

#include "ContainerStreamDecoder.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cstring>
#include <vector>

namespace container_test {
using Bytes = std::vector<uint8_t>;

/**
 * @brief A decoder's source served from memory in reads of a few bytes, so
 * every header and block spans several of them. Seekable or sequential, and
 * of known or unknown length.
 */
class BytesInput : public AudioGraphOutputNode {
public:
  Bytes bytes;
  size_t position = 0;
  bool seekable;
  explicit BytesInput(Bytes data, bool seekable = true, bool knownLength = true)
      : bytes(std::move(data)), seekable(seekable) {
    setState({AudioGraphNodeState::STREAMING, 0,
              StreamInfo{.streamType = StreamType::BYTES,
                         .streamSize = knownLength ? bytes.size() : 0}});
  }
  size_t read(void *dest, size_t n) override {
    n = std::min({n, bytes.size() - position, size_t(13)});
    std::memcpy(dest, bytes.data() + position, n);
    position += n;
    return n;
  }
  size_t waitForData(std::stop_token, size_t n) override {
    return std::min(n, bytes.size() - position);
  }
  size_t waitForDataFor(std::stop_token t, std::chrono::milliseconds,
                        size_t n) override {
    return waitForData(t, n);
  }
  size_t seekTo(size_t n) override {
    if (!seekable || n > bytes.size())
      return size_t(-1);
    position = n;
    return n;
  }
};

/// Everything the decoder plays until it finishes or fails, asked for in
/// requests that are deliberately not frame-aligned.
inline Bytes drain(ContainerStreamDecoder &node) {
  using namespace std::chrono_literals;
  Bytes result;
  std::array<uint8_t, 2401> data;
  const auto deadline = std::chrono::steady_clock::now() + 3s;
  while (std::chrono::steady_clock::now() < deadline) {
    if (node.waitForDataFor({}, 50ms, 1)) {
      const auto n = node.read(data.data(), data.size());
      result.insert(result.end(), data.begin(), data.begin() + n);
    } else if (node.getState().state == AudioGraphNodeState::FINISHED ||
               node.getState().state == AudioGraphNodeState::ERROR)
      return result;
  }
  ADD_FAILURE() << "decoder did not finish";
  return result;
}
} // namespace container_test
