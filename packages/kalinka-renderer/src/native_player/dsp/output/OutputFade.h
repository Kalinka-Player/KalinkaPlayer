#pragma once

#include <chrono>
#include <cstddef>

/// How long a pause, a resume or a seek fades over: no click, and too short to
/// be heard as a fade.
inline constexpr std::chrono::milliseconds kFadeLength{10};

/// Frames a fade lasts at @p rate.
size_t fadeFrames(unsigned rate);

/**
 * @brief Fades the output briefly, so audio that stops or starts mid-waveform
 * does not click.
 *
 * Each call takes effect from the next audio read. A fade lasts fadeFrames()
 * frames of what is read, DSD passes unfaded, and once the level is back at
 * full what is read is untouched again.
 * @note Called from the thread that pulls audio, between its reads.
 */
class OutputFade {
public:
  virtual ~OutputFade() = default;

  /// Fades to silence from the level reached, and reads silence after.
  virtual void fadeOut() = 0;
  /// Fades back to full level from the level reached.
  virtual void fadeIn() = 0;
  /// Takes @p level at once: 1 for audio that starts at its own beginning, 0
  /// for audio that starts mid-waveform and fades in.
  virtual void settle(double level) = 0;
};
