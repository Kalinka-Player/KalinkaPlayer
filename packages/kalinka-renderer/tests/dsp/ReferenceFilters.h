#ifndef HARNESS_REFERENCE_FILTERS_H
#define HARNESS_REFERENCE_FILTERS_H

#include <cstddef>
#include <span>
#include <vector>

namespace harness {

/// Biquad coefficients, normalised so a0 is 1, with feedback subtracted.
struct BiquadCoefficients {
  double b0, b1, b2, a1, a2;
};

/// The Audio EQ Cookbook peaking filter (Bristow-Johnson).
BiquadCoefficients peaking(double frequency, double gainDb, double q,
                           double rate);

/// The Audio EQ Cookbook low shelf, by its shelf slope S.
BiquadCoefficients lowShelf(double frequency, double gainDb, double slope,
                            double rate);

/**
 * @brief A cascade of biquads in transposed direct form II, in double, one
 * channel: the plain reference optimised kernels are checked against.
 */
class BiquadCascade {
public:
  explicit BiquadCascade(std::vector<BiquadCoefficients> sections);
  void process(std::span<const double> in, std::span<double> out);
  void reset();

private:
  std::vector<BiquadCoefficients> sections;
  std::vector<double> z1, z2;
};

/**
 * @brief A linear-phase FIR of @p taps (odd) whose magnitude follows
 * @p gainDb at each frequency, by frequency sampling and a Blackman window.
 * Its peak, and so its latency, is at (taps - 1) / 2.
 */
template <typename Curve>
std::vector<double> linearPhaseFir(size_t taps, double rate, Curve gainDb);

/// The full linear convolution of @p signal with @p filter, by FFT.
std::vector<double> convolve(std::span<const double> signal,
                             std::span<const double> filter);

} // namespace harness

#include "ReferenceFilters.inl"

#endif
