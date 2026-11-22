#include "dsp/HighwayTargets.h"
#include "dsp/peq/RoomEq.h"

#include <limits>

#undef HWY_TARGET_INCLUDE
#define HWY_TARGET_INCLUDE "dsp/peq/PeqKernels.cpp"
#include <hwy/foreach_target.h>
#include <hwy/highway.h>

HWY_BEFORE_NAMESPACE();
namespace peq {
namespace HWY_NAMESPACE {
namespace hn = hwy::HWY_NAMESPACE;

// Each section works one frame behind the preceding one. Drain only real
// samples so the history is identical across arbitrary read boundaries.
template <size_t S>
void group(const Section *sections, State *state, double *left, double *right,
           size_t frames) {
  const hn::FixedTag<double, 2> d;
  using V = hn::Vec<decltype(d)>;
  V z1[S], z2[S], delay[S];
  for (size_t s = 0; s < S; ++s) {
    z1[s] = hn::LoadU(d, state[s].z1.data());
    z2[s] = hn::LoadU(d, state[s].z2.data());
    delay[s] = hn::Zero(d);
  }
  for (size_t t = 0; t < frames + S - 1; ++t) {
    if (t < frames) {
      const double in[]{left[t], right ? right[t] : 0.0};
      delay[0] = hn::LoadU(d, in);
    }
    for (size_t s = S; s-- > 0;) {
      if ((t < S - 1 || t >= frames) && (s > t || t - s >= frames))
        continue;
      const Section &c = sections[s];
      const V x = delay[s];
      const V y = hn::MulAdd(hn::LoadU(d, c.b0.data()), x, z1[s]);
      z1[s] = hn::NegMulAdd(hn::LoadU(d, c.a1.data()), y,
                            hn::MulAdd(hn::LoadU(d, c.b1.data()), x, z2[s]));
      z2[s] = hn::NegMulAdd(hn::LoadU(d, c.a2.data()), y,
                            hn::Mul(hn::LoadU(d, c.b2.data()), x));
#if !defined(__SSE2__) && !defined(__aarch64__)
      // Without a hardware flush mode, keep subnormals out of the feedback.
      const auto minimum = hn::Set(d, std::numeric_limits<double>::min());
      z1[s] = hn::IfThenZeroElse(hn::Lt(hn::Abs(z1[s]), minimum), z1[s]);
      z2[s] = hn::IfThenZeroElse(hn::Lt(hn::Abs(z2[s]), minimum), z2[s]);
#endif
      if (s + 1 < S) {
        delay[s + 1] = y;
      } else {
        double out[2];
        hn::StoreU(y, d, out);
        left[t - s] = out[0];
        if (right)
          right[t - s] = out[1];
      }
    }
  }
  for (size_t s = 0; s < S; ++s) {
    hn::StoreU(z1[s], d, state[s].z1.data());
    hn::StoreU(z2[s], d, state[s].z2.data());
  }
}

void Filter(const Cascade &cascade, State *state, double *left, double *right,
            size_t frames) {
  size_t at = 0;
  const auto groups = [&]<size_t N>() {
    while (cascade.count - at >= N) {
      group<N>(cascade.sections.data() + at, state + at, left, right, frames);
      at += N;
    }
  };
  groups.template operator()<10>();
  groups.template operator()<4>();
  groups.template operator()<2>();
  groups.template operator()<1>();
}
} // namespace HWY_NAMESPACE
} // namespace peq
HWY_AFTER_NAMESPACE();

#if HWY_ONCE
#include "dsp/FlushFilterTails.h"

namespace peq {
HWY_EXPORT(Filter);
void filter(const Cascade &cascade, State *state, double *left, double *right,
            size_t frames) {
  [[maybe_unused]] dsp::FlushFilterTails flush;
#if defined(__aarch64__)
  if (cascade.parallel) {
    filterParallel(*cascade.parallel, state, left, right, frames);
    return;
  }
#endif
  HWY_DYNAMIC_DISPATCH(Filter)(cascade, state, left, right, frames);
}
} // namespace peq
#endif
