#include "dsp/dsd/Stage1.h"

#include "dsp/dsd/ByteHistory.h"
#include "dsp/dsd/DsdKernels.h"

#include <stdexcept>
#include <string>
#include <vector>

namespace {
/// Whole tables up to here stay inside the 32 KB stage 1 budget.
constexpr size_t kWholeGroups = 16;

/**
 * @brief Stage 1's taps as 256 sums per group of 8, looked up by byte: every
 * group in the stream's bit order, or past the budget the first half, MSB
 * first, which symmetry lets stand for the mirrored rest.
 */
class ByteTables {
public:
  explicit ByteTables(const Stage1Spec &spec)
      : groups_(spec.size / 8), whole_(!readsReversed(spec)),
        lsbFirst(spec.order == DsdBitOrder::LsbFirst) {
    const auto tapOf = [&](size_t g, unsigned bit) {
      return spec.taps[8 * g + (whole_ && lsbFirst ? 7 - bit : bit)];
    };
    tables.resize(whole_ ? groups_ : (groups_ + 1) / 2);
    for (size_t g = 0; g < tables.size(); ++g) {
      for (unsigned byte = 0; byte < 256; ++byte) {
        int64_t sum = 0;
        for (unsigned bit = 0; bit < 8; ++bit)
          sum += (byte >> bit) & 1 ? tapOf(g, bit) : -tapOf(g, bit);
        tables[g][byte] = double(sum);
      }
    }
  }

  /// Whether the kernels read bytes with their bits reversed: past the budget,
  /// where only half the tables are stored.
  static bool readsReversed(const Stage1Spec &spec) {
    return spec.size / 8 > kWholeGroups;
  }

  size_t groups() const { return groups_; }
  size_t stored() const { return tables.size(); }
  const dsd_kernels::Stage1Table *data() const { return tables.data(); }

  /// The bytes the stored groups read, in the order the tables were built in.
  const uint8_t *direct(const ByteHistory &history, unsigned c) const {
    return !whole_ && lsbFirst ? history.reversed(c) : history.bytes(c);
  }
  /// The bytes the mirrored groups read, reversed against direct(). With
  /// every group stored they go unread, but the kernels still offset them, so
  /// they are bytes that exist.
  const uint8_t *mirror(const ByteHistory &history, unsigned c) const {
    return lsbFirst || whole_ ? history.bytes(c) : history.reversed(c);
  }

  /// Stage 1's sum for the byte at @p newest, plainly: the reference.
  double sum(const uint8_t *newest) const {
    double sum = 0;
    for (size_t g = 0; g < stored(); ++g) {
      const uint8_t byte = newest[-ptrdiff_t(g)];
      sum += tables[g][!whole_ && lsbFirst ? kBitReversed[byte] : byte];
    }
    for (size_t g = stored(); g < groups_; ++g) {
      const uint8_t byte = newest[-ptrdiff_t(g)];
      sum += tables[groups_ - 1 - g][lsbFirst ? byte : kBitReversed[byte]];
    }
    return sum;
  }

private:
  size_t groups_;
  bool whole_;
  bool lsbFirst;
  std::vector<dsd_kernels::Stage1Table> tables;
};

/// What every stage 1 shares: each channel's history around its loops.
class HistoryStage1 : public Stage1 {
public:
  void process(const uint8_t *in, size_t bytes, double *const *out) final {
    history.append(in, bytes);
    convert(bytes, out);
    history.advance(bytes);
  }
  void reset() final { history.reset(); }

protected:
  HistoryStage1(const Stage1Spec &spec, bool keepReversed)
      : history(spec.channels, spec.size / 8 - 1, spec.capacity, spec.order,
                keepReversed),
        scale(spec.scale), channels(spec.channels) {}

  /// Converts the block just appended to the history.
  virtual void convert(size_t bytes, double *const *out) = 0;

  ByteHistory history;
  double scale;
  unsigned channels;
};

/// The plain loop the others are checked against.
class ReferenceStage1 final : public HistoryStage1 {
public:
  explicit ReferenceStage1(const Stage1Spec &spec)
      : HistoryStage1(spec, false), tables(spec) {}

private:
  void convert(size_t bytes, double *const *out) override {
    for (unsigned c = 0; c < channels; ++c) {
      const uint8_t *newest = history.bytes(c) + tables.groups() - 1;
      for (size_t m = 0; m < bytes; ++m)
        out[c][m] = tables.sum(newest + m) * scale;
    }
  }

