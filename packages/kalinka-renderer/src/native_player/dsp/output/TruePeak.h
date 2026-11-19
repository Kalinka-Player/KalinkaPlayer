#pragma once

#include <array>
#include <cstddef>
#include <vector>

/**
 * @brief The highest true peak of the samples it is fed, from an
 * interpolation to at least 176.4 kHz: 4 times at 44.1 and 48 kHz, 2 times at
 * 88.2 and 96 kHz, and the samples alone above, as ITU-R BS.1770 asks of a
 * true-peak meter.
 *
 * The interpolator is a 49-tap Kaiser-windowed sinc, so every fourth output
 * is an input sample and the others fall between them, each phase at unity
 * gain for a steady level. A signal that jumps reads its overshoot, as the
 * DAC's reconstruction would.
 * @note Not thread-safe: it belongs to the thread that pulls audio.
 */
class TruePeakMeter {
public:
  /// @param maxFrames The most frames one feed() takes.
  TruePeakMeter(size_t maxFrames, unsigned maxChannels);

  /// Measures @p frames frames of @p planes, at @p rate frames a second, as
  /// heard at @p gain. A change of rate or channels starts the history over.
  void feed(const double *const *planes, unsigned channels, size_t frames,
            unsigned rate, double gain);
  /// The highest absolute level since resetPeak(), linear.
  double peak() const { return peak_; }
  /// Forgets the peak. The samples before still lead into the next, so a
  /// gapless join is not read as a jump from silence.
  void resetPeak() { peak_ = 0; }

  /// Taps each interpolated phase has.
  static constexpr size_t kTaps = 12;

private:
  using Phase = std::array<double, kTaps>;
  /// The phases a quarter, half and three quarters of a sample on.
  static const std::array<Phase, 3> &phases();

  const size_t maxFrames;
  /// Per channel: the kTaps - 1 samples before, then the ones fed.
  std::vector<std::vector<double>> lines;
  unsigned rate = 0;
  unsigned channels = 0;
  double peak_ = 0;
};
