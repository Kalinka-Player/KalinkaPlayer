#include "dsp/fir/FirKernels.h"

#include <algorithm>

#include "dsp/HighwayTargets.h"

#undef HWY_TARGET_INCLUDE
#define HWY_TARGET_INCLUDE "dsp/fir/FirKernels.cpp"
#include <hwy/foreach_target.h> // IWYU pragma: keep
#include <hwy/highway.h>

HWY_BEFORE_NAMESPACE();
namespace fir_kernels {
namespace HWY_NAMESPACE {
namespace hn = hwy::HWY_NAMESPACE;

/// Vectors summed at once: enough to hide a multiply-add's latency.
constexpr size_t kChains = 4;

template <class D, class V>
HWY_INLINE void store(D d, V v, double *out, size_t done, size_t count) {
  const size_t lanes = hn::Lanes(d);
  if (done + lanes <= count) {
    hn::StoreU(v, d, out + done);
    return;
  }
  HWY_ALIGN double last[kSlack];
  hn::Store(v, d, last);
  std::copy(last, last + (count - done), out + done);
}

void Symmetric(const double *taps, size_t half, const double *x, size_t count,
               double *out) {
  const hn::ScalableTag<double> d;
  const size_t lanes = hn::Lanes(d);
  const size_t centre = half - 1;
  const auto first = hn::Set(d, taps[centre]);
  // Each output's sum is the same whichever path takes it: the chains only
  // hide each other's latency.
  const auto at = [&](const double *newest, auto &sums, size_t chains) {
    for (size_t k = 0; k < chains; ++k)
      sums[k] = hn::Mul(first, hn::LoadU(d, newest + k * lanes - centre));
    for (size_t j = 0; j < centre; ++j) {
      const auto tap = hn::Set(d, taps[j]);
      const double *a = newest - j, *b = newest - 2 * centre + j;
      for (size_t k = 0; k < chains; ++k)
        sums[k] = hn::MulAdd(
            tap,
            hn::Add(hn::LoadU(d, a + k * lanes), hn::LoadU(d, b + k * lanes)),
            sums[k]);
    }
  };
  hn::Vec<decltype(d)> sums[kChains];
  size_t t = 0;
  for (; t + kChains * lanes <= count; t += kChains * lanes) {
    at(x + t, sums, kChains);
    for (size_t k = 0; k < kChains; ++k)
      hn::StoreU(sums[k], d, out + t + k * lanes);
  }
  for (; t < count; t += lanes) {
    at(x + t, sums, 1);
    store(d, sums[0], out, t, count);
  }
}

void HalfBand(const double *side, size_t sides, const double *centre,
              const double *other, size_t count, double *out) {
  const hn::ScalableTag<double> d;
  const size_t lanes = hn::Lanes(d);
  const auto half = hn::Set(d, 0.5);
  const auto at = [&](size_t m, auto &sums, size_t chains) {
    for (size_t k = 0; k < chains; ++k)
      sums[k] = hn::Mul(half, hn::LoadU(d, centre + m + k * lanes));
    for (size_t i = 0; i < sides; ++i) {
      const auto tap = hn::Set(d, side[i]);
      const double *a = other + m - i, *b = other + m + i + 1;
      for (size_t k = 0; k < chains; ++k)
        sums[k] = hn::MulAdd(
            tap,
            hn::Add(hn::LoadU(d, a + k * lanes), hn::LoadU(d, b + k * lanes)),
            sums[k]);
    }
  };
  hn::Vec<decltype(d)> sums[kChains];
  size_t m = 0;
  for (; m + kChains * lanes <= count; m += kChains * lanes) {
    at(m, sums, kChains);
    for (size_t k = 0; k < kChains; ++k)
      hn::StoreU(sums[k], d, out + m + k * lanes);
  }
  for (; m < count; m += lanes) {
    at(m, sums, 1);
    store(d, sums[0], out, m, count);
  }
}

} // namespace HWY_NAMESPACE
} // namespace fir_kernels
HWY_AFTER_NAMESPACE();

#if HWY_ONCE
namespace fir_kernels {
HWY_EXPORT(Symmetric);
HWY_EXPORT(HalfBand);

void symmetric(const double *taps, size_t half, const double *x, size_t count,
               double *out) {
  HWY_DYNAMIC_DISPATCH(Symmetric)(taps, half, x, count, out);
}

void halfBand(const double *side, size_t sides, const double *centre,
              const double *other, size_t count, double *out) {
  HWY_DYNAMIC_DISPATCH(HalfBand)(side, sides, centre, other, count, out);
}
} // namespace fir_kernels
#endif
