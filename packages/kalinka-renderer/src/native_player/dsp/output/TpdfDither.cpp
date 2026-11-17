#include "dsp/output/TpdfDither.h"

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
