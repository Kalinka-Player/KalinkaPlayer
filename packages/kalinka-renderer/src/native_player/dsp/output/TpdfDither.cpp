#include "dsp/output/TpdfDither.h"
#include "dsp/output/OutputKernels.h"

#if defined(__aarch64__)
#include <arm_neon.h>
#include <bit>
#endif

namespace {
uint64_t rotl(uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }

/// Spreads one seed over the generator's state, as xoshiro's authors advise.
uint64_t splitMix(uint64_t &x) {
  uint64_t z = (x += 0x9e3779b97f4a7c15ull);
  z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
  z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
  return z ^ (z >> 31);
}

constexpr double kUnit = 1.0 / double(1ull << 32);

uint64_t next(std::array<uint64_t, 4> &state) {
  const uint64_t result = state[0] + state[3];
  const uint64_t t = state[1] << 17;
  state[2] ^= state[0];
  state[3] ^= state[1];
  state[1] ^= state[2];
  state[0] ^= state[3];
  state[2] ^= t;
  state[3] = rotl(state[3], 45);
  return result;
}

/// The two halves of one draw are the two uniforms.
double triangular(uint64_t draw) {
  return double(int64_t(draw >> 32) - int64_t(draw & 0xffffffffu)) * kUnit;
}

#if defined(__aarch64__)
// One xoshiro generator per lane, with the four state words in four registers.
float64x2_t drawPair(uint64x2x4_t &state) {
  auto &[a, b, c, d] = state.val;
  const auto draw = vaddq_u64(a, d);
  const auto t = vshlq_n_u64(b, 17);
  c = veorq_u64(c, a);
  d = veorq_u64(d, b);
  b = veorq_u64(b, c);
  a = veorq_u64(a, d);
  c = veorq_u64(c, t);
  d = vsriq_n_u64(vshlq_n_u64(d, 45), d, 19);
  const auto difference = vsubq_u64(vshrq_n_u64(draw, 32),
                                    vandq_u64(draw, vdupq_n_u64(0xffffffffu)));
  return vcvtq_n_f64_s64(vreinterpretq_s64_u64(difference), 32);
}
#endif
} // namespace

TpdfDither::TpdfDither(uint64_t seed) {
  for (auto &generator : generators)
    for (auto &word : generator)
      word = splitMix(seed);
}

void TpdfDither::fill(double *out, size_t count) {
  // Locals, so the four states stay in registers across the loop.
  Generator a = generators[0], b = generators[1], c = generators[2],
            d = generators[3];
  size_t i = 0;
  for (; i + 4 <= count; i += 4) {
    out[i] = triangular(next(a));
    out[i + 1] = triangular(next(b));
    out[i + 2] = triangular(next(c));
    out[i + 3] = triangular(next(d));
  }
  for (; i < count; ++i)
    out[i] = triangular(next(a));
  generators = {a, b, c, d};
}

size_t TpdfDither::quantise(const double *in, size_t count, double from,
                            double step, double scale, int32_t low,
                            int32_t high, double *scratch, int32_t *out) {
  size_t i = 0, clipped = 0;
#if defined(__aarch64__)
  if (step == 0.0 && count >= 4) {
    auto words = std::bit_cast<std::array<uint64_t, 16>>(generators);
    auto first = vld4q_u64(words.data()), second = vld4q_u64(words.data() + 8);
    const auto lo = vdupq_n_f64(low), hi = vdupq_n_f64(high);
    const auto lower = vdupq_n_u64(uint64_t(int64_t(low)));
    const auto width = vdupq_n_u64(uint64_t(int64_t(high) - low));
    auto outside = vdupq_n_u64(0);
    const auto writePair = [&](size_t at, float64x2_t noise) {
      const auto signal = vmulq_n_f64(vld1q_f64(in + at), from);
      const auto value = vfmaq_n_f64(noise, signal, scale);
      const auto rounded = vreinterpretq_u64_s64(vcvtnq_s64_f64(value));
      // Unsigned subtraction detects either rail. FCVT rounds ties to even
      // and converts NaN to zero, which does not count as clipped.
      outside = vsubq_u64(outside, vcgtq_u64(vsubq_u64(rounded, lower), width));
      const auto bounded = vminnmq_f64(vmaxnmq_f64(value, lo), hi);
      vst1_s32(out + at, vmovn_s64(vcvtnq_s64_f64(bounded)));
    };
    for (; i + 4 <= count; i += 4) {
      const auto a = drawPair(first), b = drawPair(second);
      writePair(i, a);
      writePair(i + 2, b);
    }
    vst4q_u64(words.data(), first);
    vst4q_u64(words.data() + 8, second);
    generators = std::bit_cast<decltype(generators)>(words);
    clipped = vaddvq_u64(outside);
  }
#endif
  // fill() draws the final one to three samples from generator zero.
  if (i < count) {
    fill(scratch, count - i);
    clipped += output_kernels::quantise(in + i, count - i, from, step, scale,
                                        scratch, low, high, out + i);
  }
  return clipped;
}
