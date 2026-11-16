#include "DsdToPcm.h"

#include "Analysis.h"
#include "DsdFilterTaps.h"
#include "SigmaDelta.h"
#include "Signals.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <bit>
#include <cmath>
#include <complex>
#include <numbers>
#include <random>
#include <span>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>

namespace {
constexpr uint8_t kIdle = 0x69;

DsdToPcm::Spec spec(unsigned base, unsigned dsd, unsigned pcm, DsdFilter filter,
                    unsigned channels = 1,
                    DsdBitOrder order = DsdBitOrder::MsbFirst) {
  return {base * dsd, base * pcm, channels, filter, order};
}

std::string name(const DsdToPcm::Spec &s) {
  static const char *filters[] = {"30k", "50k", "wide"};
  return std::to_string(s.dsdRate) + " Hz to " + std::to_string(s.pcmRate) +
         " Hz, " + filters[int(s.filter)];
}

std::vector<double> convert(const DsdToPcm::Spec &s,
                            const std::vector<uint8_t> &bytes) {
  DsdToPcm converter(s);
  std::vector<double> out(converter.maxFrames(bytes.size()));
  double *channels[] = {out.data()};
  out.resize(converter.process(bytes.data(), bytes.size(), channels));
  return out;
}

std::vector<uint8_t> randomBytes(size_t count, unsigned seed) {
  std::mt19937 random(seed);
  std::vector<uint8_t> bytes(count);
  for (auto &byte : bytes)
    byte = uint8_t(random());
  return bytes;
}

uint8_t reversed(uint8_t byte) {
  uint8_t out = 0;
  for (int i = 0; i < 8; ++i)
    out |= ((byte >> i) & 1) << (7 - i);
  return out;
}

/**
 * The chain's whole response at the one-bit rate, from flipping one bit of the
 * idle pattern at each phase: the converter is linear in the ±1 levels, and
 * idle converts to zero.
 */
std::vector<double> impulseResponse(const DsdToPcm::Spec &s) {
  const DsdToPcm probe(s);
  const unsigned ratio = probe.ratio();
  const size_t frames = 2 * probe.latency() + 16;
  const size_t bytes = frames * ratio / 8;
  std::vector<double> response(frames * ratio);
  for (unsigned phase = 0; phase < ratio; ++phase) {
    std::vector<uint8_t> in(bytes, kIdle);
    const unsigned bit = 7 - phase % 8;
    const double delta = (in[phase / 8] >> bit) & 1 ? -2.0 : 2.0;
    in[phase / 8] ^= uint8_t(1u << bit);
    const auto out = convert(s, in);
    for (size_t n = 0; n < out.size() && n < frames; ++n)
      response[n * ratio + (ratio - 1 - phase)] = out[n] / delta;
  }
  return response;
}

std::vector<DsdToPcm::Spec> everyChain() {
  std::vector<DsdToPcm::Spec> all;
  for (const auto &chain : dsd_filters::kChains)
    all.push_back(spec(chain.base, chain.dsdMultiple, chain.pcmMultiple,
                       DsdFilter(chain.filter)));
  return all;
}
} // namespace

TEST(DsdToPcm, TheIdlePatternConvertsToExactZero) {
  for (const auto &s : everyChain()) {
    const auto out = convert(s, std::vector<uint8_t>(8192, kIdle));
    ASSERT_FALSE(out.empty()) << name(s);
    EXPECT_TRUE(std::ranges::all_of(out, [](double v) { return v == 0.0; }))
        << name(s);
  }
}

TEST(DsdToPcm, SilenceAfterMusicSettlesToExactZero) {
  const auto s = spec(44100, 64, 4, DsdFilter::Cut30k);
  auto in = randomBytes(4096, 1);
  in.resize(in.size() + 8192, kIdle);
  const auto out = convert(s, in);
  EXPECT_TRUE(std::all_of(out.end() - 64, out.end(),
                          [](double v) { return v == 0.0; }));
}

