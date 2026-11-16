#ifndef DSP_PROCESSOR_H
#define DSP_PROCESSOR_H

#include "AudioInfo.h"

#include <cstddef>
#include <cstdint>

/**
 * @brief A transformation DspStage runs audio through, frame for frame.
 *
 * Output lags input by the latency start() returns, and DspStage hides that
 * lag, so a processor states it rather than compensating for it itself.
 *
 * @note Not thread-safe: every call comes from the thread that pulls audio.
 * That thread must not allocate while it streams, so start() is where buffers
 * are sized and process() never resizes them.
 */
class DspProcessor {
public:
  virtual ~DspProcessor() = default;

  /// Whether audio in @p format goes through it; any other passes untouched.
  virtual bool handles(const StreamAudioFormat &format) const = 0;

  /**
   * @brief Begins a run in @p format, which handles() accepts, with no history.
   * @return how many frames output lags input.
   */
  virtual size_t start(const StreamAudioFormat &format) = 0;

  /**
   * @brief Runs @p frames whole frames of the started format from @p in into
   * @p out, which must not overlap it.
   * @param in The input, or null to feed silence and play out the tail.
   */
  virtual void process(const uint8_t *in, uint8_t *out, size_t frames) = 0;
};

#endif
