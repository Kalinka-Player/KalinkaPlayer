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

using D = hn::ScalableTag<double>;
using V = hn::Vec<D>;

/// Vectors summed at once: enough to hide a multiply-add's latency.
constexpr size_t kChains = 4;

/// Whether a group's chains share their loads. It takes vectors of 2 lanes,
/// and NEON's 32 registers to hold what is shared.
constexpr bool kSharesLoads =
    HWY_ARCH_ARM_A64 && !HWY_HAVE_SCALABLE && hn::MaxLanes(D()) == 2;
/// The fewest taps sharing saves loads at: with fewer, a group loads as many
/// either way.
constexpr size_t kSharedFrom = 4;

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

/// Adds one tap to each of @p chains sums from loads of its own: of the
/// samples at @p older and @p newer, a vector further on for each chain.
HWY_INLINE void AddTap(D d, double tap, const double *older,
                       const double *newer, V *sums, size_t chains) {
  const size_t lanes = hn::Lanes(d);
  const auto weight = hn::Set(d, tap);
  for (size_t k = 0; k < chains; ++k)
    sums[k] = hn::MulAdd(weight,
                         hn::Add(hn::LoadU(d, older + k * lanes),
                                 hn::LoadU(d, newer + k * lanes)),
                         sums[k]);
}

/**
 * @brief The loads kChains chains of 2 lanes share, as [odd tap][slot]. At
 * tap j + 2 chain k + 1 reads the older vector chain k read at tap j, and
 * chain k the newer one chain k + 1 did, so past its first pair of taps a
 * group loads 4 vectors a pair, not 16.
 *
 * At pair p, chain k's vector is in slot (k - p) mod kChains of the older
 * samples' ring and (k + p) mod kChains of the newer ones'. The rings turn a
 * slot a pair, so nothing moves between registers.
 */
using Ring = V[2][kChains];

/// Adds a pair of taps to every chain's sum; R is the pair modulo kChains.
template <size_t R>
HWY_INLINE void AddPair(D d, const double *taps, const Ring &older,
                        const Ring &newer, V *sums) {
  for (size_t odd = 0; odd < 2; ++odd) {
    const auto tap = hn::Set(d, taps[odd]);
    for (size_t k = 0; k < kChains; ++k)
      sums[k] = hn::MulAdd(tap,
                           hn::Add(older[odd][(k + kChains - R) % kChains],
                                   newer[odd][(k + R) % kChains]),
                           sums[k]);
  }
}

/// Loads what a pair needs that the one before had not: chain 0's older
/// vectors, at @p back, and the last chain's newer ones, from @p ahead.
template <size_t R>
HWY_INLINE void Turn(D d, const double *back, const double *ahead, Ring &older,
                     Ring &newer) {
  const size_t last = (kChains - 1) * hn::Lanes(d);
  for (size_t odd = 0; odd < 2; ++odd) {
    older[odd][(kChains - R) % kChains] = hn::LoadU(d, back - odd);
    newer[odd][(R + kChains - 1) % kChains] = hn::LoadU(d, ahead + odd + last);
  }
}

/// Pair @p p of taps, whose number modulo kChains is R, onto the sums.
template <size_t R>
HWY_INLINE void Pair(D d, const double *taps, const double *back,
                     const double *ahead, size_t p, Ring &older, Ring &newer,
                     V *sums) {
  Turn<R>(d, back - 2 * p, ahead + 2 * p, older, newer);
  AddPair<R>(d, taps + 2 * p, older, newer, sums);
}

