#ifndef DELAY_LINE_H
#define DELAY_LINE_H

#include "dsp/DspProcessor.h"

#include <chrono>
#include <vector>

/**
 * @brief Holds PCM back by a fixed time and changes nothing else.
 *
 * The stand-in for a filter's latency, so DspStage's latency handling is
 * proven before any filter exists: what comes out is what went in, bit for
 * bit. DSD is left alone, because no DSP may touch it.
 */
class DelayLine final : public DspProcessor {
public:
  explicit DelayLine(std::chrono::milliseconds delay);

  bool handles(const StreamAudioFormat &format) const override;
  /// Nothing: no sample changes.
  SignalPath describe(const StreamAudioFormat &) const override { return {}; }
  size_t start(const StreamAudioFormat &format) override;
  size_t process(const uint8_t *in, uint8_t *out, size_t frames) override;

private:
  const std::chrono::milliseconds delay;
  size_t frameBytes = 0;
  std::vector<uint8_t> held;
  size_t oldest = 0;
};

#endif
