#include "Simd.h"

namespace bench {
namespace {
bool always() { return true; }

void fir(const double *x, const double *reversed, size_t taps, double *y,
         size_t frames) {
  for (size_t n = 0; n < frames; ++n) {
    const double *window = x + n - (taps - 1);
    double sum = 0;
    for (size_t k = 0; k < taps; ++k) {
      sum += reversed[k] * window[k];
    }
    y[n] = sum;
  }
}

void biquad(const double *in, double *out, size_t frames,
            const double *coefficients, double *state, size_t sections) {
  for (size_t n = 0; n < frames; ++n) {
    for (size_t channel = 0; channel < 2; ++channel) {
      double v = in[2 * n + channel];
      for (size_t s = 0; s < sections; ++s) {
        const double *c = coefficients + 10 * s + channel;
        double *z = state + 4 * s + channel;
        const double y = c[0] * v + z[0];
        z[0] = c[2] * v - c[6] * y + z[2];
        z[2] = c[4] * v - c[8] * y;
        v = y;
      }
      out[2 * n + channel] = v;
    }
  }
}
} // namespace

SimdKernels scalarKernels() { return {"scalar reference", always, fir, biquad}; }

std::vector<SimdKernels> simdKernels() {
  std::vector<SimdKernels> all{scalarKernels()};
#if defined(__x86_64__)
  all.push_back(sse2Kernels());
  all.push_back(avx2Kernels());
  all.push_back(experimentalSse2Kernels());
  all.push_back(experimentalAvx2Kernels());
#elif defined(__aarch64__)
  all.push_back(neonKernels());
  all.push_back(experimentalNeonKernels());
#endif
#ifdef KALINKA_BENCH_HIGHWAY
  all.push_back(highwayKernels());
  all.push_back(highwayPipelinedKernels());
#endif
  return all;
}

} // namespace bench
