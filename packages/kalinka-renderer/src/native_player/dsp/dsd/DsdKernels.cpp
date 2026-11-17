#include "dsp/dsd/DsdKernels.h"

#include <algorithm>
#include <iterator>
#include <utility>

#include "dsp/HighwayTargets.h"

#undef HWY_TARGET_INCLUDE
#define HWY_TARGET_INCLUDE "dsp/dsd/DsdKernels.cpp"
#include <hwy/foreach_target.h> // IWYU pragma: keep
#include <hwy/highway.h>

HWY_BEFORE_NAMESPACE();
namespace dsd_kernels {
namespace HWY_NAMESPACE {
namespace hn = hwy::HWY_NAMESPACE;

void Stage1Pair(const Stage1Table *tables, size_t stored, size_t groups,
                const uint8_t *const direct[2], const uint8_t *const mirror[2],
                size_t count, double scale, double *const out[2]) {
  const hn::FixedTag<double, 2> d;
  const auto factor = hn::Set(d, scale);
  for (size_t m = 0; m < count; ++m) {
    const uint8_t *a = direct[0] + groups - 1 + m;
    const uint8_t *b = direct[1] + groups - 1 + m;
    const uint8_t *ra = mirror[0] + groups - 1 + m;
    const uint8_t *rb = mirror[1] + groups - 1 + m;
    const auto look = [&](const Stage1Table &table, const uint8_t *x,
                          const uint8_t *y, ptrdiff_t back) {
      return hn::Dup128VecFromValues(d, table[x[-back]], table[y[-back]]);
    };
    auto even = hn::Zero(d), odd = hn::Zero(d);
    ptrdiff_t g = 0;
    for (; g + 2 <= ptrdiff_t(stored); g += 2) {
      even = hn::Add(even, look(tables[g], a, b, g));
      odd = hn::Add(odd, look(tables[g + 1], a, b, g + 1));
    }
    for (; g < ptrdiff_t(stored); ++g)
      even = hn::Add(even, look(tables[g], a, b, g));
    for (; g < ptrdiff_t(groups); ++g)
      odd = hn::Add(odd, look(tables[groups - 1 - g], ra, rb, g));
    HWY_ALIGN double pair[2];
    hn::Store(hn::Mul(hn::Add(even, odd), factor), d, pair);
    out[0][m] = pair[0];
    out[1][m] = pair[1];
  }
}

void Stage1Nibbles(const NibbleTables *tables, size_t groups,
                   const uint8_t *direct, size_t count, double scale,
                   double *out) {
  const hn::ScalableTag<uint8_t> d8;
  const hn::Repartition<uint16_t, decltype(d8)> d16;
  using Sums = hn::Vec<decltype(d16)>;
  const size_t lanes = hn::Lanes(d8);
  const auto lowBits = hn::Set(d8, 0x0F);
  const double offset = 2.0 * double(groups) * kNibbleBias;
  HWY_ALIGN uint16_t planes[kNibblePlanes][kByteSlack];
  for (size_t m = 0; m < count; m += lanes) {
    const uint8_t *newest = direct + groups - 1 + m;
    Sums lowLanes[kNibblePlanes], highLanes[kNibblePlanes];
    for (size_t p = 0; p < kNibblePlanes; ++p)
      lowLanes[p] = highLanes[p] = hn::Zero(d16);
    for (size_t g = 0; g < groups; ++g) {
      const auto bytes = hn::LoadU(d8, newest - ptrdiff_t(g));
      const auto low = hn::And(bytes, lowBits);
      const auto high = hn::ShiftRight<4>(bytes);
      const auto &table = tables[g];
      // Two lookups per plane stay under 2^9, so 16-bit lanes hold them all.
      for (size_t p = 0; p < kNibblePlanes; ++p) {
        const auto a =
            hn::TableLookupBytes(hn::LoadDup128(d8, table.low[p]), low);
        const auto b =
            hn::TableLookupBytes(hn::LoadDup128(d8, table.high[p]), high);
        lowLanes[p] = hn::Add(lowLanes[p], hn::Add(hn::PromoteLowerTo(d16, a),
                                                   hn::PromoteLowerTo(d16, b)));
        highLanes[p] =
            hn::Add(highLanes[p], hn::Add(hn::PromoteUpperTo(d16, a),
                                          hn::PromoteUpperTo(d16, b)));
      }
    }
    for (size_t p = 0; p < kNibblePlanes; ++p) {
      hn::Store(lowLanes[p], d16, planes[p]);
      hn::Store(highLanes[p], d16, planes[p] + lanes / 2);
    }
    const size_t done = std::min(lanes, count - m);
    for (size_t lane = 0; lane < done; ++lane) {
      // Whole numbers under 2^53: exact, as every other stage 1 is.
      double sum = 0;
      for (size_t p = kNibblePlanes; p-- > 0;)
        sum = sum * 256.0 + double(planes[p][lane]);
      out[m + lane] = (sum - offset) * scale;
    }
  }
}

using D2 = hn::FixedTag<double, 2>;
using V2 = hn::Vec<D2>;

/// Vectors of two pending outputs stage1Rows() keeps a channel for G groups.
template <size_t G> constexpr size_t kPending = (G + 2) / 2;
/// Vectors of its row the first byte of a pair adds; the second adds
/// kPending.
template <size_t G> constexpr size_t kFirstAdds = (G + 1) / 2;

/// Stands for the byte missing beside a lone one at either end of the bytes.
HWY_ALIGN constexpr double
    kZeroRow[stage1RowStride(std::ranges::max(kStage1RowGroups))] = {};

template <size_t G>
HWY_INLINE const double *RowOf(const double *rows, uint8_t byte) {
  return rows + size_t(byte) * stage1RowStride(G);
}

/**
 * @brief One pair of a channel's bytes onto its pending sums: the first's row
 * as they stand, the second's a lane on. The first pending vector is then
 * complete: it goes to @p out as two outputs and comes back zeroed as the
 * last. R is the step modulo kPending, the index of the first.
 */
template <size_t G, size_t R>
HWY_INLINE void Step(D2 d, const double *first, const double *second, V2 factor,
                     V2 *pending, double *out) {
  constexpr size_t n = kPending<G>;
  for (size_t i = 0; i < n; ++i) {
    // The rows summed first leave each pending chain one add a step.
    V2 rows = hn::Load(d, second + 2 * i);
    if (i < kFirstAdds<G>)
      rows = hn::Add(rows, hn::LoadU(d, first + 1 + 2 * i));
    pending[(R + i) % n] = hn::Add(pending[(R + i) % n], rows);
  }
  hn::StoreU(hn::Mul(pending[R], factor), d, out);
  pending[R] = hn::Zero(d);
}

/// Step() for each of C channels on its pair of bytes at @p at, onto
/// out[c] + at.
template <size_t G, size_t C, size_t R>
HWY_INLINE void Pair(D2 d, const double *rows, const uint8_t *const *bytes,
                     size_t at, V2 factor, V2 (*pending)[kPending<G>],
                     double *const *out) {
  for (size_t c = 0; c < C; ++c)
    Step<G, R>(d, RowOf<G>(rows, bytes[c][at]),
               RowOf<G>(rows, bytes[c][at + 1]), factor, pending[c],
               out[c] + at);
}

/// Pair() on the pairs from @p at on, one for each K, the first at rotation
/// R0.
template <size_t G, size_t C, size_t R0, size_t... K>
HWY_INLINE void Pairs(std::index_sequence<K...>, D2 d, const double *rows,
                      const uint8_t *const *bytes, size_t at, V2 factor,
                      V2 (*pending)[kPending<G>], double *const *out) {
  (Pair<G, C, (R0 + K) % kPending<G>>(d, rows, bytes, at + 2 * K, factor,
                                      pending, out),
   ...);
}

/**
 * @brief Runs the G - 1 bytes carried from the block before onto zeroed
 * pending sums, so the block's first pair finds them at rotation 0. What they
 * complete was the block before's, and is dropped. An odd count leaves the
 * first carried byte without the one before it, which the zero row stands
 * for.
 */
template <size_t G, size_t C>
HWY_INLINE void WarmUp(D2 d, const double *rows, const uint8_t *const *carried,
                       V2 factor, V2 (*pending)[kPending<G>]) {
  constexpr size_t n = kPending<G>, count = G - 1, lone = count % 2;
  constexpr size_t steps = (count + 1) / 2, first = (n - steps % n) % n;
  double dropped[C][count];
  double *drop[C];
  for (size_t c = 0; c < C; ++c) {
    drop[c] = dropped[c];
    if constexpr (lone)
      Step<G, first>(d, kZeroRow, RowOf<G>(rows, carried[c][0]), factor,
                     pending[c], drop[c]);
  }
  Pairs<G, C, (first + lone) % n>(std::make_index_sequence<steps - lone>{}, d,
                                  rows, carried, lone, factor, pending, drop);
}

/**
 * @brief Step K of a block's last round, short of kPending pairs: a pair
 * while @p pairs last, then the block's odd last byte, if @p odd, beside the
 * zero row, its first output kept.
 */
template <size_t G, size_t C, size_t K>
HWY_INLINE void TailStep(D2 d, const double *rows, const uint8_t *const *bytes,
                         size_t at, size_t pairs, bool odd, V2 factor,
                         V2 (*pending)[kPending<G>], double *const *out) {
  const size_t from = at + 2 * K;
  if (K < pairs) {
    Pair<G, C, K>(d, rows, bytes, from, factor, pending, out);
  } else if (K == pairs && odd) {
    for (size_t c = 0; c < C; ++c) {
      double both[2];
      Step<G, K>(d, RowOf<G>(rows, bytes[c][from]), kZeroRow, factor,
                 pending[c], both);
      out[c][from] = both[0];
    }
  }
}

template <size_t G, size_t C, size_t... K>
HWY_INLINE void Tail(std::index_sequence<K...>, D2 d, const double *rows,
                     const uint8_t *const *bytes, size_t at, size_t pairs,
                     bool odd, V2 factor, V2 (*pending)[kPending<G>],
                     double *const *out) {
  (TailStep<G, C, K>(d, rows, bytes, at, pairs, odd, factor, pending, out),
   ...);
}

/// stage1Rows() for G groups and C channels, kPending steps a round so each
/// round ends with the pending sums where it began.
template <size_t G, size_t C>
HWY_NOINLINE void Rows(const double *rows, const uint8_t *const *direct,
                       size_t count, double scale, double *const *out) {
  constexpr size_t n = kPending<G>;
  const D2 d;
  const V2 factor = hn::Set(d, scale);
  V2 pending[C][n];
  const uint8_t *block[C];
  // Vector stores may alias the caller's array, which would be read again.
  double *to[C];
  for (size_t c = 0; c < C; ++c) {
    for (V2 &sum : pending[c])
      sum = hn::Zero(d);
    block[c] = direct[c] + G - 1;
    to[c] = out[c];
  }
  WarmUp<G, C>(d, rows, direct, factor, pending);
  size_t at = 0;
  for (; at + 2 * n <= count; at += 2 * n)
    Pairs<G, C, 0>(std::make_index_sequence<n>{}, d, rows, block, at, factor,
                   pending, to);
  Tail<G, C>(std::make_index_sequence<n>{}, d, rows, block, at,
             (count - at) / 2, (count - at) % 2 != 0, factor, pending, to);
}

/// Whether two channels share Rows()'s loop: past 8 groups their pending sums
/// no longer fit NEON's registers together.
template <size_t G> constexpr bool kChannelsShare = G <= 8;

/// Rows() for @p channels, if @p groups is G.
template <size_t G>
HWY_INLINE void RowsIf(size_t groups, const double *rows,
                       const uint8_t *const *direct, unsigned channels,
                       size_t count, double scale, double *const *out) {
  if (groups != G)
    return;
  if (channels == 2 && kChannelsShare<G>) {
    Rows<G, 2>(rows, direct, count, scale, out);
    return;
  }
  for (unsigned c = 0; c < channels; ++c)
    Rows<G, 1>(rows, direct + c, count, scale, out + c);
}

template <size_t... I>
HWY_INLINE void RowsForAny(std::index_sequence<I...>, size_t groups,
                           const double *rows, const uint8_t *const *direct,
                           unsigned channels, size_t count, double scale,
                           double *const *out) {
  (RowsIf<kStage1RowGroups[I]>(groups, rows, direct, channels, count, scale,
                               out),
   ...);
}

void Stage1Rows(const double *rows, size_t groups, const uint8_t *const *direct,
                unsigned channels, size_t count, double scale,
                double *const *out) {
  RowsForAny(std::make_index_sequence<std::size(kStage1RowGroups)>{}, groups,
             rows, direct, channels, count, scale, out);
}

} // namespace HWY_NAMESPACE
} // namespace dsd_kernels
HWY_AFTER_NAMESPACE();

