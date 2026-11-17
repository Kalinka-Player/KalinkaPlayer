#include "dsp/output/OutputKernels.h"

#include <algorithm>

#include "dsp/HighwayTargets.h"

#undef HWY_TARGET_INCLUDE
#define HWY_TARGET_INCLUDE "dsp/output/OutputKernels.cpp"
#include <hwy/foreach_target.h> // IWYU pragma: keep
#include <hwy/highway.h>

HWY_BEFORE_NAMESPACE();
namespace output_kernels {
namespace HWY_NAMESPACE {
namespace hn = hwy::HWY_NAMESPACE;

/// Lanes a tail is padded to: the widest vector of doubles.
constexpr size_t kTail = 16;

size_t Quantise(const double *x, size_t count, double from, double step,
                double scale, const double *dither, int32_t lo, int32_t hi,
                int32_t *out) {
  const hn::ScalableTag<double> d;
  const hn::RebindToSigned<decltype(d)> dc;
  const hn::Rebind<int32_t, decltype(d)> di;
  const size_t lanes = hn::Lanes(d);
  const auto low = hn::Set(d, double(lo)), high = hn::Set(d, double(hi));
  const auto steps = hn::Set(d, step), start = hn::Set(d, from);
  const auto scaled = hn::Set(d, scale);
  const auto advance = hn::Set(d, double(lanes));
  auto index = hn::Iota(d, 1.0);
  auto outside = hn::Zero(dc);
  const auto round = [&](const double *xs, const double *ds,
                         hn::Vec<decltype(d)> gain, int32_t *to) {
    const auto v = hn::Round(
        hn::MulAdd(hn::Mul(hn::LoadU(d, xs), gain), scaled, hn::LoadU(d, ds)));
    const auto over = hn::Or(hn::Lt(v, low), hn::Gt(v, high));
    outside = hn::Sub(outside, hn::BitCast(dc, hn::VecFromMask(d, over)));
    hn::StoreU(hn::DemoteTo(di, hn::Min(hn::Max(v, low), high)), di, to);
  };
  const auto ramped = [&] {
    const auto gain = hn::MulAdd(steps, index, start);
    index = hn::Add(index, advance);
    return gain;
  };
  size_t i = 0;
  if (step == 0.0) {
    for (; i + lanes <= count; i += lanes)
      round(x + i, dither + i, start, out + i);
  } else {
    for (; i + lanes <= count; i += lanes)
      round(x + i, dither + i, ramped(), out + i);
  }
  if (i < count) {
    // The padding rounds to 0, which lo <= 0 <= hi never counts as clipped.
    HWY_ALIGN double xs[kTail] = {}, ds[kTail] = {};
    HWY_ALIGN int32_t to[kTail];
    std::copy(x + i, x + count, xs);
    std::copy(dither + i, dither + count, ds);
    round(xs, ds, ramped(), to);
    std::copy(to, to + (count - i), out + i);
  }
  return size_t(hn::ReduceSum(dc, outside));
}

} // namespace HWY_NAMESPACE
} // namespace output_kernels
HWY_AFTER_NAMESPACE();

#if HWY_ONCE
namespace output_kernels {
HWY_EXPORT(Quantise);

size_t quantise(const double *x, size_t count, double from, double step,
                double scale, const double *dither, int32_t lo, int32_t hi,
                int32_t *out) {
  return HWY_DYNAMIC_DISPATCH(Quantise)(x, count, from, step, scale, dither, lo,
                                        hi, out);
}
} // namespace output_kernels
#endif
