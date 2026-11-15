#include "ReferenceFilters.h"

#include <algorithm>

namespace harness {

BiquadCoefficients peaking(double frequency, double gainDb, double q,
                           double rate) {
  const double a = std::pow(10.0, gainDb / 40);
  const double w = 2 * std::numbers::pi * frequency / rate;
  const double alpha = std::sin(w) / (2 * q);
  const double a0 = 1 + alpha / a;
  return {(1 + alpha * a) / a0, -2 * std::cos(w) / a0, (1 - alpha * a) / a0,
          -2 * std::cos(w) / a0, (1 - alpha / a) / a0};
}

BiquadCoefficients lowShelf(double frequency, double gainDb, double slope,
                            double rate) {
  const double a = std::pow(10.0, gainDb / 40);
  const double w = 2 * std::numbers::pi * frequency / rate;
  const double alpha =
      std::sin(w) / 2 * std::sqrt((a + 1 / a) * (1 / slope - 1) + 2);
  const double c = std::cos(w), root = 2 * std::sqrt(a) * alpha;
  const double a0 = (a + 1) + (a - 1) * c + root;
  return {a * ((a + 1) - (a - 1) * c + root) / a0,
          2 * a * ((a - 1) - (a + 1) * c) / a0,
          a * ((a + 1) - (a - 1) * c - root) / a0,
          -2 * ((a - 1) + (a + 1) * c) / a0,
          ((a + 1) + (a - 1) * c - root) / a0};
}

BiquadCascade::BiquadCascade(std::vector<BiquadCoefficients> sections)
    : sections(std::move(sections)), z1(this->sections.size(), 0.0),
      z2(this->sections.size(), 0.0) {}

void BiquadCascade::process(std::span<const double> in, std::span<double> out) {
  for (size_t n = 0; n < in.size(); ++n) {
    double x = in[n];
    for (size_t s = 0; s < sections.size(); ++s) {
      const auto &c = sections[s];
      const double y = c.b0 * x + z1[s];
      z1[s] = c.b1 * x - c.a1 * y + z2[s];
      z2[s] = c.b2 * x - c.a2 * y;
      x = y;
    }
    out[n] = x;
  }
}

void BiquadCascade::reset() {
  std::ranges::fill(z1, 0.0);
  std::ranges::fill(z2, 0.0);
}

std::vector<double> convolve(std::span<const double> signal,
                             std::span<const double> filter) {
  const size_t length = signal.size() + filter.size() - 1;
  const size_t size = std::bit_ceil(length);
  std::vector<std::complex<double>> a(size), b(size);
  std::ranges::copy(signal, a.begin());
  std::ranges::copy(filter, b.begin());
  a = fft(std::move(a));
  b = fft(std::move(b));
  for (size_t k = 0; k < size; ++k) {
    a[k] = std::conj(a[k] * b[k]);
  }
  const auto product = fft(std::move(a));
  std::vector<double> out(length);
  for (size_t n = 0; n < length; ++n) {
    out[n] = product[n].real() / double(size);
  }
  return out;
}

} // namespace harness
