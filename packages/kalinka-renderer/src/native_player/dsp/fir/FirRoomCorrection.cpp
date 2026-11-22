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

struct Filter {
  Filter(size_t hop, std::span<const double> impulse, Plan plan)
      : hop(hop), parts((impulse.size() + hop - 1) / hop), fft(std::move(plan)),
        bins(buffer(parts * 2 * hop)) {
    const size_t n = 2 * hop;
    auto *time = fft->sum.get();
    for (size_t p = 0; p < parts; ++p) {
      const auto count = std::min(hop, impulse.size() - p * hop);
      for (size_t i = 0; i < count; ++i)
        time[i] = impulse[p * hop + i] / n;
      std::fill(time + count, time + n, 0);
      fft->run(time, bins.get() + p * n, PFFFT_FORWARD);
    }
  }
  size_t hop, parts;
  Plan fft;
  Buffer bins;
};
using PreparedFilter = std::shared_ptr<const Filter>;

std::array<PreparedFilter, 2> prepare(size_t hop, std::span<const double> left,
                                      std::span<const double> right) {
  if (left.empty() && right.empty())
    return {};
  const auto fft = std::make_shared<Transform>(2 * hop);
  const auto channel = [&](std::span<const double> impulse) -> PreparedFilter {
    return impulse.empty() ? nullptr
                           : std::make_shared<Filter>(hop, impulse, fft);
  };
  const auto first = channel(left);
  return {first, std::ranges::equal(left, right) ? first : channel(right)};
}

/// One overlap-save bank. Its output slice remains valid until the hop fills.
class PartitionBank {
public:
  PartitionBank(PreparedFilter prepared, size_t phase)
      : filter(std::move(prepared)), hop(filter->hop), n(2 * hop), phase(phase),
        spectra(buffer(filter->parts * n)), time(buffer(n)), result(buffer(n)) {
    reset();
  }

  void reset() {
    filled = phase;
    slot = valid = 0;
    std::fill_n(time.get(), n, 0);
    std::fill_n(result.get(), n, 0);
  }

  const double *ready() const { return result.get() + hop + filled; }

  size_t remaining() const { return hop - filled; }

  void capture(const double *input, size_t count) {
    std::copy_n(input, count, time.get() + hop + filled);
  }

  void advance(size_t count) {
    filled += count;
    if (filled != hop)
      return;
    filled = 0;
    auto &fft = filter->fft;
    const auto *filters = filter->bins.get();
    const auto parts = filter->parts;
    fft->run(time.get(), spectra.get() + slot * n, PFFFT_FORWARD);
    std::copy_n(time.get() + hop, hop, time.get());
    valid = std::min(valid + 1, parts);
    size_t block = slot;
    pffftd_zconvolve(fft->setup.get(), spectra.get() + block * n, filters,
                     fft->sum.get());
    for (size_t p = 1; p < valid; ++p) {
      if (++block == parts)
        block = 0;
      pffftd_zconvolve_accumulate(fft->setup.get(), spectra.get() + block * n,
                                  filters + p * n, fft->sum.get(), 1);
    }
    fft->run(fft->sum.get(), result.get(), PFFFT_BACKWARD);
    slot = (slot == 0 ? parts : slot) - 1;
  }

private:
  PreparedFilter filter;
  const size_t hop, n, phase;
  size_t filled = 0, slot = 0, valid = 0;
  Buffer spectra, time, result;
};
} // namespace

class FirRoomCorrection::Channel {
public:
  Channel(PreparedFilter early, PreparedFilter late, size_t phase)
      : head(std::move(early), 0) {
    if (late)
      tail.emplace(std::move(late), phase);
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

FirRoomCorrection::FirRoomCorrection(unsigned rate,
                                     std::span<const double> left,
                                     std::span<const double> right,
                                     Partitions sizes)
    : rate(rate), taps(std::max(left.size(), right.size())),
      latency(sizes.head) {
  if (!rate || left.empty() || right.empty() || taps > kMaxTaps)
    throw std::invalid_argument("Invalid room impulse size or sample rate");
  auto validHop = [](size_t hop) {
    return hop >= 32 && hop <= 16384 &&
           pffftd_is_valid_size(2 * hop, PFFFT_REAL);
  };
  if (!validHop(sizes.head) || !validHop(sizes.tail) ||
      sizes.tail % sizes.head != 0)
    throw std::invalid_argument("Invalid FIR partition lengths");
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
  historyFrames = activeTaps - 1;
  // The difference between bank delays aligns their output at the split.
  const size_t split =
      sizes.head == sizes.tail ? activeTaps : sizes.tail - sizes.head;
  const size_t leftSplit = std::min(split, left.size());
  const size_t rightSplit = std::min(split, right.size());
  const auto early =
      prepare(sizes.head, left.first(leftSplit), right.first(rightSplit));
  const auto late =
      prepare(sizes.tail, left.subspan(leftSplit), right.subspan(rightSplit));
  // Keep the two long FFTs out of the same short playback callback.
  const size_t phase = (sizes.tail / sizes.head / 2) * sizes.head;
  channels[0] = std::make_unique<Channel>(early[0], late[0], 0);
  channels[1] = std::make_unique<Channel>(early[1], late[1], phase);
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
