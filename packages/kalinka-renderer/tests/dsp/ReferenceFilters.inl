#include "Analysis.h"

#include <bit>
#include <cmath>
#include <complex>
#include <numbers>

namespace harness {

template <typename Curve>
std::vector<double> linearPhaseFir(size_t taps, double rate, Curve gainDb) {
  const size_t size = std::bit_ceil(taps) * 2;
  std::vector<std::complex<double>> spectrum(size);
  const double centre = double(taps - 1) / 2;
  for (size_t k = 0; k <= size / 2; ++k) {
    const double frequency = double(k) * rate / double(size);
    const double magnitude = std::pow(10.0, gainDb(frequency) / 20);
    // Conjugated, so the forward transform below runs as an inverse.
    spectrum[k] = std::polar(magnitude, 2 * std::numbers::pi * double(k) *
                                            centre / double(size));
    if (k > 0 && k < size / 2) {
      spectrum[size - k] = std::conj(spectrum[k]);
    }
  }
  const auto impulse = fft(std::move(spectrum));
  std::vector<double> fir(taps);
  for (size_t n = 0; n < taps; ++n) {
    const double x = 2 * std::numbers::pi * double(n) / double(taps - 1);
    const double window = 0.42 - 0.5 * std::cos(x) + 0.08 * std::cos(2 * x);
    fir[n] = impulse[n].real() / double(size) * window;
  }
  return fir;
}

} // namespace harness
