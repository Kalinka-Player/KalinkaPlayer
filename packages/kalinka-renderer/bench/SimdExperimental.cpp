// Built once per instruction set: std::experimental::simd sizes at compile time.
#include "Simd.h"

#include <experimental/simd>

namespace bench {
namespace {
namespace stdx = std::experimental;
using Wide = stdx::native_simd<double>;
using Pair = stdx::fixed_size_simd<double, 2>;

bool supported() {
#if defined(__AVX2__)
  return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#else
  return true;
#endif
}

void fir(const double *x, const double *reversed, size_t taps, double *y,
         size_t frames) {
  for (size_t n = 0; n < frames; ++n) {
    const double *window = x + n - (taps - 1);
    Wide a = 0, b = 0;
    size_t k = 0;
    for (; k + 2 * Wide::size() <= taps; k += 2 * Wide::size()) {
      a += Wide(reversed + k, stdx::element_aligned) *
           Wide(window + k, stdx::element_aligned);
      b += Wide(reversed + k + Wide::size(), stdx::element_aligned) *
           Wide(window + k + Wide::size(), stdx::element_aligned);
    }
    double tail = 0;
    for (; k < taps; ++k) {
      tail += reversed[k] * window[k];
    }
    y[n] = stdx::reduce(a + b) + tail;
  }
}

void biquad(const double *in, double *out, size_t frames,
            const double *coefficients, double *state, size_t sections) {
  for (size_t n = 0; n < frames; ++n) {
    Pair v(in + 2 * n, stdx::element_aligned);
    for (size_t s = 0; s < sections; ++s) {
      const double *c = coefficients + 10 * s;
      double *z = state + 4 * s;
      const Pair y = Pair(c, stdx::element_aligned) * v +
                     Pair(z, stdx::element_aligned);
      const Pair z1 = Pair(c + 2, stdx::element_aligned) * v -
                      Pair(c + 6, stdx::element_aligned) * y +
                      Pair(z + 2, stdx::element_aligned);
      const Pair z2 = Pair(c + 4, stdx::element_aligned) * v -
                      Pair(c + 8, stdx::element_aligned) * y;
      z1.copy_to(z, stdx::element_aligned);
      z2.copy_to(z + 2, stdx::element_aligned);
      v = y;
    }
    v.copy_to(out + 2 * n, stdx::element_aligned);
  }
}
} // namespace

SimdKernels KALINKA_BENCH_EXPERIMENTAL() {
  return {KALINKA_BENCH_EXPERIMENTAL_NAME, supported, fir, biquad};
}

} // namespace bench
