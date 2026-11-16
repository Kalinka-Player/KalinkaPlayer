#ifndef DSP_PROCESSOR_H
#define DSP_PROCESSOR_H

#include "AudioInfo.h"

#include <cstddef>
#include <cstdint>

/**
 * @brief A transformation DspStage runs audio through.
 *
 * It may change the format and divide the rate: every `ratio` input frames
 * make one output frame, so output frame n stands for input frame n × ratio.
 * Output lags input by the latency start() returns, in output frames, and
 * DspStage hides that lag, so a processor states it rather than compensating
 * for it itself.
 *
 * @note Not thread-safe, but for output(), which states may be translated
 * with from any thread: every other call comes from the thread that pulls
 * audio. That thread must not allocate while it streams, so start() is where
 * buffers are sized and process() never resizes them.
 */
class DspProcessor {
public:
  /// What a run in some format puts out.
  struct Output {
    StreamAudioFormat format;
    /// Input frames to one output frame.
    unsigned ratio = 1;
  };

  virtual ~DspProcessor() = default;

  /// Whether audio in @p format goes through it; any other passes untouched.
  virtual bool handles(const StreamAudioFormat &format) const = 0;

  /// What a run in @p format, which handles() accepts, puts out: by default
  /// the same format, frame for frame.
  virtual Output output(const StreamAudioFormat &format) const {
    return {format, 1};
  }

  /**
   * @brief Begins a run in @p format, which handles() accepts, with no history.
   * @return how many output frames output lags input.
   */
  virtual size_t start(const StreamAudioFormat &format) = 0;

  /**
   * @brief Runs @p frames whole input frames of the started format from @p in
   * into @p out, which must not overlap it.
   * @param in The input, or null to feed silence and play out the tail.
   * @param out Room for at most frames / ratio + 1 output frames.
   * @return The output frames written: over a run, one for every `ratio`
   * input frames fed, silence included.
   */
  virtual size_t process(const uint8_t *in, uint8_t *out, size_t frames) = 0;
};

#endif
