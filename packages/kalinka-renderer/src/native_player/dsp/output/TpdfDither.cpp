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
} // namespace

TpdfDither::TpdfDither(uint64_t seed) {
  for (auto &word : state)
    word = splitMix(seed);
}

uint64_t TpdfDither::next() {
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

void TpdfDither::fill(double *out, size_t count) {
  for (size_t i = 0; i < count; ++i) {
    // The two halves of one draw are the two uniforms.
    const uint64_t draw = next();
    out[i] = (double(draw >> 32) - double(draw & 0xffffffffu)) * kUnit;
  }
}
