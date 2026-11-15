#include "Analysis.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <numbers>
#include <stdexcept>

namespace harness {
namespace {
constexpr double kPi = std::numbers::pi;
// What a silent residual reports instead of minus infinity.
constexpr double kFloorDb = -400.0;

double dbOfPower(double power) {
  return power > 0 ? 10 * std::log10(power) : kFloorDb;
}

std::complex<long double> transferAt(std::span<const double> impulse,
                                     double frequency, double rate) {
  const long double omega = 2 * std::numbers::pi_v<long double> * frequency / rate;
  std::complex<long double> sum = 0;
  for (size_t n = 0; n < impulse.size(); ++n) {
    sum += (long double)impulse[n] * std::polar(1.0L, -omega * (long double)n);
  }
  return sum;
}
} // namespace

std::vector<std::complex<double>> fft(std::vector<std::complex<double>> data) {
  const size_t n = data.size();
  if (!std::has_single_bit(n)) {
    throw std::invalid_argument("FFT size must be a power of two");
  }
  for (size_t i = 1, j = 0; i < n; ++i) {
    size_t bit = n >> 1;
    for (; j & bit; bit >>= 1) {
      j ^= bit;
    }
    j ^= bit;
    if (i < j) {
      std::swap(data[i], data[j]);
    }
  }
  std::vector<std::complex<double>> twiddle(n / 2);
  for (size_t k = 0; k < n / 2; ++k) {
    twiddle[k] = std::polar(1.0, -2 * kPi * double(k) / double(n));
  }
  for (size_t length = 2; length <= n; length <<= 1) {
    const size_t stride = n / length;
    for (size_t start = 0; start < n; start += length) {
      for (size_t k = 0; k < length / 2; ++k) {
        const auto even = data[start + k];
        const auto odd = data[start + k + length / 2] * twiddle[k * stride];
        data[start + k] = even + odd;
        data[start + k + length / 2] = even - odd;
      }
    }
  }
  return data;
}

std::vector<std::complex<double>> realSpectrum(std::span<const double> signal,
                                               size_t size) {
  std::vector<std::complex<double>> padded(size);
  std::copy_n(signal.begin(), std::min(signal.size(), size), padded.begin());
  auto spectrum = fft(std::move(padded));
  spectrum.resize(size / 2 + 1);
  return spectrum;
}

std::vector<double> blackmanHarris7(size_t size) {
  constexpr double a[] = {0.27105140069342, 0.43329793923448,
                          0.21812299954311, 0.06592544638803,
                          0.01081174209837, 0.00077658482522,
                          0.00001388721735};
  std::vector<double> window(size);
  for (size_t n = 0; n < size; ++n) {
    double sum = 0;
    for (size_t k = 0; k < std::size(a); ++k) {
      sum += (k % 2 ? -a[k] : a[k]) *
             std::cos(2 * kPi * double(k) * double(n) / double(size));
    }
    window[n] = sum;
  }
  return window;
}

double bandPowerDb(std::span<const double> signal, double rate, double low,
                   double high) {
  if (signal.empty()) {
    return kFloorDb;
  }
  const auto window = blackmanHarris7(signal.size());
  std::vector<double> weighted(signal.size());
  double windowPower = 0;
  for (size_t n = 0; n < signal.size(); ++n) {
    weighted[n] = signal[n] * window[n];
    windowPower += window[n] * window[n];
  }
  const size_t size = std::bit_ceil(signal.size());
  const auto spectrum = realSpectrum(weighted, size);
  double power = 0;
  for (size_t k = 1; k < size / 2; ++k) {
    const double frequency = double(k) * rate / double(size);
    if (frequency >= low && frequency <= high) {
      power += std::norm(spectrum[k]);
    }
  }
  power *= 2 / (double(size) * windowPower);
  return dbOfPower(power / 0.5);
}

double gainDb(std::span<const double> impulse, double frequency, double rate) {
  return 20 * std::log10(double(std::abs(transferAt(impulse, frequency, rate))));
}

double phaseAt(std::span<const double> impulse, double frequency,
               double rate) {
  return double(std::arg(transferAt(impulse, frequency, rate)));
}

Response response(std::span<const double> impulse, double rate, size_t size) {
  const auto spectrum = realSpectrum(impulse, size);
  Response out;
  double previous = 0, unwrap = 0;
  for (size_t k = 0; k < spectrum.size(); ++k) {
    out.frequency.push_back(double(k) * rate / double(size));
    out.magnitudeDb.push_back(20 * std::log10(std::abs(spectrum[k])));
    const double phase = std::arg(spectrum[k]);
    if (k > 0) {
      const double jump = phase - previous;
      unwrap -= 2 * kPi * std::round(jump / (2 * kPi));
    }
    previous = phase;
    out.phase.push_back(phase + unwrap);
  }
  return out;
}

Distortion thdPlusN(std::span<const double> signal, double rate,
                    double frequency, double low, double high) {
  const long double omega = 2 * std::numbers::pi_v<long double> * frequency / rate;
  long double m[3][4] = {};
  for (size_t n = 0; n < signal.size(); ++n) {
    const long double basis[3] = {std::cos(omega * n), std::sin(omega * n), 1};
    for (int r = 0; r < 3; ++r) {
      for (int c = 0; c < 3; ++c) {
        m[r][c] += basis[r] * basis[c];
      }
      m[r][3] += basis[r] * signal[n];
    }
  }
  for (int pivot = 0; pivot < 3; ++pivot) {
    for (int r = pivot + 1; r < 3; ++r) {
      const long double f = m[r][pivot] / m[pivot][pivot];
      for (int c = pivot; c < 4; ++c) {
        m[r][c] -= f * m[pivot][c];
      }
    }
  }
  long double fit[3];
  for (int r = 2; r >= 0; --r) {
    long double sum = m[r][3];
    for (int c = r + 1; c < 3; ++c) {
      sum -= m[r][c] * fit[c];
    }
    fit[r] = sum / m[r][r];
  }
  std::vector<double> residual(signal.size());
  for (size_t n = 0; n < signal.size(); ++n) {
    residual[n] = double(signal[n] - (fit[0] * std::cos(omega * n) +
                                      fit[1] * std::sin(omega * n) + fit[2]));
  }
  const double amplitude = double(std::hypot(fit[0], fit[1]));
  const double fundamental = 20 * std::log10(amplitude);
  return {fundamental,
          bandPowerDb(residual, rate, low, high) - fundamental};
}

Null nullTest(std::span<const double> reference,
              std::span<const double> measured, long maxLag, bool matchGain) {
  const size_t size = std::bit_ceil(reference.size() + measured.size());
  std::vector<std::complex<double>> a(size), b(size);
  std::copy(reference.begin(), reference.end(), a.begin());
  std::copy(measured.begin(), measured.end(), b.begin());
  a = fft(std::move(a));
  b = fft(std::move(b));
  for (size_t k = 0; k < size; ++k) {
    b[k] = std::conj(std::conj(a[k]) * b[k]);
  }
  const auto correlation = fft(std::move(b));
  long lag = 0;
  double best = -1;
  for (long l = -maxLag; l <= maxLag; ++l) {
    const double c =
        std::abs(correlation[size_t((l + long(size)) % long(size))].real());
    if (c > best) {
      best = c;
      lag = l;
    }
  }
  const size_t from = size_t(std::max(0L, -lag));
  const size_t to =
      size_t(std::min(long(reference.size()), long(measured.size()) - lag));
  long double cross = 0, energy = 0;
  for (size_t n = from; n < to; ++n) {
    cross += (long double)reference[n] * measured[n + lag];
    energy += (long double)reference[n] * reference[n];
  }
  const double gain = matchGain && energy > 0 ? double(cross / energy) : 1.0;
  double peak = 0;
  long double power = 0;
  for (size_t n = from; n < to; ++n) {
    const double r = measured[n + lag] - gain * reference[n];
    peak = std::max(peak, std::abs(r));
    power += (long double)r * r;
  }
  const double mean = to > from ? double(power / (to - from)) : 0.0;
  return {lag, gain, peak > 0 ? 20 * std::log10(peak) : kFloorDb,
          dbOfPower(mean / 0.5)};
}

Clicks findClicks(std::span<const double> signal, double frequency,
                  double rate, double amplitude, double slack) {
  Clicks out{0, 2 * amplitude * std::sin(kPi * frequency / rate) + slack, {}};
  for (size_t n = 1; n < signal.size(); ++n) {
    const double step = std::abs(signal[n] - signal[n - 1]);
    out.largestStep = std::max(out.largestStep, step);
    if (step > out.allowedStep) {
      out.at.push_back(n);
    }
  }
  return out;
}

} // namespace harness
