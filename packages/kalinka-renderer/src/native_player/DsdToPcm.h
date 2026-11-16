#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

/// What a DSD-to-PCM conversion keeps above 20 kHz.
enum class DsdFilter {
  /// Flat to 20 kHz, 110 dB down from 40 kHz.
  Cut30k,
  /// Flat to 40 kHz, 110 dB down from 60 kHz; wide where that is past 0.45 of
  /// the output rate.
  Cut50k,
  /// Flat to 0.45 of the output rate, or less straight out of stage 1.
  Wide,
};

/// Which bit of a DSD byte holds its earliest sample.
enum class DsdBitOrder { MsbFirst, LsbFirst };

/**
 * @brief Converts one-bit DSD into float64 PCM at 2, 4 or 8 times 44.1 or
 * 48 kHz.
 *
 * Stage 1 divides by 8 one byte at a time from integer tables; half-bands
 * divide by 2 each; a band filter at the output rate sets the ultrasonic
 * cut-off. Whatever would fold below the band filter's stop edge, or into the
 * wide passband, is at least 165 dB down, which keeps a DSD128 fixture's
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
  struct Spec {
    unsigned dsdRate;
    unsigned pcmRate;
    unsigned channels;
    DsdFilter filter;
    DsdBitOrder order;
  };

  /// @throws std::invalid_argument for rates or channels it has no chain for.
  explicit DsdToPcm(const Spec &spec);

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
  struct Fir {
    const double *taps;
    size_t size;
  };
  struct Channel {
    std::vector<uint8_t> bytes;
    std::vector<std::vector<double>> stages;
    std::vector<double> band;
  };

  unsigned ratio_;
  size_t latency_ = 0;
  double passbandHz_;
  bool lsbFirst;
  uint8_t idle;
  unsigned channels;
  size_t groups;
  /// Stage 1's first groups, MSB first, whole numbers in units of 2^-36.
  std::vector<std::array<double, 256>> tables;
  std::vector<Fir> halfBands;
  Fir band{nullptr, 0};
  /// Per half-band, whether one input already waits for its pair.
  std::vector<unsigned> pending;
  std::vector<size_t> blockInputs;
  std::vector<Channel> state;

  size_t stage1(const uint8_t *in, size_t bytes, Channel &channel) const;
  size_t halfBand(size_t stage, size_t count, Channel &channel) const;
  void bandFilter(size_t count, Channel &channel, double *out) const;
};
