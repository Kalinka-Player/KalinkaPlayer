#include "FirFilterBank.h"

#include <pffft_double.h>
#include <soxr.h>
#include <spdlog/spdlog.h>

#include <algorithm>
#include <bit>
#include <cmath>
#include <numeric>

namespace {
// Keep the pre-ringing of the linear-phase SRC before tap zero. A shorter
// first convolution partition absorbs these frames into the existing FIR lag.
constexpr size_t guard = FirRoomCorrection::kConversionLookahead;
constexpr size_t maxConvertedTaps = FirRoomCorrection::kMaxConvertedTaps;

std::vector<double> convert(std::span<const double> taps, unsigned from,
                            unsigned to) {
  while (taps.size() > 1 && taps.back() == 0)
    taps = taps.first(taps.size() - 1);
  const double ratio = double(to) / from;
  const double length = std::ceil(taps.size() * ratio) + 2 * guard;
  if (length > maxConvertedTaps)
    throw std::length_error(
        "Converted impulse exceeds the tap limit; supply a WAV at this rate");
  // Pad on an exact rational-rate boundary so both channels retain their
  // timing for fractional conversions (e.g. 44.1 to 48 kHz).
  const unsigned gcd = std::gcd(from, to);
  const size_t periods = (guard + to / gcd - 1) / (to / gcd);
  const size_t prefix = periods * (from / gcd);
  const size_t skip = periods * (to / gcd) - guard;
  if (prefix > maxConvertedTaps || skip > maxConvertedTaps)
    throw std::length_error("Unsupported FIR sample-rate ratio");
  std::vector<double> input(prefix + taps.size() + prefix);
  std::ranges::copy(taps, input.begin() + prefix);
  std::vector<double> output(skip + size_t(length) + 1);
  auto io = soxr_io_spec(SOXR_FLOAT64_I, SOXR_FLOAT64_I);
  // An impulse is a transfer function: preserve its sum across rate changes.
  io.scale = 1 / ratio;
  const auto quality = soxr_quality_spec(SOXR_VHQ, 0);
  const auto runtime = soxr_runtime_spec(1);
  size_t written = 0;
  const auto error = soxr_oneshot(from, to, 1, input.data(), input.size(),
                                  nullptr, output.data(), output.size(),
                                  &written, &io, &quality, &runtime);
  if (error || written <= skip)
    throw std::runtime_error(error ? error : "Empty converted FIR impulse");
  output.resize(std::min(written, skip + size_t(length)));
  output.erase(output.begin(), output.begin() + skip);
  return output;
}

std::vector<float> magnitude(std::span<const double> taps, unsigned rate) {
  const size_t n = std::bit_ceil(std::max<size_t>(8192, taps.size()));
  std::unique_ptr<PFFFTD_Setup, decltype(&pffftd_destroy_setup)> fft(
      pffftd_new_setup(int(n), PFFFT_REAL), pffftd_destroy_setup);
  auto buffer = [n] {
    return std::unique_ptr<double[], decltype(&pffftd_aligned_free)>(
        static_cast<double *>(pffftd_aligned_malloc(n * sizeof(double))),
        pffftd_aligned_free);
  };
  auto bins = buffer(), work = buffer();
  if (!fft || !bins || !work)
    throw std::bad_alloc();
  std::fill_n(bins.get(), n, 0.);
  std::ranges::copy(taps, bins.get());
  pffftd_transform_ordered(fft.get(), bins.get(), bins.get(), work.get(),
                           PFFFT_FORWARD);
  const auto amplitude = [&](size_t k) {
    if (k == 0 || k == n / 2)
      return std::abs(bins[k == 0 ? 0 : 1]);
    return std::hypot(bins[2 * k], bins[2 * k + 1]);
  };
  std::vector<float> result(257);
  const double top = std::min(20000., rate / 2.);
  for (size_t i = 0; i < result.size(); ++i) {
    const double hz = 20 * std::pow(top / 20, double(i) / (result.size() - 1));
    const double bin = std::clamp(hz * n / rate, 0., double(n / 2));
    const size_t k = size_t(bin);
    const double mag =
        std::lerp(amplitude(k), amplitude(std::min(k + 1, n / 2)), bin - k);
    result[i] = float(20 * std::log10(std::clamp(mag, 1e-6, 1e6)));
  }
  return result;
}
} // namespace

void FirFilterBank::add(FirImpulse impulse) {
  std::lock_guard lock(mutex);
  if (!impulse.rate || impulse.channels[0].empty() ||
      !originals.try_emplace(impulse.rate, std::move(impulse)).second)
    throw std::invalid_argument("Invalid or duplicate FIR impulse rate");
  recent.clear();
}

std::vector<unsigned> FirFilterBank::suppliedRates() const {
  std::lock_guard lock(mutex);
  std::vector<unsigned> rates;
  for (const auto &[rate, impulse] : originals)
    rates.push_back(rate);
  return rates;
}

std::shared_ptr<const FirVariant> FirFilterBank::at(unsigned rate) const {
  std::lock_guard lock(mutex);
  if (!rate || originals.empty())
    return {};
  auto hit =
      std::ranges::find(recent, rate, &decltype(recent)::value_type::first);
  if (hit != recent.end()) {
    recent.splice(recent.begin(), recent, hit);
    return recent.front().second;
  }
  // Nearest in octaves. Prefer the higher-rate original at an exact tie.
  auto best = originals.lower_bound(rate);
  if (best == originals.end() ||
      (best != originals.begin() &&
       uint64_t(rate) * rate < uint64_t(best->first) * std::prev(best)->first))
    --best;
  const auto &[from, impulse] = *best;
  std::shared_ptr<FirVariant> variant;
  try {
    std::array<std::vector<double>, 2> converted;
    if (from != rate)
      for (size_t c = 0; c < 2; ++c)
        if (!impulse.channels[c].empty())
          converted[c] = convert(impulse.channels[c], from, rate);
    const auto &taps = from == rate ? impulse.channels : converted;
    const auto &left = taps[0], &right = taps[1].empty() ? left : taps[1];
    auto filter = from == rate
                      ? std::make_shared<FirRoomCorrection>(rate, left, right)
                      : FirRoomCorrection::converted(rate, left, right);
    variant =
        std::make_shared<FirVariant>(FirVariant{from, std::move(filter), {}});
    variant->responseDb[0] = magnitude(left, rate);
    if (&right != &left && right != left)
      variant->responseDb[1] = magnitude(right, rate);
  } catch (const std::exception &error) {
    // A preview allocation can fail after the audio filter is ready.
    if (variant)
      variant->responseDb = {};
    spdlog::warn("FIR {} at {} Hz: {}", name, rate, error.what());
  }
  recent.emplace_front(rate, variant);
  if (recent.size() > 3)
    recent.pop_back();
  return variant;
}
