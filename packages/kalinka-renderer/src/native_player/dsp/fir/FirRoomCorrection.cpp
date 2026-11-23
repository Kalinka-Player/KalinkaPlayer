#include "dsp/fir/FirRoomCorrection.h"

#include "dsp/FlushFilterTails.h"
#include "dsp/output/PlanarPcm.h"
#include <pffft_double.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <format>
#include <optional>
#include <stdexcept>

namespace {
using Buffer = std::unique_ptr<double[], decltype(&pffftd_aligned_free)>;

Buffer buffer(size_t size) {
  Buffer allocation(
      static_cast<double *>(pffftd_aligned_malloc(size * sizeof(double))),
      pffftd_aligned_free);
  if (!allocation)
    throw std::bad_alloc();
  return allocation;
}

// Channels use each transform serially and share its plan and scratch.
struct Transform {
  explicit Transform(size_t size)
      : setup(pffftd_new_setup(size, PFFFT_REAL), pffftd_destroy_setup),
        work(buffer(size)), sum(buffer(size)) {
    if (!setup)
      throw std::runtime_error("Cannot prepare FIR transform");
  }
  void run(const double *input, double *output, pffft_direction_t direction) {
    pffftd_transform(setup.get(), input, output, work.get(), direction);
  }
  std::unique_ptr<PFFFTD_Setup, decltype(&pffftd_destroy_setup)> setup;
  Buffer work, sum;
};
using Plan = std::shared_ptr<Transform>;

// 64 complex bins occupy 1 KiB and stay in L1 across the partition sum.
constexpr size_t kTileValues = 128;
// Store four real values followed by four imaginary values for vector loads.
constexpr size_t kGroupBins = 4;
constexpr size_t kMaxHop = 16384;
constexpr size_t kPairedHop = 4096;
// The native PFFFT layout was faster on x86-64 at the same partition size.
#if defined(__aarch64__)
constexpr bool kTileWideSpectra = true;
#else
constexpr bool kTileWideSpectra = false;
#endif
static_assert((2 * kMaxHop) % kTileValues == 0 &&
              kTileValues % (2 * kGroupBins) == 0);

struct Filter {
  Filter(size_t hop, std::span<const double> impulse, Plan plan, bool uniform)
      : hop(hop), parts((impulse.size() + hop - 1) / hop), fft(std::move(plan)),
        wideUniform(uniform && hop == kMaxHop && parts >= 4),
        pairedUniform(uniform && hop == kPairedHop && parts >= 32),
        bins(buffer(parts * 2 * hop)) {
    const size_t n = 2 * hop;
    auto *time = fft->sum.get();
    for (size_t p = 0; p < parts; ++p) {
      const auto count = std::min(hop, impulse.size() - p * hop);
      for (size_t i = 0; i < count; ++i)
        time[i] = impulse[p * hop + i] / n;
      std::fill(time + count, time + n, 0);
      storeSpectrum(time, bins.get(), p);
    }
  }

  bool tiled() const { return kTileWideSpectra && wideUniform; }

  void storeSpectrum(const double *input, double *bank, size_t part) const {
    const size_t n = 2 * hop;
    if (!tiled()) {
      fft->run(input, bank + part * n, PFFFT_FORWARD);
      return;
    }
    auto *ordered = fft->sum.get();
    pffftd_transform_ordered(fft->setup.get(), input, ordered, fft->work.get(),
                             PFFFT_FORWARD);
    for (size_t i = 0; i < n; i += kTileValues) {
      for (size_t j = 0; j < kTileValues; j += 2 * kGroupBins) {
        auto *dest = bank + i * parts + part * kTileValues + j;
        for (size_t lane = 0; lane < kGroupBins; ++lane) {
          dest[lane] = ordered[i + j + 2 * lane];
          dest[kGroupBins + lane] = ordered[i + j + 2 * lane + 1];
        }
      }
    }
  }

