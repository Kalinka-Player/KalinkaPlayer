#include "DsdToPcm.h"

#include "DsdFilterTaps.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <stdexcept>
#include <string>

namespace {
constexpr size_t kBlockBytes = 512;

constexpr std::array<uint8_t, 256> kReversed = [] {
  std::array<uint8_t, 256> table{};
  for (unsigned byte = 0; byte < 256; ++byte)
    for (unsigned bit = 0; bit < 8; ++bit)
      table[byte] |= uint8_t(((byte >> bit) & 1) << (7 - bit));
  return table;
}();

const dsd_filters::Chain *findChain(const DsdToPcm::Spec &spec) {
  for (const unsigned base : {44100u, 48000u}) {
    if (spec.dsdRate % base || spec.pcmRate % base)
      continue;
    const unsigned dsd = spec.dsdRate / base, pcm = spec.pcmRate / base;
    for (const auto &chain : dsd_filters::kChains) {
      if (chain.base == base && chain.dsdMultiple == dsd &&
          chain.pcmMultiple == pcm &&
          chain.filter == static_cast<int>(spec.filter))
        return &chain;
    }
  }
  return nullptr;
}

size_t halfBandLength(size_t sideTaps) { return 4 * sideTaps - 1; }
size_t bandLength(size_t halfTaps) { return 2 * halfTaps - 1; }
} // namespace

DsdToPcm::DsdToPcm(const Spec &spec)
    : lsbFirst(spec.order == DsdBitOrder::LsbFirst),
      // The idle pattern 0x69 in time order, whichever bit comes first.
      idle(lsbFirst ? kReversed[0x69] : 0x69), channels(spec.channels) {
  const auto *chain = findChain(spec);
  if (!chain || !channels)
    throw std::invalid_argument("No DSD to PCM conversion of " +
                                std::to_string(channels) + " channels from " +
                                std::to_string(spec.dsdRate) + " Hz to " +
                                std::to_string(spec.pcmRate) + " Hz");
  ratio_ = chain->dsdMultiple / chain->pcmMultiple;
  passbandHz_ = chain->passbandHz;

  const auto &stage = dsd_filters::kStage1[chain->stage1];
  groups = stage.size / 8;
  // The taps are symmetric, so group g's mirror reads group g's table with
  // the byte reversed; only the first half is stored.
  tables.resize((groups + 1) / 2);
  for (size_t g = 0; g < tables.size(); ++g) {
    for (unsigned byte = 0; byte < 256; ++byte) {
      int64_t sum = 0;
      // MSB first: bit i holds the sample tap 8g + i weighs.
      for (unsigned i = 0; i < 8; ++i)
        sum += (byte >> i) & 1 ? stage.taps[8 * g + i] : -stage.taps[8 * g + i];
      tables[g][byte] = double(sum);
    }
  }

  double delay = (stage.size - 1) / 2.0 / ratio_;
  unsigned stageRatio = 8;
  for (const int index : chain->halfBands) {
    if (index == dsd_filters::kNone)
      break;
    const auto &taps = dsd_filters::kHalfBand[index];
    halfBands.push_back({taps.taps, taps.size});
    delay += (halfBandLength(taps.size) - 1) / 2.0 * stageRatio / ratio_;
    stageRatio *= 2;
  }
  if (chain->band != dsd_filters::kNone) {
    const auto &taps = dsd_filters::kBand[chain->band];
    band = {taps.taps, taps.size};
    delay += (bandLength(taps.size) - 1) / 2.0;
  }
  latency_ = size_t(std::lround(delay));

  pending.assign(halfBands.size(), 0);
  blockInputs.assign(halfBands.size(), 0);
  state.resize(channels);
  for (auto &channel : state) {
    channel.bytes.resize(groups - 1 + kBlockBytes);
    for (const auto &fir : halfBands)
      channel.stages.emplace_back(halfBandLength(fir.size) - 1 + kBlockBytes);
    channel.band.resize((band.size ? bandLength(band.size) - 1 : 0) +
                        kBlockBytes);
  }
  reset();
}