/// One group of kChains vectors of outputs, as Mirrored() sums them, with
/// the chains sharing their loads. Takes @p count of at least 2.
HWY_INLINE void SharedGroup(D d, V first, const double *mid, const double *taps,
                            size_t count, const double *back,
                            const double *ahead, double *out) {
  const size_t lanes = hn::Lanes(d);
  V sums[kChains];
  Ring older, newer;
  for (size_t k = 0; k < kChains; ++k) {
    sums[k] = hn::Mul(first, hn::LoadU(d, mid + k * lanes));
    for (size_t odd = 0; odd < 2; ++odd) {
      older[odd][k] = hn::LoadU(d, back - odd + k * lanes);
      newer[odd][k] = hn::LoadU(d, ahead + odd + k * lanes);
    }
  }
  const size_t pairs = count / 2;
  AddPair<0>(d, taps, older, newer, sums);
  size_t p = 1;
  for (; p + kChains <= pairs; p += kChains) {
    Pair<1>(d, taps, back, ahead, p, older, newer, sums);
    Pair<2>(d, taps, back, ahead, p + 1, older, newer, sums);
    Pair<3>(d, taps, back, ahead, p + 2, older, newer, sums);
    Pair<0>(d, taps, back, ahead, p + 3, older, newer, sums);
  }
  if (p < pairs)
    Pair<1>(d, taps, back, ahead, p, older, newer, sums);
  if (p + 1 < pairs)
    Pair<2>(d, taps, back, ahead, p + 1, older, newer, sums);
  if (p + 2 < pairs)
    Pair<3>(d, taps, back, ahead, p + 2, older, newer, sums);
  if (count % 2) {
    const size_t last = count - 1;
    AddTap(d, taps[last], back - last, ahead + last, sums, kChains);
  }
  for (size_t k = 0; k < kChains; ++k)
    hn::StoreU(sums[k], d, out + k * lanes);
}

/// Mirrored()'s whole groups with the chains sharing their loads. Apart from
/// it, so the registers this takes are saved only when it runs.
/// @return The outputs made.
static HWY_NOINLINE size_t SharedGroups(double centre, const double *mid,
                                        const double *taps, size_t count,
                                        const double *back, const double *ahead,
                                        size_t outputs, double *out) {
  const D d;
  const size_t group = kChains * hn::Lanes(d);
  const auto first = hn::Set(d, centre);
  size_t t = 0;
  for (; t + group <= outputs; t += group)
    SharedGroup(d, first, mid + t, taps, count, back + t, ahead + t, out + t);
  return t;
}

/// What both filters are: out[t] = centre mid[t] + sum over j < count of
/// taps[j] (back[t - j] + ahead[t + j]), for t < outputs. Inlined, because
/// the plain loop is faster in a filter that knows how its inputs lie.
HWY_INLINE void Mirrored(double centre, const double *mid, const double *taps,
                         size_t count, const double *back, const double *ahead,
                         size_t outputs, double *out) {
  const D d;
  const size_t lanes = hn::Lanes(d);
  const auto first = hn::Set(d, centre);
  // Every path adds an output's terms in the same order, to the same sum.
  const auto at = [&](size_t t, V *sums, size_t chains) {
    for (size_t k = 0; k < chains; ++k)
      sums[k] = hn::Mul(first, hn::LoadU(d, mid + t + k * lanes));
    for (size_t j = 0; j < count; ++j)
      AddTap(d, taps[j], back + t - j, ahead + t + j, sums, chains);
  };
  V sums[kChains];
  size_t t = 0;
  if (kSharesLoads && count >= kSharedFrom) {
    t = SharedGroups(centre, mid, taps, count, back, ahead, outputs, out);
  } else {
    for (; t + kChains * lanes <= outputs; t += kChains * lanes) {
      at(t, sums, kChains);
      for (size_t k = 0; k < kChains; ++k)
        hn::StoreU(sums[k], d, out + t + k * lanes);
    }
  }
  for (; t < outputs; t += lanes) {
    at(t, sums, 1);
    store(d, sums[0], out, t, outputs);
  }
}

void Symmetric(const double *taps, size_t half, const double *x, size_t count,
               double *out) {
  const size_t centre = half - 1;
  Mirrored(taps[centre], x - centre, taps, centre, x, x - 2 * centre, count,
           out);
}

void HalfBand(const double *side, size_t sides, const double *centre,
              const double *other, size_t count, double *out) {
  Mirrored(0.5, centre, side, sides, other, other + 1, count, out);
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
