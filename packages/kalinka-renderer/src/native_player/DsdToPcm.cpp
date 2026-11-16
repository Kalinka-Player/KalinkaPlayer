#include "DsdToPcm.h"

#include "DsdFilterTaps.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <stdexcept>
#include <string>

namespace {
constexpr size_t kBlockBytes = 512;
constexpr double kStage1Scale = 1.0 / double(1ull << dsd_filters::kStage1Shift);
/// Whole tables up to here stay inside the 32 KB stage 1 budget.
constexpr size_t kWholeGroups = 16;

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

} // namespace

DsdToPcm::DsdToPcm(const Spec &spec)
    // Channel pairs measured fastest on x86-64 and the Cortex-A76; on the A72
    // nibble lookups lead below DSD256 (benchmarks/dsp/results.md).
    : kernel(spec.kernel == DsdKernel::Fast ? DsdKernel::ChannelPair
                                            : spec.kernel),
      lsbFirst(spec.order == DsdBitOrder::LsbFirst),
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
  // Past the budget symmetry lets a mirrored group read its twin's table.
  whole = groups <= kWholeGroups;
  if (kernel != DsdKernel::Nibbles) {
    const auto tapOf = [&](size_t g, unsigned bit) {
      return stage.taps[8 * g + (whole && lsbFirst ? 7 - bit : bit)];
    };
    tables.resize(whole ? groups : (groups + 1) / 2);
    for (size_t g = 0; g < tables.size(); ++g) {
      for (unsigned byte = 0; byte < 256; ++byte) {
        int64_t sum = 0;
        for (unsigned bit = 0; bit < 8; ++bit)
          sum += (byte >> bit) & 1 ? tapOf(g, bit) : -tapOf(g, bit);
        tables[g][byte] = double(sum);
      }
    }
  } else {
    // Every group is stored, always in the stream's order, whole or not.
    const auto nibbleTap = [&](size_t g, unsigned bit) {
      return stage.taps[8 * g + (lsbFirst ? 7 - bit : bit)];
    };
    nibbles.resize(groups);
    for (size_t g = 0; g < groups; ++g) {
      for (unsigned half = 0; half < 2; ++half) {
        auto &planes = half ? nibbles[g].high : nibbles[g].low;
        for (unsigned n = 0; n < 16; ++n) {
          int64_t sum = 0;
          for (unsigned i = 0; i < 4; ++i) {
            const auto tap = nibbleTap(g, 4 * half + i);
            sum += (n >> i) & 1 ? tap : -tap;
          }
          const auto biased = uint64_t(sum + int64_t(dsd_kernels::kNibbleBias));
          for (size_t p = 0; p < dsd_kernels::kNibblePlanes; ++p)
            planes[p][n] = uint8_t(biased >> (8 * p));
        }
      }
    }
  }

  const FirPath path =
      kernel == DsdKernel::Reference ? FirPath::Reference : FirPath::Vector;
  state.resize(channels);
  blockTargets.resize(channels);
  for (auto &channel : state) {
    channel.bytes.resize(groups - 1 + kBlockBytes + dsd_kernels::kByteSlack);
    channel.reversed.resize(channel.bytes.size());
    for (const int index : chain->halfBands) {
      if (index == dsd_filters::kNone)
        break;
      const auto &taps = dsd_filters::kHalfBand[index];
      channel.halfBands.emplace_back(taps.taps, taps.size, kBlockBytes, path);
    }
    if (chain->band != dsd_filters::kNone) {
      const auto &taps = dsd_filters::kBand[chain->band];
      channel.band.emplace(taps.taps, taps.size, kBlockBytes, path);
    }
  }

  const auto &first = state.front();
  // Stage 1's output m is complete only with bit 8m + 7.
  double delay = ((stage.size - 1) / 2.0 - 7) / ratio_;
  unsigned stageRatio = 8;
  for (const auto &halfBand : first.halfBands) {
    delay += double(halfBand.delay()) * stageRatio / ratio_;
    stageRatio *= 2;
  }
  if (first.band)
    delay += double(first.band->delay());
  latency_ = size_t(std::lround(delay));
  reset();
}

void DsdToPcm::reset() {
  for (auto &channel : state) {
    std::ranges::fill(channel.bytes, idle);
    std::ranges::fill(channel.reversed, kReversed[idle]);
    for (auto &halfBand : channel.halfBands)
      halfBand.reset();
    if (channel.band)
      channel.band->reset();
  }
}