  size_t hop, parts;
  Plan fft;
  const bool wideUniform, pairedUniform;
  Buffer bins;
};
using PreparedFilter = std::shared_ptr<const Filter>;

std::array<PreparedFilter, 2> prepare(size_t hop, std::span<const double> left,
                                      std::span<const double> right,
                                      bool uniform = false) {
  if (left.empty() && right.empty())
    return {};
  const auto fft = std::make_shared<Transform>(2 * hop);
  const auto channel = [&](std::span<const double> impulse) -> PreparedFilter {
    return impulse.empty()
               ? nullptr
               : std::make_shared<Filter>(hop, impulse, fft, uniform);
  };
  const auto first = channel(left);
  return {first, std::ranges::equal(left, right) ? first : channel(right)};
}

/// One overlap-save bank. Its output slice remains valid until the hop fills.
class PartitionBank {
public:
  PartitionBank(PreparedFilter prepared, size_t phase, bool distribute = false)
      : filter(std::move(prepared)), hop(filter->hop), n(2 * hop), phase(phase),
        spectra(buffer(filter->parts * n)), time(buffer(n)), result(buffer(n)),
        partial((distribute || filter->pairedUniform) && filter->parts > 1
                    ? buffer(n)
                    : Buffer(nullptr, pffftd_aligned_free)),
        nextSum(filter->pairedUniform ? buffer(n)
                                      : Buffer(nullptr, pffftd_aligned_free)) {
    reset();
  }

  void reset() {
    filled = phase;
    slot = valid = accumulated = 0;
    std::fill_n(time.get(), n, 0);
    std::fill_n(result.get(), n, 0);
    if (nextSum) {
      secondHop = phase != 0;
      clearPair();
    }
  }

  const double *ready() const { return result.get() + hop + filled; }

  size_t remaining() const { return hop - filled; }

  void capture(const double *input, size_t count) {
    std::copy_n(input, count, time.get() + hop + filled);
  }

  void advance(size_t count) {
    filled += count;
    const bool complete = filled == hop;
    if (!complete && !partial)
      return;
    const auto past = std::min(valid, filter->parts - 1);
    if (filter->tiled()) {
      convolveTiles(past + 1);
    } else if (nextSum) {
      if (!secondHop)
        accumulatePair();
      if (!complete)
        return;
      auto *sum = secondHop ? nextSum.get() : partial.get();
      if (past)
        product(1, sum, false);
      finishHop(sum, false);
      if (secondHop)
        clearPair();
      secondHop = !secondHop;
    } else {
      auto *sum = partial ? partial.get() : filter->fft->sum.get();
      // Older spectra are ready before this hop's input FFT.
      const size_t due = complete ? past : (filled * past + hop - 1) / hop;
      while (accumulated < due) {
        ++accumulated;
        product(accumulated, sum, accumulated == 1);
      }
      if (!complete)
        return;
      finishHop(sum, accumulated == 0);
    }
    std::copy_n(time.get() + hop, hop, time.get());
    valid = std::min(valid + 1, filter->parts);
    slot = (slot == 0 ? filter->parts : slot) - 1;
    filled = accumulated = 0;
  }

private:
  PreparedFilter filter;
  const size_t hop, n, phase;
  size_t filled = 0, slot = 0, valid = 0, accumulated = 0;
  Buffer spectra, time, result, partial, nextSum;
  bool secondHop = false;

  void clearPair() {
    std::fill_n(partial.get(), n, 0);
    std::fill_n(nextSum.get(), n, 0);
  }

  void accumulatePair() {
    // From partition two onwards, both hops' input spectra are already known.
    const size_t count = std::min(valid, filter->parts - 2);
    const size_t due = (filled * count + hop - 1) / hop;
    while (accumulated < due) {
      const size_t partition = 2 + accumulated++;
      if (partition <= valid)
        product(partition, partial.get(), false);
      product(partition, nextSum.get(), false, 1);
    }
  }

  void finishHop(double *sum, bool first) {
    auto &fft = filter->fft;
    fft->run(time.get(), spectra.get() + slot * n, PFFFT_FORWARD);
    product(0, sum, first);
    fft->run(sum, result.get(), PFFFT_BACKWARD);
  }

