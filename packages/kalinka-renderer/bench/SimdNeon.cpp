#include "Simd.h"

#include <arm_neon.h>

namespace bench {
namespace {
bool always() { return true; }

void fir(const double *x, const double *reversed, size_t taps, double *y,
         size_t frames) {
  for (size_t n = 0; n < frames; ++n) {
    const double *window = x + n - (taps - 1);
    float64x2_t a = vdupq_n_f64(0), b = vdupq_n_f64(0);
    size_t k = 0;
    for (; k + 4 <= taps; k += 4) {
      a = vfmaq_f64(a, vld1q_f64(reversed + k), vld1q_f64(window + k));
      b = vfmaq_f64(b, vld1q_f64(reversed + k + 2), vld1q_f64(window + k + 2));
    }
    double tail = 0;
    for (; k < taps; ++k) {
      tail += reversed[k] * window[k];
    }
    y[n] = vaddvq_f64(vaddq_f64(a, b)) + tail;
  }
}

void biquad(const double *in, double *out, size_t frames,
            const double *coefficients, double *state, size_t sections) {
  for (size_t n = 0; n < frames; ++n) {
    float64x2_t v = vld1q_f64(in + 2 * n);
    for (size_t s = 0; s < sections; ++s) {
      const double *c = coefficients + 10 * s;
      double *z = state + 4 * s;
      const float64x2_t yv = vfmaq_f64(vld1q_f64(z), vld1q_f64(c), v);
      vst1q_f64(z, vfmsq_f64(vfmaq_f64(vld1q_f64(z + 2), vld1q_f64(c + 2), v),
                             vld1q_f64(c + 6), yv));
      vst1q_f64(z + 2, vfmsq_f64(vmulq_f64(vld1q_f64(c + 4), v),
                                 vld1q_f64(c + 8), yv));
      v = yv;
    }
    vst1q_f64(out + 2 * n, v);
  }
}
} // namespace

SimdKernels neonKernels() { return {"intrinsics NEON", always, fir, biquad}; }

} // namespace bench