void DsdToPcm::reset() {
  std::ranges::fill(pending, 0u);
  for (auto &channel : state) {
    std::ranges::fill(channel.bytes, idle);
    for (auto &buffer : channel.stages)
      std::ranges::fill(buffer, 0.0);
    std::ranges::fill(channel.band, 0.0);
  }
}

size_t DsdToPcm::process(const uint8_t *in, size_t bytes, double *const *out) {
  size_t written = 0;
  while (bytes) {
    const size_t block = std::min(bytes, kBlockBytes);
    size_t count = 0;
    for (unsigned c = 0; c < channels; ++c) {
      auto &channel = state[c];
      count = stage1(in + c, block, channel);
      for (size_t k = 0; k < halfBands.size(); ++k) {
        blockInputs[k] = count;
        count = halfBand(k, count, channel);
      }
      bandFilter(count, channel, out[c] + written);
    }
    // Every channel decimated at the same phase; it moves on only now.
    for (size_t k = 0; k < halfBands.size(); ++k)
      pending[k] = (pending[k] + blockInputs[k]) % 2;
    written += count;
    in += block * channels;
    bytes -= block;
  }
  return written;
}

size_t DsdToPcm::stage1(const uint8_t *in, size_t bytes,
                        Channel &channel) const {
  constexpr double kScale = 1.0 / double(1ull << dsd_filters::kStage1Shift);
  auto &history = channel.bytes;
  for (size_t m = 0; m < bytes; ++m)
    history[groups - 1 + m] = in[m * channels];
  auto &target = halfBands.empty() ? channel.band : channel.stages.front();
  const size_t kept = target.size() - kBlockBytes;
  const size_t stored = tables.size();
  for (size_t m = 0; m < bytes; ++m) {
    const uint8_t *newest = history.data() + groups - 1 + m;
    // Whole numbers under 2^53: the sum is exact, whatever its order.
    double sum = 0;
    for (size_t g = 0; g < stored; ++g) {
      const uint8_t byte = newest[-ptrdiff_t(g)];
      sum += tables[g][lsbFirst ? kReversed[byte] : byte];
    }
    for (size_t g = stored; g < groups; ++g) {
      const uint8_t byte = newest[-ptrdiff_t(g)];
      sum += tables[groups - 1 - g][lsbFirst ? byte : kReversed[byte]];
    }
    target[kept + m] = sum * kScale;
  }
  std::copy(history.begin() + bytes, history.begin() + bytes + groups - 1,
            history.begin());
  return bytes;
}

size_t DsdToPcm::halfBand(size_t stage, size_t count, Channel &channel) const {
  const auto &fir = halfBands[stage];
  auto &input = channel.stages[stage];
  auto &target =
      stage + 1 < halfBands.size() ? channel.stages[stage + 1] : channel.band;
  const size_t history = input.size() - kBlockBytes;
  const size_t targetHistory = target.size() - kBlockBytes;
  const size_t centre = (halfBandLength(fir.size) - 1) / 2;
  size_t produced = 0;
  // An output follows every second input; one may already be waiting.
  for (size_t t = 1 - pending[stage]; t < count; t += 2) {
    const double *x = input.data() + history + t - centre;
    double sum = 0.5 * x[0];
    for (size_t i = 0; i < fir.size; ++i)
      sum += fir.taps[i] * (x[-ptrdiff_t(2 * i + 1)] + x[2 * i + 1]);
    target[targetHistory + produced++] = sum;
  }
  std::copy(input.begin() + count, input.begin() + count + history,
            input.begin());
  return produced;
}

void DsdToPcm::bandFilter(size_t count, Channel &channel, double *out) const {
  const size_t history = channel.band.size() - kBlockBytes;
  const double *x = channel.band.data() + history;
  if (!band.size) {
    std::copy(x, x + count, out);
    return;
  }
  const size_t centre = band.size - 1;
  for (size_t t = 0; t < count; ++t) {
    const double *newest = x + t;
    double sum = band.taps[centre] * newest[-ptrdiff_t(centre)];
    for (size_t j = 0; j < centre; ++j)
      sum += band.taps[j] *
             (newest[-ptrdiff_t(j)] + newest[-ptrdiff_t(2 * centre - j)]);
    out[t] = sum;
  }
  std::copy(channel.band.begin() + count,
            channel.band.begin() + count + history, channel.band.begin());
}
