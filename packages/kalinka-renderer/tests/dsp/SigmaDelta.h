#ifndef HARNESS_SIGMA_DELTA_H
#define HARNESS_SIGMA_DELTA_H

#include <cstdint>
#include <span>
#include <vector>

namespace harness {

/**
 * @brief A one-bit sigma-delta modulator that turns a known signal into DSD,
 * for fixtures whose content a converter can be measured against.
 *
 * Its noise transfer function has its zeros spread optimally across the audio
 * band (roots of the Legendre polynomial) and Butterworth poles placed so the
 * out-of-band gain peaks at @p maxGain (Lee's rule). It is realised in
 * error-feedback form, so the signal reaches the output with a gain of
 * exactly one.
 */
class SigmaDelta {
public:
  /**
   * @param oversampling The one-bit rate over twice the audio band edge.
   * @param maxGain 1.4 holds a seventh-order loop stable at half modulation
   * from DSD64 to DSD256; 1.5 does not above DSD64.
   */
  SigmaDelta(unsigned order, double oversampling, double maxGain = 1.4);

  /**
   * @brief One bit per input sample, 1 for +1 and 0 for -1. Input is at the
   * one-bit rate, full scale ±1; state carries over from the last call.
   * @throws std::runtime_error when the loop goes unstable.
   */
  std::vector<uint8_t> modulate(std::span<const double> input);

  /// |NTF| in dB at @p omega radians per sample.
  double noiseGainDb(double omega) const;

private:
  std::vector<double> numerator;   ///< Monic, in powers of z^-1.
  std::vector<double> denominator; ///< Monic, in powers of z^-1.
  std::vector<double> errors;      ///< Past quantisation errors, newest first.
  std::vector<double> feedback;    ///< Past loop outputs, newest first.
};

/// Packs one bit per entry into bytes, earliest bit in the MSB.
std::vector<uint8_t> packMsbFirst(std::span<const uint8_t> bits);

/**
 * @brief A DSF file holding @p channels, one bit per entry, at @p rate.
 * @param lsbFirst DSF's usual bit order; false writes MSB first.
 */
std::vector<uint8_t> dsfFile(const std::vector<std::vector<uint8_t>> &channels,
                             unsigned rate, bool lsbFirst = true);

/// An uncompressed DSDIFF file holding @p channels at @p rate.
std::vector<uint8_t> dffFile(const std::vector<std::vector<uint8_t>> &channels,
                             unsigned rate);

} // namespace harness

#endif