#if HWY_ONCE
namespace dsd_kernels {
HWY_EXPORT(Stage1Pair);
HWY_EXPORT(Stage1Nibbles);
HWY_EXPORT(Stage1Rows);

void stage1Nibbles(const NibbleTables *tables, size_t groups,
                   const uint8_t *direct, size_t count, double scale,
                   double *out) {
  HWY_DYNAMIC_DISPATCH(Stage1Nibbles)(tables, groups, direct, count, scale,
                                      out);
}

void stage1(const Stage1Table *tables, size_t stored, size_t groups,
            const uint8_t *direct, const uint8_t *mirror, size_t count,
            double scale, double *out) {
  const ptrdiff_t whole = ptrdiff_t(stored), all = ptrdiff_t(groups);
  for (size_t m = 0; m < count; ++m) {
    const uint8_t *newest = direct + groups - 1 + m;
    const uint8_t *reversed = mirror + groups - 1 + m;
    // Four sums: each add would otherwise wait for the one before it.
    double s0 = 0, s1 = 0, s2 = 0, s3 = 0;
    ptrdiff_t g = 0;
    for (; g + 4 <= whole; g += 4) {
      s0 += tables[g][newest[-g]];
      s1 += tables[g + 1][newest[-g - 1]];
      s2 += tables[g + 2][newest[-g - 2]];
      s3 += tables[g + 3][newest[-g - 3]];
    }
    for (; g < whole; ++g)
      s0 += tables[g][newest[-g]];
    for (; g + 4 <= all; g += 4) {
      s1 += tables[all - 1 - g][reversed[-g]];
      s2 += tables[all - 2 - g][reversed[-g - 1]];
      s3 += tables[all - 3 - g][reversed[-g - 2]];
      s0 += tables[all - 4 - g][reversed[-g - 3]];
    }
    for (; g < all; ++g)
      s1 += tables[all - 1 - g][reversed[-g]];
    out[m] = ((s0 + s1) + (s2 + s3)) * scale;
  }
}

void stage1Pair(const Stage1Table *tables, size_t stored, size_t groups,
                const uint8_t *const direct[2], const uint8_t *const mirror[2],
                size_t count, double scale, double *const out[2]) {
  HWY_DYNAMIC_DISPATCH(Stage1Pair)(tables, stored, groups, direct, mirror,
                                   count, scale, out);
}

void stage1Rows(const double *rows, size_t groups, const uint8_t *const *direct,
                unsigned channels, size_t count, double scale,
                double *const *out) {
  HWY_DYNAMIC_DISPATCH(Stage1Rows)(rows, groups, direct, channels, count, scale,
                                   out);
}

} // namespace dsd_kernels
#endif
