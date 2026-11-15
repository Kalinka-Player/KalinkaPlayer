#include "SigmaDelta.h"

#include <algorithm>
#include <cmath>
#include <complex>
#include <numbers>
#include <stdexcept>
#include <string>

namespace harness {
namespace {
using Polynomial = std::vector<std::complex<double>>;

// Roots of the Legendre polynomial of degree n, in (-1, 1), by Newton.
std::vector<double> legendreRoots(unsigned n) {
  std::vector<double> roots;
  for (unsigned i = 1; i <= n; ++i) {
    double x = std::cos(std::numbers::pi * (i - 0.25) / (n + 0.5));
    for (int iteration = 0; iteration < 100; ++iteration) {
      double p0 = 1, p1 = x;
      for (unsigned k = 2; k <= n; ++k) {
        const double p2 = ((2.0 * k - 1) * x * p1 - (k - 1.0) * p0) / k;
        p0 = p1;
        p1 = p2;
      }
      const double derivative = n * (x * p1 - p0) / (x * x - 1);
      const double step = p1 / derivative;
      x -= step;
      if (std::abs(step) < 1e-15) {
        break;
      }
    }
    roots.push_back(x);
  }
  return roots;
}

// The monic polynomial in z^-1 with these roots, which come in conjugates.
std::vector<double> fromRoots(const Polynomial &roots) {
  Polynomial poly{1.0};
  for (const auto &root : roots) {
    Polynomial next(poly.size() + 1, 0.0);
    for (size_t k = 0; k < poly.size(); ++k) {
      next[k] += poly[k];
      next[k + 1] -= root * poly[k];
    }
    poly = std::move(next);
  }
  std::vector<double> real;
  for (const auto &c : poly) {
    real.push_back(c.real());
  }
  return real;
}

std::complex<double> evaluate(const std::vector<double> &poly, double omega) {
  std::complex<double> sum = 0;
  for (size_t k = 0; k < poly.size(); ++k) {
    sum += poly[k] * std::polar(1.0, -omega * double(k));
  }
  return sum;
}

double peakGain(const std::vector<double> &numerator,
                const std::vector<double> &denominator) {
  double peak = 0;
  for (int i = 0; i <= 4096; ++i) {
    const double omega = std::numbers::pi * i / 4096;
    peak = std::max(peak, std::abs(evaluate(numerator, omega) /
                                   evaluate(denominator, omega)));
  }
  return peak;
}

// Butterworth poles of the given order, cut off at omega, by bilinear transform.
std::vector<double> butterworthPoles(unsigned order, double omega) {
  const double warped = std::tan(omega / 2);
  Polynomial poles;
  for (unsigned k = 0; k < order; ++k) {
    const auto s = std::polar(
        warped, std::numbers::pi * (2.0 * k + order + 1) / (2.0 * order));
    poles.push_back((1.0 + s) / (1.0 - s));
  }
  return fromRoots(poles);
}

void put(std::vector<uint8_t> &out, const char *tag) {
  for (int i = 0; i < 4; ++i) {
    out.push_back(uint8_t(tag[i]));
  }
}

void put(std::vector<uint8_t> &out, uint64_t value, unsigned width,
         bool little) {
  for (unsigned i = 0; i < width; ++i) {
    out.push_back(uint8_t(value >> (8 * (little ? i : width - 1 - i))));
  }
}

uint8_t reversed(uint8_t b) {
  b = uint8_t((b & 0x55) << 1 | (b >> 1 & 0x55));
  b = uint8_t((b & 0x33) << 2 | (b >> 2 & 0x33));
  return uint8_t(b << 4 | b >> 4);
}
} // namespace

SigmaDelta::SigmaDelta(unsigned order, double oversampling, double maxGain) {
  if (order < 1 || maxGain <= 1) {
    throw std::invalid_argument("A modulator needs an order and a gain above 1");
  }
  const double band = std::numbers::pi / oversampling;
  Polynomial zeros;
  for (double root : legendreRoots(order)) {
    zeros.push_back(std::polar(1.0, band * root));
  }
  numerator = fromRoots(zeros);
  double low = 1e-6, high = std::numbers::pi - 1e-6;
  for (int i = 0; i < 200; ++i) {
    const double mid = (low + high) / 2;
    (peakGain(numerator, butterworthPoles(order, mid)) < maxGain ? low : high) =
        mid;
  }
  denominator = butterworthPoles(order, low);
  errors.assign(order, 0.0);
  feedback.assign(order, 0.0);
}

double SigmaDelta::noiseGainDb(double omega) const {
  return 20 * std::log10(std::abs(evaluate(numerator, omega) /
                                  evaluate(denominator, omega)));
}

std::vector<uint8_t> SigmaDelta::modulate(std::span<const double> input) {
  const size_t order = errors.size();
  std::vector<uint8_t> bits(input.size());
  for (size_t n = 0; n < input.size(); ++n) {
    double loop = 0;
    for (size_t k = 0; k < order; ++k) {
      loop += (numerator[k + 1] - denominator[k + 1]) * errors[k] -
              denominator[k + 1] * feedback[k];
    }
    const double v = input[n] + loop;
    if (!std::isfinite(v) || std::abs(v) > 64) {
      throw std::runtime_error("Sigma-delta loop went unstable at sample " +
                               std::to_string(n));
    }
    const double y = v >= 0 ? 1.0 : -1.0;
    std::copy_backward(errors.begin(), errors.end() - 1, errors.end());
    std::copy_backward(feedback.begin(), feedback.end() - 1, feedback.end());
    errors[0] = y - v;
    feedback[0] = loop;
    bits[n] = y > 0;
  }
  return bits;
}

std::vector<uint8_t> packMsbFirst(std::span<const uint8_t> bits) {
  std::vector<uint8_t> bytes((bits.size() + 7) / 8, 0);
  for (size_t n = 0; n < bits.size(); ++n) {
    if (bits[n]) {
      bytes[n / 8] |= uint8_t(0x80 >> (n % 8));
    }
  }
  return bytes;
}

std::vector<uint8_t> dsfFile(const std::vector<std::vector<uint8_t>> &channels,
                             unsigned rate, bool lsbFirst) {
  constexpr size_t block = 4096;
  const size_t samples = channels.front().size();
  const size_t blocks = ((samples + 7) / 8 + block - 1) / block;
  const uint64_t data = uint64_t(blocks) * block * channels.size();
  std::vector<uint8_t> out;
  put(out, "DSD ");
  put(out, 28, 8, true);
  put(out, 92 + data, 8, true);
  put(out, 0, 8, true);
  put(out, "fmt ");
  put(out, 52, 8, true);
  for (uint64_t field : {uint64_t(1), uint64_t(0), uint64_t(channels.size()),
                         uint64_t(channels.size()), uint64_t(rate),
                         uint64_t(lsbFirst ? 1 : 8)}) {
    put(out, field, 4, true);
  }
  put(out, samples, 8, true);
  put(out, block, 4, true);
  put(out, 0, 4, true);
  put(out, "data");
  put(out, 12 + data, 8, true);
  std::vector<std::vector<uint8_t>> packed;
  for (const auto &bits : channels) {
    packed.push_back(packMsbFirst(bits));
    packed.back().resize(blocks * block, 0);
    if (lsbFirst) {
      std::ranges::transform(packed.back(), packed.back().begin(), reversed);
    }
  }
  for (size_t b = 0; b < blocks; ++b) {
    for (const auto &bytes : packed) {
      out.insert(out.end(), bytes.begin() + b * block,
                 bytes.begin() + (b + 1) * block);
    }
  }
  return out;
}

std::vector<uint8_t> dffFile(const std::vector<std::vector<uint8_t>> &channels,
                             unsigned rate) {
  const auto chunk = [](const char *tag, const std::vector<uint8_t> &body) {
    std::vector<uint8_t> out;
    put(out, tag);
    put(out, body.size(), 8, false);
    out.insert(out.end(), body.begin(), body.end());
    if (body.size() & 1) {
      out.push_back(0);
    }
    return out;
  };
  std::vector<uint8_t> fs, layout, compression, prop, audio, form;
  put(fs, rate, 4, false);
  put(layout, channels.size(), 2, false);
  if (channels.size() == 2) {
    put(layout, "SLFT");
    put(layout, "SRGT");
  } else {
    put(layout, "C   ");
  }
  put(compression, "DSD ");
  compression.push_back(14);
  const std::string name = "not compressed";
  compression.insert(compression.end(), name.begin(), name.end());
  put(prop, "SND ");
  for (const auto &part :
       {chunk("FS  ", fs), chunk("CHNL", layout), chunk("CMPR", compression)}) {
    prop.insert(prop.end(), part.begin(), part.end());
  }
  std::vector<std::vector<uint8_t>> packed;
  for (const auto &bits : channels) {
    packed.push_back(packMsbFirst(bits));
  }
  for (size_t i = 0; i < packed.front().size(); ++i) {
    for (const auto &bytes : packed) {
      audio.push_back(bytes[i]);
    }
  }
  std::vector<uint8_t> version;
  put(version, 0x01050000, 4, false);
  put(form, "DSD ");
  for (const auto &part : {chunk("FVER", version), chunk("PROP", prop),
                           chunk("DSD ", audio)}) {
    form.insert(form.end(), part.begin(), part.end());
  }
  return chunk("FRM8", form);
}

} // namespace harness
