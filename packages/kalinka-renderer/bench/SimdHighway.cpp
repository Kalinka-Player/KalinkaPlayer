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
    y[n] = hn::ReduceSum(d, hn::Add(a, b)) + tail;
  }
}

// One transposed direct form II section on a stereo frame.
template <class D>
HWY_INLINE hn::Vec<D> Section(D d, const double *c, hn::Vec<D> v,
                              hn::Vec<D> &z1, hn::Vec<D> &z2) {
  const auto y = hn::MulAdd(hn::LoadU(d, c), v, z1);
  z1 = hn::NegMulAdd(hn::LoadU(d, c + 6), y,
                     hn::MulAdd(hn::LoadU(d, c + 2), v, z2));
  z2 = hn::NegMulAdd(hn::LoadU(d, c + 8), y, hn::Mul(hn::LoadU(d, c + 4), v));
  return y;
}

void Biquad(const double *in, double *out, size_t frames,
            const double *coefficients, double *state, size_t sections) {
  const hn::FixedTag<double, 2> d;
  for (size_t n = 0; n < frames; ++n) {
    auto v = hn::LoadU(d, in + 2 * n);
    for (size_t s = 0; s < sections; ++s) {
      double *z = state + 4 * s;
      auto z1 = hn::LoadU(d, z), z2 = hn::LoadU(d, z + 2);
      v = Section(d, coefficients + 10 * s, v, z1, z2);
      hn::StoreU(z1, d, z);
      hn::StoreU(z2, d, z + 2);
    }
    hn::StoreU(v, d, out + 2 * n);
  }
}

// Every SVE vector is sizeless, even at a fixed width, and none can sit in an
// array; nor can a scalable one on any other target.
#define KALINKA_BENCH_VECTOR_ARRAYS                                            \
  (!HWY_HAVE_SCALABLE &&                                                       \
   !(HWY_TARGET & (HWY_SVE | HWY_SVE2 | HWY_SVE_256 | HWY_SVE2_128)))

bool Pipelines() { return KALINKA_BENCH_VECTOR_ARRAYS; }

#if KALINKA_BENCH_VECTOR_ARRAYS
// In step t, section s works on frame t - s, so no section of a step waits on
// another. A section steps only over the call's own frames, which leaves its
// state as the plain cascade would, and the output lines up with the input.
template <size_t S>
void BiquadPipelinedFixed(const double *in, double *out, size_t frames,
                          const double *coefficients, double *state) {
  const hn::FixedTag<double, 2> d;
  using V = hn::Vec<decltype(d)>;
  V z1[S], z2[S], waiting[S];
  for (size_t s = 0; s < S; ++s) {
    z1[s] = hn::LoadU(d, state + 4 * s);
    z2[s] = hn::LoadU(d, state + 4 * s + 2);
    waiting[s] = hn::Zero(d);
  }
  for (size_t t = 0; t < frames + S - 1; ++t) {
    if (t < frames) {
      waiting[0] = hn::LoadU(d, in + 2 * t);
    }
    const bool filling = t < S - 1 || t >= frames;
    for (size_t s = S; s-- > 0;) {
      if (filling && (s > t || t - s >= frames)) {
        continue;
      }
      const V y = Section(d, coefficients + 10 * s, waiting[s], z1[s], z2[s]);
      if (s + 1 < S) {
        waiting[s + 1] = y;
      } else {
        hn::StoreU(y, d, out + 2 * (t - s));
      }
    }
  }
  for (size_t s = 0; s < S; ++s) {
    hn::StoreU(z1[s], d, state + 4 * s);
    hn::StoreU(z2[s], d, state + 4 * s + 2);
  }
}
#endif

void BiquadPipelined(const double *in, double *out, size_t frames,
                     const double *coefficients, double *state,
                     size_t sections) {
#if KALINKA_BENCH_VECTOR_ARRAYS
  if (sections == 10) {
    BiquadPipelinedFixed<10>(in, out, frames, coefficients, state);
    return;
  }
#endif
  Biquad(in, out, frames, coefficients, state, sections);
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
HWY_EXPORT(BiquadPipelined);
HWY_EXPORT(Pipelines);
HWY_EXPORT(Target);

bool always() { return true; }

bool pipelines() { return HWY_DYNAMIC_DISPATCH(Pipelines)(); }

void fir(const double *x, const double *reversed, size_t taps, double *y,
         size_t frames) {
  HWY_DYNAMIC_DISPATCH(Fir)(x, reversed, taps, y, frames);
}

void biquad(const double *in, double *out, size_t frames,
            const double *coefficients, double *state, size_t sections) {
  HWY_DYNAMIC_DISPATCH(Biquad)(in, out, frames, coefficients, state, sections);
}

void biquadPipelined(const double *in, double *out, size_t frames,
                     const double *coefficients, double *state,
                     size_t sections) {
  HWY_DYNAMIC_DISPATCH(BiquadPipelined)(in, out, frames, coefficients, state,
                                        sections);
}
} // namespace

SimdKernels highwayKernels() {
  static const std::string name =
      std::string("Highway, dispatched to ") + HWY_DYNAMIC_DISPATCH(Target)();
  return {name.c_str(), always, fir, biquad};
}

SimdKernels highwayPipelinedKernels() {
  static const std::string name = std::string("Highway, sections pipelined, ") +
                                  HWY_DYNAMIC_DISPATCH(Target)();
  return {name.c_str(), pipelines, fir, biquadPipelined};
}

} // namespace bench
#endif
