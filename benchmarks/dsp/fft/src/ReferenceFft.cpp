#include "ReferenceFft.h"

#include <bit>
#include <cmath>
#include <numbers>

namespace {

using Complex = std::complex<long double>;

/// exp(-2πi k / n) for k below @p count.
std::vector<Complex> twiddles(size_t n, size_t count) {
  std::vector<Complex> w(count);
  for (size_t k = 0; k < count; ++k) {
    const long double angle = -std::numbers::pi_v<long double> *
                              (2.0L * static_cast<long double>(k) /
                               static_cast<long double>(n));
    w[k] = {std::cos(angle), std::sin(angle)};
  }
  return w;
}

size_t reverseBits(size_t value, int bits) {
  size_t reversed = 0;
  for (int i = 0; i < bits; ++i) {
    reversed = (reversed << 1) | ((value >> i) & 1);
  }
  return reversed;
}

} // namespace

std::vector<Complex> referenceSpectrum(std::span<const long double> x) {
  const size_t n = x.size();
  const int bits = std::countr_zero(n);
  std::vector<Complex> a(n);
  for (size_t i = 0; i < n; ++i) {
    a[reverseBits(i, bits)] = x[i];
  }
  const std::vector<Complex> w = twiddles(n, n / 2);
  for (size_t half = 1; half < n; half *= 2) {
    const size_t stride = n / (2 * half);
    for (size_t start = 0; start < n; start += 2 * half) {
      for (size_t k = 0; k < half; ++k) {
        const Complex t = w[k * stride] * a[start + k + half];
        a[start + k + half] = a[start + k] - t;
        a[start + k] += t;
      }
    }
  }
  a.resize(n / 2 + 1);
  return a;
}

std::vector<Complex> naiveSpectrum(std::span<const long double> x) {
  const size_t n = x.size();
  const std::vector<Complex> w = twiddles(n, n);
  std::vector<Complex> bins(n / 2 + 1);
  for (size_t k = 0; k <= n / 2; ++k) {
    Complex sum = 0;
    for (size_t j = 0; j < n; ++j) {
      sum += x[j] * w[(j * k) % n];
    }
    bins[k] = sum;
  }
  return bins;
}
