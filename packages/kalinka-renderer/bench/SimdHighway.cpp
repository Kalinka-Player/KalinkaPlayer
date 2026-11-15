#include "Simd.h"

#include <string>

#undef HWY_TARGET_INCLUDE
#define HWY_TARGET_INCLUDE "SimdHighway.cpp"
#include <hwy/foreach_target.h> // IWYU pragma: keep
#include <hwy/highway.h>

HWY_BEFORE_NAMESPACE();
namespace bench {
namespace HWY_NAMESPACE {
namespace hn = hwy::HWY_NAMESPACE;

void Fir(const double *x, const double *reversed, size_t taps, double *y,
         size_t frames) {
  const hn::ScalableTag<double> d;
  const size_t lanes = hn::Lanes(d);
  for (size_t n = 0; n < frames; ++n) {
    const double *window = x + n - (taps - 1);
    auto a = hn::Zero(d), b = hn::Zero(d);
    size_t k = 0;
    for (; k + 2 * lanes <= taps; k += 2 * lanes) {
      a = hn::MulAdd(hn::LoadU(d, reversed + k), hn::LoadU(d, window + k), a);
      b = hn::MulAdd(hn::LoadU(d, reversed + k + lanes),
                     hn::LoadU(d, window + k + lanes), b);
    }
    double tail = 0;
    for (; k < taps; ++k) {
      tail += reversed[k] * window[k];
    }
#if HWY_MAJOR > 1 || HWY_MINOR >= 1
    y[n] = hn::ReduceSum(d, hn::Add(a, b)) + tail;
#else
    // Bookworm's Highway 1.0 predates ReduceSum.
    y[n] = hn::GetLane(hn::SumOfLanes(d, hn::Add(a, b))) + tail;
#endif
  }
}

void Biquad(const double *in, double *out, size_t frames,
            const double *coefficients, double *state, size_t sections) {
  const hn::FixedTag<double, 2> d;
  for (size_t n = 0; n < frames; ++n) {
    auto v = hn::LoadU(d, in + 2 * n);
    for (size_t s = 0; s < sections; ++s) {
      const double *c = coefficients + 10 * s;
      double *z = state + 4 * s;
      const auto yv = hn::MulAdd(hn::LoadU(d, c), v, hn::LoadU(d, z));
      hn::StoreU(hn::NegMulAdd(hn::LoadU(d, c + 6), yv,
                               hn::MulAdd(hn::LoadU(d, c + 2), v,
                                          hn::LoadU(d, z + 2))),
                 d, z);
      hn::StoreU(hn::NegMulAdd(hn::LoadU(d, c + 8), yv,
                               hn::Mul(hn::LoadU(d, c + 4), v)),
                 d, z + 2);
      v = yv;
    }
    hn::StoreU(v, d, out + 2 * n);
  }
}

const char *Target() { return hwy::TargetName(HWY_TARGET); }

} // namespace HWY_NAMESPACE
} // namespace bench
HWY_AFTER_NAMESPACE();

#if HWY_ONCE
namespace bench {
namespace {
HWY_EXPORT(Fir);
HWY_EXPORT(Biquad);
HWY_EXPORT(Target);

bool always() { return true; }

void fir(const double *x, const double *reversed, size_t taps, double *y,
         size_t frames) {
  HWY_DYNAMIC_DISPATCH(Fir)(x, reversed, taps, y, frames);
}

void biquad(const double *in, double *out, size_t frames,
            const double *coefficients, double *state, size_t sections) {
  HWY_DYNAMIC_DISPATCH(Biquad)(in, out, frames, coefficients, state, sections);
}
} // namespace

SimdKernels highwayKernels() {
  static const std::string name = std::string("Highway, dispatched to ") +
                                  HWY_DYNAMIC_DISPATCH(Target)();
  return {name.c_str(), always, fir, biquad};
}

} // namespace bench
#endif
