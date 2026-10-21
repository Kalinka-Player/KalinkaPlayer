#ifndef GAIN_FADE_H
#define GAIN_FADE_H

#include "AudioSampleFormat.h"

#include <chrono>
#include <cstddef>

/**
 * @brief Software volume that fades to each new level instead of jumping.
 *
 * A gain that jumps mid-waveform is heard as a click, so each change is spread
 * over a fixed length of audio. A change that arrives mid-fade carries on from
 * the level reached. Once a fade lands on 1.0 the audio is left untouched.
 *
 * @note Not thread-safe: it belongs to the thread writing the audio, which
 * hands it the latest target with every buffer.
 */
class GainFade {
public:
  explicit GainFade(std::chrono::microseconds length);

  /// Takes @p gain at once, for audio that follows silence and so has nothing
  /// to click against.
  void settle(float gain);

  /// Applies the gain to the whole frames in @p buffer, fading toward
  /// @p target.
  void apply(void *buffer, size_t bytes, AudioSampleFormat format,
             unsigned channels, unsigned sampleRate, float target);

private:
  std::chrono::microseconds length;
  float level = 1.0f;
  float target = 1.0f;
  size_t framesLeft = 0;
};

#endif // GAIN_FADE_H
