#include "dsp/dsd/DsdKernels.h"

#include <algorithm>

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

} // namespace HWY_NAMESPACE
} // namespace dsd_kernels
HWY_AFTER_NAMESPACE();

#if HWY_ONCE
namespace dsd_kernels {
HWY_EXPORT(Stage1Pair);
HWY_EXPORT(Stage1Nibbles);

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

} // namespace dsd_kernels
#endif