size_t DsdToPcm::process(const uint8_t *in, size_t bytes, double *const *out) {
  size_t written = 0;
  while (bytes) {
    const size_t block = std::min(bytes, kBlockBytes);
    for (unsigned c = 0; c < channels; ++c)
      blockTargets[c] = out[c] + written;
    stage1(in, block, blockTargets.data());
    size_t count = 0;
    for (unsigned c = 0; c < channels; ++c) {
      auto &channel = state[c];
      count = block;
      for (size_t k = 0; k < channel.halfBands.size(); ++k)
        count = channel.halfBands[k].decimate(
            count, feed(channel, k + 1, blockTargets[c]));
      if (channel.band)
        channel.band->filter(count, blockTargets[c]);
    }
    written += count;
    in += block * channels;
    bytes -= block;
  }
  return written;
}

double *DsdToPcm::feed(Channel &channel, size_t stage, double *out) {
  if (stage < channel.halfBands.size())
    return channel.halfBands[stage].input();
  return channel.band ? channel.band->input() : out;
}

void DsdToPcm::stage1(const uint8_t *in, size_t bytes, double *const *out) {
  // Only half tables, read by the table kernels, need the bytes reversed.
  const bool reversing =
      !whole && (kernel == DsdKernel::Sums || kernel == DsdKernel::ChannelPair);
  for (unsigned c = 0; c < channels; ++c) {
    auto &channel = state[c];
    for (size_t m = 0; m < bytes; ++m)
      channel.bytes[groups - 1 + m] = in[m * channels + c];
    if (reversing)
      for (size_t m = 0; m < bytes; ++m)
        channel.reversed[groups - 1 + m] =
            kReversed[channel.bytes[groups - 1 + m]];
  }
  // Half tables are MSB first, and a mirrored group reads its byte reversed.
  const auto direct = [&](const Channel &channel) {
    return (!whole && lsbFirst ? channel.reversed : channel.bytes).data();
  };
  const auto mirror = [&](const Channel &channel) {
    return (lsbFirst ? channel.bytes : channel.reversed).data();
  };
  const auto target = [&](unsigned c) { return feed(state[c], 0, out[c]); };
  const size_t stored = tables.size();
  unsigned c = 0;
  if (kernel == DsdKernel::ChannelPair) {
    for (; c + 1 < channels; c += 2) {
      const auto &a = state[c], &b = state[c + 1];
      const uint8_t *const directs[] = {direct(a), direct(b)};
      const uint8_t *const mirrors[] = {mirror(a), mirror(b)};
      double *const outs[] = {target(c), target(c + 1)};
      dsd_kernels::stage1Pair(tables.data(), stored, groups, directs, mirrors,
                              bytes, kStage1Scale, outs);
    }
  }
  for (; c < channels; ++c) {
    const auto &channel = state[c];
    if (kernel == DsdKernel::Reference)
      stage1Reference(bytes, channel, target(c));
    else if (kernel == DsdKernel::Nibbles)
      dsd_kernels::stage1Nibbles(nibbles.data(), groups, channel.bytes.data(),
                                 bytes, kStage1Scale, target(c));
    else
      dsd_kernels::stage1(tables.data(), stored, groups, direct(channel),
                          mirror(channel), bytes, kStage1Scale, target(c));
  }
  for (auto &channel : state) {
    std::copy(channel.bytes.begin() + bytes,
              channel.bytes.begin() + bytes + groups - 1,
              channel.bytes.begin());
    if (reversing)
      std::copy(channel.reversed.begin() + bytes,
                channel.reversed.begin() + bytes + groups - 1,
                channel.reversed.begin());
  }
}

void DsdToPcm::stage1Reference(size_t bytes, const Channel &channel,
                               double *target) const {
  const size_t stored = tables.size();
  for (size_t m = 0; m < bytes; ++m) {
    const uint8_t *newest = channel.bytes.data() + groups - 1 + m;
    double sum = 0;
    for (size_t g = 0; g < stored; ++g) {
      const uint8_t byte = newest[-ptrdiff_t(g)];
      sum += tables[g][!whole && lsbFirst ? kReversed[byte] : byte];
    }
    for (size_t g = stored; g < groups; ++g) {
      const uint8_t byte = newest[-ptrdiff_t(g)];
      sum += tables[groups - 1 - g][lsbFirst ? byte : kReversed[byte]];
    }
    target[m] = sum * kStage1Scale;
  }
}