  void convolveTiles(size_t activeParts) {
    filter->storeSpectrum(time.get(), spectra.get(), slot);
    const auto &fft = filter->fft;
    auto *sum = fft->sum.get();
    double dc = 0, nyquist = 0;
    for (size_t i = 0; i < n; i += kTileValues) {
      const double *history = spectra.get() + i * filter->parts;
      const double *coeff = filter->bins.get() + i * filter->parts;
      alignas(64) std::array<double, kTileValues> tile{};
      size_t block = slot;
      for (size_t p = 0; p < activeParts; ++p) {
        const double *spectrum = history + block * kTileValues;
        // The first ordered pair holds separate real DC and Nyquist values.
        if (i == 0) {
          dc += spectrum[0] * coeff[0];
          nyquist += spectrum[kGroupBins] * coeff[kGroupBins];
        }
        for (size_t j = 0; j < kTileValues; j += 2 * kGroupBins) {
          for (size_t lane = j; lane < j + kGroupBins; ++lane) {
            const size_t imag = lane + kGroupBins;
            tile[lane] +=
                spectrum[lane] * coeff[lane] - spectrum[imag] * coeff[imag];
            tile[imag] +=
                spectrum[lane] * coeff[imag] + spectrum[imag] * coeff[lane];
          }
        }
        coeff += kTileValues;
        if (++block == filter->parts)
          block = 0;
      }
      for (size_t j = 0; j < kTileValues; j += 2 * kGroupBins) {
        for (size_t lane = 0; lane < kGroupBins; ++lane) {
          sum[i + j + 2 * lane] = tile[j + lane];
          sum[i + j + 2 * lane + 1] = tile[j + kGroupBins + lane];
        }
      }
    }
    sum[0] = dc;
    sum[1] = nyquist;
    pffftd_transform_ordered(fft->setup.get(), sum, result.get(),
                             fft->work.get(), PFFFT_BACKWARD);
  }

  void product(size_t partition, double *sum, bool first,
               size_t hopsAhead = 0) const {
    size_t block = slot + partition - hopsAhead;
    if (block >= filter->parts)
      block -= filter->parts;
    const double *input = spectra.get() + block * n;
    const double *coefficients = filter->bins.get() + partition * n;
    if (first)
      pffftd_zconvolve(filter->fft->setup.get(), input, coefficients, sum);
    else
      pffftd_zconvolve_accumulate(filter->fft->setup.get(), input, coefficients,
                                  sum, 1);
  }
};
} // namespace

class FirRoomCorrection::Channel {
public:
  Channel(PreparedFilter early, PreparedFilter late, size_t headPhase,
          size_t tailPhase, bool distribute)
      : head(std::move(early), headPhase) {
    if (late)
      tail.emplace(std::move(late), tailPhase, distribute);
  }

  void reset() {
    head.reset();
    if (tail)
      tail->reset();
  }

  double *plane() { return samples.data(); }

  void process(size_t frames) {
    double *pcm = plane();
    while (frames) {
      const size_t count = std::min(frames, head.remaining());
      head.capture(pcm, count);
      const auto *early = head.ready();
      if (tail) {
        tail->capture(pcm, count);
        const auto *late = tail->ready();
        for (size_t i = 0; i < count; ++i)
          pcm[i] = early[i] + late[i];
        tail->advance(count);
      } else {
        std::copy_n(early, count, pcm);
      }
      head.advance(count);
      pcm += count;
      frames -= count;
    }
  }

private:
  std::array<double, kPlanarFrames> samples;
  PartitionBank head;
  std::optional<PartitionBank> tail;
};

FirRoomCorrection::Partitions
FirRoomCorrection::partitionsFor(size_t activeTaps) {
  if (activeTaps <= kPartitions.tail - kPartitions.head)
    return {kPartitions.head, kPartitions.head};
  return activeTaps <= 65536 ? Partitions{kPartitions.head, 5120} : kPartitions;
}

FirRoomCorrection::FirRoomCorrection(unsigned rate,
                                     std::span<const double> left,
                                     std::span<const double> right)
    : FirRoomCorrection(rate, left, right, std::nullopt) {}

FirRoomCorrection::FirRoomCorrection(unsigned rate,
                                     std::span<const double> left,
                                     std::span<const double> right,
                                     Partitions sizes)
    : FirRoomCorrection(rate, left, right, std::optional(sizes)) {}