TEST(DsdToPcm, EveryChainMeetsItsResponse) {
  for (const auto &s : everyChain()) {
    const DsdToPcm converter(s);
    const auto h = impulseResponse(s);
    size_t size = 1 << 16;
    while (size < 8 * h.size())
      size *= 2;
    const auto response = harness::response(h, s.dsdRate, size);
    const double fout = s.pcmRate;
    double reference = 0, ripple = 0, fold = -400;
    for (size_t k = 0; k < response.frequency.size(); ++k) {
      const double f = response.frequency[k], db = response.magnitudeDb[k];
      if (std::abs(f - 1000) < s.dsdRate / double(size))
        reference = db;
      if (f <= 20000)
        ripple = std::max(ripple, std::abs(db));
      const double folded = std::fmod(f, fout);
      if (f > fout / 2 && (folded <= 20000 || folded >= fout - 20000))
        fold = std::max(fold, db);
    }
    EXPECT_LT(std::abs(reference), 0.001) << name(s) << ": level at 1 kHz";
    EXPECT_LT(ripple, 0.005) << name(s) << ": ripple to 20 kHz";
    EXPECT_LT(fold, -130.0) << name(s) << ": what folds below 20 kHz";
  }
}

TEST(DsdToPcm, BothBitOrdersGiveTheSameOutput) {
  const auto msb = randomBytes(20000, 2);
  std::vector<uint8_t> lsb(msb.size());
  std::ranges::transform(msb, lsb.begin(), reversed);
  for (const auto filter : {DsdFilter::Cut30k, DsdFilter::Wide}) {
    auto s = spec(44100, 64, 4, filter);
    const auto a = convert(s, msb);
    s.order = DsdBitOrder::LsbFirst;
    EXPECT_EQ(convert(s, lsb), a);
  }
}

TEST(DsdToPcm, HowTheInputIsSplitDoesNotChangeTheOutput) {
  const auto s = spec(44100, 128, 4, DsdFilter::Cut50k);
  const auto in = randomBytes(30000, 3);
  const auto whole = convert(s, in);

  DsdToPcm converter(s);
  std::vector<double> pieces(converter.maxFrames(in.size()));
  std::mt19937 random(4);
  size_t done = 0, frames = 0;
  while (done < in.size()) {
    const size_t bytes =
        std::min<size_t>(1 + random() % 1500, in.size() - done);
    double *out[] = {pieces.data() + frames};
    const size_t made = converter.process(in.data() + done, bytes, out);
    EXPECT_LE(made, converter.maxFrames(bytes));
    frames += made;
    done += bytes;
  }
  pieces.resize(frames);
  EXPECT_EQ(pieces, whole);
}

TEST(DsdToPcm, ChannelsAreConvertedApart) {
  const auto left = randomBytes(10000, 5);
  std::vector<uint8_t> stereo;
  for (const auto byte : left) {
    stereo.push_back(byte);
    stereo.push_back(kIdle);
  }
  const auto s = spec(48000, 64, 4, DsdFilter::Cut30k, 2);
  DsdToPcm converter(s);
  std::vector<double> l(converter.maxFrames(left.size())), r(l.size());
  double *out[] = {l.data(), r.data()};
  const size_t frames = converter.process(stereo.data(), left.size(), out);
  l.resize(frames);
  r.resize(frames);
  EXPECT_EQ(l, convert(spec(48000, 64, 4, DsdFilter::Cut30k), left));
  EXPECT_TRUE(std::ranges::all_of(r, [](double v) { return v == 0.0; }));
}

TEST(DsdToPcm, ResetForgetsWhatCameBefore) {
  const auto s = spec(44100, 64, 4, DsdFilter::Cut30k);
  const auto in = randomBytes(5000, 6);
  DsdToPcm converter(s);
  std::vector<double> scratch(converter.maxFrames(in.size()));
  double *out[] = {scratch.data()};
  converter.process(in.data(), in.size(), out);
  converter.reset();
  scratch.resize(converter.process(in.data(), in.size(), out));
  EXPECT_EQ(scratch, convert(s, in));
}

