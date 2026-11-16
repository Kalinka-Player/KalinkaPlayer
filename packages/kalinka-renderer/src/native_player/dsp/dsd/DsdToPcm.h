#pragma once

#include "dsp/dsd/DsdOptions.h"
#include "dsp/dsd/Stage1.h"
#include "dsp/fir/HalfBandDecimator.h"
#include "dsp/fir/SymmetricFir.h"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <vector>

/**
 * @brief Converts one-bit DSD into float64 PCM at 2, 4 or 8 times 44.1 or
 * 48 kHz.
 *
 * Stage 1 divides by 8 one byte at a time from integer tables; half-bands
 * divide by 2 each; a band filter at the output rate sets the ultrasonic
 * cut-off. Whatever would fold below the band filter's stop edge, or into the
 * wide passband, is at least 168 dB down, which keeps a DSD128 fixture's
 * THD+N. The filters come from scripts/design_dsd_filters.py and are linear
 * phase.
 *
 * Full modulation is ±1.0. The history starts as the idle pattern 0x69, in
 * time order whichever the bit order, which converts to exact zero, so a run
 * starts silent.
 * @note Not thread-safe. Allocates only when constructed.
 */
class DsdToPcm {
public:
  /// What to convert from and to; the kernel matters only to measurements.
  struct Spec {
    unsigned dsdRate;
    unsigned pcmRate;
    unsigned channels;
    DsdFilter filter;
    DsdBitOrder order;
    DsdKernel kernel = DsdKernel::Fast;
  };

  /// @throws std::invalid_argument for rates or channels it has no chain for.
  explicit DsdToPcm(const Spec &spec);

  /// Whether a converter can be made for @p spec.
  static bool converts(const Spec &spec);

  /// One-bit samples per output frame, a power of two of at least 8.
  unsigned ratio() const { return ratio_; }
  /// Output frames the PCM lags the DSD by, rounded to the nearest frame.
  size_t latency() const { return latency_; }
  /// Where the passband ends, in Hz.
  double passbandHz() const { return passbandHz_; }
  /// The most frames process() writes per channel for @p bytes.
  size_t maxFrames(size_t bytes) const { return bytes * 8 / ratio_ + 1; }

  /**
   * @brief Converts @p bytes bytes of each channel, interleaved one byte per
   * channel, into @p out, one array per channel.
   * @return The frames written to each channel.
   */
  size_t process(const uint8_t *in, size_t bytes, double *const *out);

  /// Forgets the history, as if every earlier byte had been 0x69.
  void reset();

private:
  /// One channel's filters after stage 1.
  struct Channel {
    std::vector<HalfBandDecimator> halfBands;
    std::optional<SymmetricFir> band;
  };

  unsigned ratio_;
  size_t latency_ = 0;
  double passbandHz_;
  unsigned channels;
  std::unique_ptr<Stage1> stage1;
  std::vector<Channel> state;
  /// Where each channel's output for the current block goes, and where stage
  /// 1 writes for it.
  std::vector<double *> blockTargets, stage1Targets;

  /// Where half-band @p stage takes its input or, past the last, the band
  /// filter, or @p out when there is neither.
  static double *feed(Channel &channel, size_t stage, double *out);
};
