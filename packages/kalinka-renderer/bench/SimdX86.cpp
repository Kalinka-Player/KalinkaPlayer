#include "Simd.h"

#include <immintrin.h>

namespace bench {
namespace {
bool always() { return true; }

bool hasAvx2() {
  return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
}

double sum(__m128d v) {
  return _mm_cvtsd_f64(_mm_add_sd(v, _mm_unpackhi_pd(v, v)));
}

void firSse2(const double *x, const double *reversed, size_t taps, double *y,
             size_t frames) {
  for (size_t n = 0; n < frames; ++n) {
    const double *window = x + n - (taps - 1);
    __m128d a = _mm_setzero_pd(), b = _mm_setzero_pd();
    size_t k = 0;
    for (; k + 4 <= taps; k += 4) {
      a = _mm_add_pd(a, _mm_mul_pd(_mm_loadu_pd(reversed + k),
                                   _mm_loadu_pd(window + k)));
      b = _mm_add_pd(b, _mm_mul_pd(_mm_loadu_pd(reversed + k + 2),
                                   _mm_loadu_pd(window + k + 2)));
    }
    double tail = 0;
    for (; k < taps; ++k) {
      tail += reversed[k] * window[k];
    }
    y[n] = sum(_mm_add_pd(a, b)) + tail;
  }
}

void biquadSse2(const double *in, double *out, size_t frames,
                const double *coefficients, double *state, size_t sections) {
  for (size_t n = 0; n < frames; ++n) {
    __m128d v = _mm_loadu_pd(in + 2 * n);
    for (size_t s = 0; s < sections; ++s) {
      const double *c = coefficients + 10 * s;
      double *z = state + 4 * s;
      const __m128d z1 = _mm_loadu_pd(z), z2 = _mm_loadu_pd(z + 2);
      const __m128d yv = _mm_add_pd(_mm_mul_pd(_mm_loadu_pd(c), v), z1);
      _mm_storeu_pd(z, _mm_add_pd(_mm_sub_pd(_mm_mul_pd(_mm_loadu_pd(c + 2), v),
                                             _mm_mul_pd(_mm_loadu_pd(c + 6), yv)),
                                  z2));
      _mm_storeu_pd(z + 2, _mm_sub_pd(_mm_mul_pd(_mm_loadu_pd(c + 4), v),
                                      _mm_mul_pd(_mm_loadu_pd(c + 8), yv)));
      v = yv;
    }
    _mm_storeu_pd(out + 2 * n, v);
  }
}

__attribute__((target("avx2,fma"))) void
firAvx2(const double *x, const double *reversed, size_t taps, double *y,
        size_t frames) {
  for (size_t n = 0; n < frames; ++n) {
    const double *window = x + n - (taps - 1);
    __m256d a = _mm256_setzero_pd(), b = _mm256_setzero_pd();
    size_t k = 0;
    for (; k + 8 <= taps; k += 8) {
      a = _mm256_fmadd_pd(_mm256_loadu_pd(reversed + k),
                          _mm256_loadu_pd(window + k), a);
      b = _mm256_fmadd_pd(_mm256_loadu_pd(reversed + k + 4),
                          _mm256_loadu_pd(window + k + 4), b);
    }
    const __m256d both = _mm256_add_pd(a, b);
    __m128d half = _mm_add_pd(_mm256_castpd256_pd128(both),
                              _mm256_extractf128_pd(both, 1));
    double tail = 0;
    for (; k < taps; ++k) {
      tail += reversed[k] * window[k];
    }
    y[n] = _mm_cvtsd_f64(_mm_add_sd(half, _mm_unpackhi_pd(half, half))) + tail;
  }
}

__attribute__((target("avx2,fma"))) void
biquadAvx2(const double *in, double *out, size_t frames,
           const double *coefficients, double *state, size_t sections) {
  for (size_t n = 0; n < frames; ++n) {
    __m128d v = _mm_loadu_pd(in + 2 * n);
    for (size_t s = 0; s < sections; ++s) {
      const double *c = coefficients + 10 * s;
      double *z = state + 4 * s;
      const __m128d yv = _mm_fmadd_pd(_mm_loadu_pd(c), v, _mm_loadu_pd(z));
      _mm_storeu_pd(z, _mm_fnmadd_pd(_mm_loadu_pd(c + 6), yv,
                                     _mm_fmadd_pd(_mm_loadu_pd(c + 2), v,
                                                  _mm_loadu_pd(z + 2))));
      _mm_storeu_pd(z + 2, _mm_fnmadd_pd(_mm_loadu_pd(c + 8), yv,
                                         _mm_mul_pd(_mm_loadu_pd(c + 4), v)));
      v = yv;
    }
    _mm_storeu_pd(out + 2 * n, v);
  }
}
} // namespace

SimdKernels sse2Kernels() {
  return {"intrinsics SSE2", always, firSse2, biquadSse2};
}

SimdKernels avx2Kernels() {
  return {"intrinsics AVX2+FMA", hasAvx2, firAvx2, biquadAvx2};
}

} // namespace bench
