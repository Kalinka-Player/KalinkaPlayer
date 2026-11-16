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
  const hn::Rebind<int32_t, decltype(d)> di;
  const size_t lanes = hn::Lanes(d);
  const auto low = hn::Set(d, double(lo)), high = hn::Set(d, double(hi));
  const auto steps = hn::Set(d, step), start = hn::Set(d, from);
  const auto scaled = hn::Set(d, scale);
  size_t clipped = 0;
  const auto one = [&](const double *xs, const double *ds, size_t at,
                       size_t valid, int32_t *to) {
    const auto gain = hn::MulAdd(steps, hn::Iota(d, double(at + 1)), start);
    const auto v = hn::Round(
        hn::MulAdd(hn::Mul(hn::LoadU(d, xs), gain), scaled, hn::LoadU(d, ds)));
    const auto outside = hn::Or(hn::Lt(v, low), hn::Gt(v, high));
    const auto firstN = hn::FirstN(d, valid);
    clipped += hn::CountTrue(d, hn::And(outside, firstN));
    hn::StoreU(hn::DemoteTo(di, hn::Min(hn::Max(v, low), high)), di, to);
  };
  size_t i = 0;
  for (; i + lanes <= count; i += lanes)
    one(x + i, dither + i, i, lanes, out + i);
  if (i < count) {
    HWY_ALIGN double xs[kTail] = {}, ds[kTail] = {};
    HWY_ALIGN int32_t to[kTail];
    std::copy(x + i, x + count, xs);
    std::copy(dither + i, dither + count, ds);
    one(xs, ds, i, count - i, to);
    std::copy(to, to + (count - i), out + i);
  }
  return clipped;
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
