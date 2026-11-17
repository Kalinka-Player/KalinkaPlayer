#pragma once

#include <chrono>
#include <cstddef>

/**
 * @brief A gain that ramps to each new target over a fixed length of audio
 * instead of jumping, which would click.
 *
 * A target that arrives mid-ramp carries on from the level reached.
 * @note Not thread-safe: it belongs to the thread that pulls audio.
 */
class GainRamp {
public:
  /// A stretch of frames whose gain moves linearly from `from`, reaching `to`
  /// on its last frame; a hold when the two are equal.
  struct Span {
    double from;
    double to;
    size_t frames;
  };

  explicit GainRamp(std::chrono::microseconds length);

  /// Frames a ramp of @p length lasts at @p rate: at least one.
  static size_t framesFor(std::chrono::microseconds length, unsigned rate);

  /// Heads for @p target from the level reached, over the ramp's length at
  /// @p rate frames a second; the same target again changes nothing.
  void aim(double target, unsigned rate);
  /// Takes @p target at once, for audio after silence, with nothing to click
  /// against.
  void settle(double target);
  /// The next stretch of at most @p frames frames: the rest of a ramp, or a
  /// hold at the target once it is reached.
  Span take(size_t frames);
  /// Whether it holds at exactly @p gain.
  bool holding(double gain) const { return framesLeft == 0 && level == gain; }
  /// Frames until the ramp reaches its target; 0 while it holds.
  size_t left() const { return framesLeft; }

private:
  std::chrono::microseconds length;
  double level = 1.0;
  double target = 1.0;
  size_t framesLeft = 0;
};