  ByteTables tables;
};

/// One channel after another, four partial sums at a time.
class SumsStage1 final : public HistoryStage1 {
public:
  explicit SumsStage1(const Stage1Spec &spec)
      : HistoryStage1(spec, ByteTables::readsReversed(spec)), tables(spec) {}

private:
  void convert(size_t bytes, double *const *out) override {
    for (unsigned c = 0; c < channels; ++c)
      dsd_kernels::stage1(tables.data(), tables.stored(), tables.groups(),
                          tables.direct(history, c), tables.mirror(history, c),
                          bytes, scale, out[c]);
  }

  ByteTables tables;
};

/// Two channels at once, their sums side by side in one vector.
class ChannelPairStage1 final : public HistoryStage1 {
public:
  explicit ChannelPairStage1(const Stage1Spec &spec)
      : HistoryStage1(spec, ByteTables::readsReversed(spec)), tables(spec) {}

private:
  void convert(size_t bytes, double *const *out) override {
    unsigned c = 0;
    for (; c + 1 < channels; c += 2) {
      const uint8_t *const direct[] = {tables.direct(history, c),
                                       tables.direct(history, c + 1)};
      const uint8_t *const mirror[] = {tables.mirror(history, c),
                                       tables.mirror(history, c + 1)};
      double *const pair[] = {out[c], out[c + 1]};
      dsd_kernels::stage1Pair(tables.data(), tables.stored(), tables.groups(),
                              direct, mirror, bytes, scale, pair);
    }
    if (c < channels)
      dsd_kernels::stage1(tables.data(), tables.stored(), tables.groups(),
                          tables.direct(history, c), tables.mirror(history, c),
                          bytes, scale, out[c]);
  }

  ByteTables tables;
};

/// Every group's taps by nibble, always in the stream's bit order.
std::vector<dsd_kernels::NibbleTables> nibbleTables(const Stage1Spec &spec) {
  const bool lsbFirst = spec.order == DsdBitOrder::LsbFirst;
  const auto tapOf = [&](size_t g, unsigned bit) {
    return spec.taps[8 * g + (lsbFirst ? 7 - bit : bit)];
  };
  std::vector<dsd_kernels::NibbleTables> tables(spec.size / 8);
  for (size_t g = 0; g < tables.size(); ++g) {
    for (unsigned half = 0; half < 2; ++half) {
      auto &planes = half ? tables[g].high : tables[g].low;
      for (unsigned n = 0; n < 16; ++n) {
        int64_t sum = 0;
        for (unsigned i = 0; i < 4; ++i) {
          const auto tap = tapOf(g, 4 * half + i);
          sum += (n >> i) & 1 ? tap : -tap;
        }
        const auto biased = uint64_t(sum + int64_t(dsd_kernels::kNibbleBias));
        for (size_t p = 0; p < dsd_kernels::kNibblePlanes; ++p)
          planes[p][n] = uint8_t(biased >> (8 * p));
      }
    }
  }
  return tables;
}

/// Lookups by 4 bits, from tables held in vector registers.
class NibbleStage1 final : public HistoryStage1 {
public:
  explicit NibbleStage1(const Stage1Spec &spec)
      : HistoryStage1(spec, false), tables(nibbleTables(spec)) {}

private:
  void convert(size_t bytes, double *const *out) override {
    for (unsigned c = 0; c < channels; ++c)
      dsd_kernels::stage1Nibbles(tables.data(), tables.size(), history.bytes(c),
                                 bytes, scale, out[c]);
  }

  std::vector<dsd_kernels::NibbleTables> tables;
};
} // namespace

std::unique_ptr<Stage1> makeStage1(DsdKernel kernel, const Stage1Spec &spec) {
  switch (kernel) {
  case DsdKernel::Reference:
    return std::make_unique<ReferenceStage1>(spec);
  case DsdKernel::Sums:
    return std::make_unique<SumsStage1>(spec);
  case DsdKernel::Nibbles:
    return std::make_unique<NibbleStage1>(spec);
  // Channel pairs measured fastest on x86-64 and the Cortex-A76; on the A72
  // nibble lookups lead below DSD256 (benchmarks/dsp/results.md).
  case DsdKernel::ChannelPair:
  case DsdKernel::Fast:
    return std::make_unique<ChannelPairStage1>(spec);
  }
  throw std::invalid_argument("No stage 1 kernel " +
                              std::to_string(static_cast<int>(kernel)));
}
