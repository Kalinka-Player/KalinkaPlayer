#ifndef COUNTING_NODE_H
#define COUNTING_NODE_H

#include "AudioGraphNode.h"

#include <limits>
#include <vector>

/**
 * @brief A stereo S32 source whose every frame says which one it is.
 *
 * Frame n of the stream is `base + n + 1` on the left and its negation on the
 * right, so a reader can tell exactly which frames it got, in what order, and
 * from which stream, and silence is never mistaken for audio.
 */
class CountingNode : public AudioGraphOutputNode {
public:
  struct Options {
    unsigned rate = 48000;
    size_t first = 0;
    size_t errorAt = std::numeric_limits<size_t>::max();
    AudioSampleFormat format = PCM32_LE;
    bool knownLength = true;
  };

  CountingNode(std::optional<StreamId> id, int32_t base, size_t frames)
      : CountingNode(id, base, frames, Options{}) {}

  CountingNode(std::optional<StreamId> id, int32_t base, size_t frames,
               Options options)
      : AudioGraphNode(id), base(base), frames(frames), options(options),
        at(options.first) {
    info = StreamInfo{.format = {.sampleRate = options.rate,
                                 .channels = 2,
                                 .bitsPerSample = 32,
                                 .sampleFormat = options.format},
                      .streamType = StreamType::FRAMES,
                      .streamSize = options.knownLength ? frames : 0};
    setState({AudioGraphNodeState::STREAMING, long(at), info});
  }

  size_t read(void *data, size_t size) override {
    std::lock_guard lock(mutex);
    if (failed) {
      return 0;
    }
    const size_t end = std::min(frames, options.errorAt);
    const size_t count = std::min(size / 8, end - at);
    auto *out = static_cast<int32_t *>(data);
    for (size_t i = 0; i < count; ++i) {
      out[2 * i] = value(at + i);
      out[2 * i + 1] = -value(at + i);
    }
    at += count;
    if (at == options.errorAt) {
      failed = true;
      setState({AudioGraphNodeState::ERROR,
                StreamError{StreamErrorSource::DECODER, "broken"}});
    } else if (at == frames) {
      setState(StreamState(AudioGraphNodeState::FINISHED));
    }
    return count * 8;
  }

  size_t waitForData(std::stop_token = std::stop_token(),
                     size_t size = 1) override {
    std::lock_guard lock(mutex);
    return failed ? 0 : std::min(size, (frames - at) * 8);
  }

  size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds,
                        size_t size) override {
    return waitForData(token, size);
  }

  std::optional<long> streamReadPosition() const override {
    std::lock_guard lock(mutex);
    return long(at);
  }

  size_t seekTo(size_t frame) override {
    std::lock_guard lock(mutex);
    at = std::min(frame, frames);
    setState(StreamState{AudioGraphNodeState::PREPARING});
    if (at == frames) {
      setState(StreamState(AudioGraphNodeState::FINISHED));
    } else {
      setState({AudioGraphNodeState::STREAMING, long(at), info});
    }
    return at;
  }

  int32_t value(size_t frame) const { return base + int32_t(frame) + 1; }

  /// The left samples of frames [@p from, @p to).
  std::vector<int32_t> values(size_t from, size_t to) const {
    std::vector<int32_t> left;
    for (size_t frame = from; frame < to; ++frame) {
      left.push_back(value(frame));
    }
    return left;
  }

private:
  const int32_t base;
  const size_t frames;
  const Options options;
  StreamInfo info;
  mutable std::mutex mutex;
  size_t at;
  bool failed = false;
};

#endif
