#ifndef REFERENCE_FFT_H
#define REFERENCE_FFT_H

#include <complex>
#include <span>
#include <vector>

/**
 * @brief Bins 0 … n/2 of the DFT of @p x, by radix-2 decimation in time in
 * long double, the yardstick for every library's accuracy.
 *
 * Each twiddle comes from cosl/sinl directly, not by recurrence, so the error
 * is that of the butterflies alone. @p x must have a power-of-two length.
 */
std::vector<std::complex<long double>>
referenceSpectrum(std::span<const long double> x);

/// The same bins by direct summation, O(n²): the check on referenceSpectrum().
std::vector<std::complex<long double>>
naiveSpectrum(std::span<const long double> x);

#endif