FirRoomCorrection::FirRoomCorrection(unsigned rate,
                                     std::span<const double> left,
                                     std::span<const double> right,
                                     std::optional<Partitions> requested)
    : rate(rate), taps(std::max(left.size(), right.size())),
      latency(requested ? requested->head : kPartitions.head) {
  if (!rate || left.empty() || right.empty() || taps > kMaxTaps)
    throw std::invalid_argument("Invalid room impulse size or sample rate");
  auto validHop = [](size_t hop) {
    return hop >= 32 && hop <= kMaxHop &&
           pffftd_is_valid_size(2 * hop, PFFFT_REAL);
  };
  for (auto response : {left, right})
    if (!std::ranges::all_of(response,
                             [](double tap) { return std::isfinite(tap); }))
      throw std::invalid_argument("Non-finite room impulse");

  auto trimPadding = [](std::span<const double> impulse) {
    while (impulse.size() > 1 && impulse.back() == 0)
      impulse = impulse.first(impulse.size() - 1);
    return impulse;
  };
  left = trimPadding(left);
  right = trimPadding(right);
  const size_t activeTaps = std::max(left.size(), right.size());
  const auto sizes = requested.value_or(partitionsFor(activeTaps));
  if (!validHop(sizes.head) || !validHop(sizes.tail) ||
      sizes.tail % sizes.head != 0)
    throw std::invalid_argument("Invalid FIR partition lengths");
  historyFrames = activeTaps - 1;
  const bool uniform = sizes.head == sizes.tail;
  // The difference between bank delays aligns their output at the split.
  const size_t split = uniform ? activeTaps : sizes.tail - sizes.head;
  const size_t leftSplit = std::min(split, left.size());
  const size_t rightSplit = std::min(split, right.size());
  const auto early = prepare(sizes.head, left.first(leftSplit),
                             right.first(rightSplit), uniform);
  const auto late =
      prepare(sizes.tail, left.subspan(leftSplit), right.subspan(rightSplit));
  // Keep the two long FFTs out of the same short playback callback.
  const bool staggerHead =
      (uniform && (sizes.head == kPairedHop || sizes.head == 8192)) ||
      early[1]->wideUniform;
  size_t headPhase = staggerHead ? sizes.head / 2 : 0;
  // The paired banks alternate heavy hops and offset their FFTs by one callback.
  if (early[1]->pairedUniform)
    headPhase = 256;
  const size_t tailPhase = (sizes.tail / sizes.head / 2) * sizes.head;
  const bool distribute = rate > 48000;
  channels[0] = std::make_unique<Channel>(early[0], late[0], 0, 0, distribute);
  channels[1] = std::make_unique<Channel>(early[1], late[1], headPhase,
                                          tailPhase, distribute);
}

FirRoomCorrection::~FirRoomCorrection() = default;

bool FirRoomCorrection::handles(const StreamAudioFormat &f) const {
  return f.sampleRate == rate && f.channels == 2 && !isDsd(f.sampleFormat);
}

DspProcessor::Output
FirRoomCorrection::output(const StreamAudioFormat &format) const {
  auto out = format;
  out.bitsPerSample = 64;
  out.sampleFormat = PCM_FLOAT64_LE;
  return {out, 1};
}

SignalPath FirRoomCorrection::describe(const StreamAudioFormat &f) const {
  if (!handles(f))
    return {};
  return {{{"fir", "FIR room correction", std::format("{} taps", taps)}},
          1000.0 * latency / rate};
}

size_t FirRoomCorrection::start(const StreamAudioFormat &f) {
  if (!handles(f))
    throw std::invalid_argument(
        "Room impulse does not match the stream format");
  inputFormat = f.sampleFormat;
  reset();
  return latency;
}

bool FirRoomCorrection::changeInputFormat(const StreamAudioFormat &format) {
  if (!handles(format))
    return false;
  inputFormat = format.sampleFormat;
  return true;
}

size_t FirRoomCorrection::preparedHistory() const { return historyFrames; }

void FirRoomCorrection::reset() {
  for (auto &ch : channels)
    ch->reset();
  pending = 0;
}

size_t FirRoomCorrection::process(const uint8_t *in, uint8_t *out,
                                  size_t frames) {
  dsp::FlushFilterTails guard;
  double *planes[]{channels[0]->plane(), channels[1]->plane()};
  for (size_t at = 0; at < frames;) {
    const auto count = std::min(frames - at, kPlanarFrames);
    if (in)
      readPlanar(in + at * 2 * sampleSize(inputFormat), inputFormat, 2, count,
                 planes);
    const auto silentPlane = [count](const double *plane) {
      return std::all_of(plane, plane + count, [](double x) { return x == 0; });
    };
    const bool silent =
        !in || (silentPlane(planes[0]) && silentPlane(planes[1]));
    if (silent && !pending) {
      std::memset(out + at * 2 * sizeof(double), 0, count * 2 * sizeof(double));
      at += count;
      continue;
    }
    const auto active = silent ? std::min(count, pending) : count;
    if (!in)
      for (auto *plane : planes)
        std::fill_n(plane, count, 0);
    for (auto &ch : channels)
      ch->process(active);
    for (size_t i = 0; i < count; ++i) {
      const double pair[]{planes[0][i], planes[1][i]};
      std::memcpy(out + (at + i) * sizeof(pair), pair, sizeof(pair));
    }
    pending = silent ? pending - active : historyFrames + latency;
    if (!pending)
      reset();
    at += count;
  }
  return frames;
}
