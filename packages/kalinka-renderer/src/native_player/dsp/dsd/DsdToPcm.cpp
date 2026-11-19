#include "dsp/dsd/DsdToPcm.h"

#include "dsp/dsd/DsdFilterTaps.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>

namespace {
/// Bytes per channel converted at a time: 64 output frames' worth, held
/// between 512 and 2048 bytes, the sizes a Pi 4 ran fastest.
size_t blockFor(unsigned ratio) {
  return std::clamp<size_t>(8 * size_t(ratio), 512, 2048);
}
constexpr double kStage1Scale = 1.0 / double(1ull << dsd_filters::kStage1Shift);

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

const dsd_filters::Chain &chainFor(const DsdToPcm::Spec &spec) {
  const auto *chain = findChain(spec);
  if (!chain || !spec.channels)
    throw std::invalid_argument("No DSD to PCM conversion of " +
                                std::to_string(spec.channels) +
                                " channels from " +
                                std::to_string(spec.dsdRate) + " Hz to " +
                                std::to_string(spec.pcmRate) + " Hz");
  return *chain;
}

/// Output frames the chain's output lags its input by.
double delayOf(const dsd_filters::Chain &chain) {
  const unsigned ratio = chain.dsdMultiple / chain.pcmMultiple;
  // Stage 1's output m is complete only with bit 8m + 7.
  double delay =
      ((dsd_filters::kStage1[chain.stage1].size - 1) / 2.0 - 7) / ratio;
  unsigned stageRatio = 8;
  for (const int index : chain.halfBands) {
    if (index == dsd_filters::kNone)
      break;
    delay += double(HalfBandDecimator::delayFor(
                 dsd_filters::kHalfBand[index].size)) *
             stageRatio / ratio;
    stageRatio *= 2;
  }
  if (chain.band != dsd_filters::kNone)
    delay +=
        double(SymmetricFir::delayFor(dsd_filters::kBand[chain.band].size));
  return delay;
}

} // namespace

size_t DsdToPcm::latencyOf(const Spec &spec) {
  return size_t(std::lround(delayOf(chainFor(spec))));
}

bool DsdToPcm::converts(const Spec &spec) {
  return spec.channels > 0 && findChain(spec) != nullptr;
}

DsdToPcm::DsdToPcm(const Spec &spec) : channels(spec.channels) {
  const auto *chain = &chainFor(spec);
  ratio_ = chain->dsdMultiple / chain->pcmMultiple;
  passbandHz_ = chain->passbandHz;
  blockBytes = blockFor(ratio_);

  const auto &taps = dsd_filters::kStage1[chain->stage1];
  stage1 = makeStage1(spec.kernel, {taps.taps, taps.size, kStage1Scale,
                                    channels, spec.order, blockBytes});

  const FirPath path = spec.kernel == DsdKernel::Reference ? FirPath::Reference
                                                           : FirPath::Vector;
  state.resize(channels);
  blockTargets.resize(channels);
  stage1Targets.resize(channels);
  for (auto &channel : state) {
    for (const int index : chain->halfBands) {
      if (index == dsd_filters::kNone)
        break;
      const auto &side = dsd_filters::kHalfBand[index];
      channel.halfBands.emplace_back(side.taps, side.size, blockBytes, path);
    }
    if (chain->band != dsd_filters::kNone) {
      const auto &half = dsd_filters::kBand[chain->band];
      channel.band.emplace(half.taps, half.size, blockBytes, path);
    }
  }

  latency_ = size_t(std::lround(delayOf(*chain)));
  reset();
}

void DsdToPcm::reset() {
  stage1->reset();
  for (auto &channel : state) {
    for (auto &halfBand : channel.halfBands)
      halfBand.reset();
    if (channel.band)
      channel.band->reset();
  }
}

size_t DsdToPcm::process(const uint8_t *in, size_t bytes, double *const *out) {
  size_t written = 0;
  while (bytes) {
    const size_t block = std::min(bytes, blockBytes);
    for (unsigned c = 0; c < channels; ++c) {
      blockTargets[c] = out[c] + written;
      stage1Targets[c] = feed(state[c], 0, blockTargets[c]);
    }
    stage1->process(in, block, stage1Targets.data());
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