TEST(DsdToPcm, UnsupportedRatesAndChannelsAreRefused) {
  EXPECT_THROW(DsdToPcm(spec(44100, 64, 1, DsdFilter::Cut30k)),
               std::invalid_argument)
      << "44.1 kHz is not one of 2, 4 or 8 times the base";
  EXPECT_THROW(DsdToPcm(spec(44100, 64, 16, DsdFilter::Cut30k)),
               std::invalid_argument)
      << "a ratio under 8";
  EXPECT_THROW(
      DsdToPcm({2822400, 192000, 1, DsdFilter::Cut30k, DsdBitOrder::MsbFirst}),
      std::invalid_argument)
      << "families mixed";
  EXPECT_THROW(
      DsdToPcm({2822401, 176400, 1, DsdFilter::Cut30k, DsdBitOrder::MsbFirst}),
      std::invalid_argument);
  EXPECT_THROW(DsdToPcm(spec(44100, 64, 4, DsdFilter::Cut30k, 0)),
               std::invalid_argument);
}

namespace {
/**
 * @p signal with nothing left above @p cutoff, cut over the whole record. DSD's
 * ultrasonic noise is near full scale, and any left in leaks into a THD+N
 * measured near -170 dB.
 */
std::vector<double> bandLimited(std::span<const double> signal, double rate,
                                double cutoff) {
  const size_t size = std::bit_ceil(signal.size());
  std::vector<std::complex<double>> data(size);
  std::ranges::copy(signal, data.begin());
  // Tapered at both ends, which the measurement leaves out: an abrupt end
  // rings through the cut.
  const size_t taper = signal.size() / 25;
  for (size_t n = 0; n < taper; ++n) {
    const double w = 0.5 - 0.5 * std::cos(std::numbers::pi * double(n) / taper);
    data[n] *= w;
    data[signal.size() - 1 - n] *= w;
  }
  data = harness::fft(std::move(data));
  for (size_t k = 0; k < size; ++k) {
    if (double(std::min(k, size - k)) * rate / double(size) > cutoff)
      data[k] = 0;
    data[k] = std::conj(data[k]);
  }
  data = harness::fft(std::move(data));
  std::vector<double> out(signal.size());
  for (size_t n = 0; n < out.size(); ++n)
    out[n] = data[n].real() / double(size);
  return out;
}

/// A 1 kHz tone at half modulation through the harness modulator, and the
/// converter's output of it, each band-limited and over the same stretch.
struct Tone {
  harness::Distortion fixture, converted;
};

Tone tone(unsigned multiple, unsigned pcm, DsdFilter filter) {
  constexpr double kCut = 22000;
  const unsigned rate = 44100 * multiple;
  harness::SigmaDelta modulator(7, multiple);
  const auto bits =
      modulator.modulate(harness::sine(1000, rate, rate / 4, 0.5));
  const auto s = spec(44100, multiple, pcm, filter);
  const DsdToPcm probe(s);
  const auto out = convert(s, harness::packMsbFirst(bits));

  std::vector<double> levels(bits.size());
  std::ranges::transform(bits, levels.begin(),
                         [](uint8_t bit) { return bit ? 1.0 : -1.0; });
  const auto fixture = bandLimited(levels, rate, kCut);
  const auto converted = bandLimited(out, s.pcmRate, kCut);

  // Clear of the start, the end and the cut's wrap-around at both.
  const size_t from = out.size() / 20, frames = out.size() - 2 * from;
  std::vector<double> a(frames), b(frames);
  for (size_t n = 0; n < frames; ++n) {
    a[n] = fixture[(from + n) * probe.ratio()];
    b[n] = converted[from + n + probe.latency()];
  }
  return {harness::thdPlusN(a, s.pcmRate, 1000),
          harness::thdPlusN(b, s.pcmRate, 1000)};
}
} // namespace

TEST(DsdToPcm, AToneKeepsTheFixturesLevelAndDistortion) {
  for (const auto &[multiple, pcm, filter] :
       {std::tuple{64u, 4u, DsdFilter::Cut30k},
        std::tuple{128u, 4u, DsdFilter::Cut50k}}) {
    const auto result = tone(multiple, pcm, filter);
    EXPECT_NEAR(result.converted.fundamentalDbfs,
                result.fixture.fundamentalDbfs, 0.001)
        << "DSD" << multiple;
    EXPECT_NEAR(result.converted.thdnDb, result.fixture.thdnDb, 0.1)
        << "DSD" << multiple;
  }
}
