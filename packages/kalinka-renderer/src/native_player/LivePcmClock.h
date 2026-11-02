#pragma once

#include "AudioInfo.h"
#include <memory>
#include <soxr.h>
#include <span>
#include <vector>

/** @brief Tracks slow clock drift without following short network bursts. */
class LiveClockServo {
public:
  explicit LiveClockServo(unsigned rate) : rate(rate) {}
  double update(size_t queuedFrames, size_t inputFrames);
  bool active() const { return frames > uint64_t(rate) * 2; }

private:
  const unsigned rate;
  uint64_t frames = 0;
  double errorSeconds = 0;
};

/** @brief Adapts live PCM to the DAC clock, starting at unity rate.
 *  The filter stays in the path from the first frame to avoid a midstream
 *  startup transient. File playback and native DSD do not use this adapter.
 */
class LivePcmClock {
public:
  explicit LivePcmClock(StreamAudioFormat format);
  std::span<const uint8_t> process(std::span<const uint8_t> pcm,
                                   size_t queuedFrames);
  double pendingInputFrames() const;

private:
  StreamAudioFormat format;
  LiveClockServo servo;
  std::unique_ptr<soxr, decltype(&soxr_delete)> engine{nullptr, soxr_delete};
  double ratio = 1;
  std::vector<int32_t> integerIn, integerOut;
  std::vector<float> floatIn, floatOut;
  std::vector<uint8_t> output;
};
