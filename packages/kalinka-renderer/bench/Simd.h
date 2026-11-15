#ifndef BENCH_SIMD_H
#define BENCH_SIMD_H

#include <cstddef>
#include <vector>

namespace bench {

/**
 * @brief One SIMD mechanism's version of the two kernels the mechanisms are
 * compared on: a float64 FIR dot product and a stereo float64 biquad cascade.
 */
struct SimdKernels {
  const char *name;
  bool (*available)();

  /// y[n] = sum of reversed[k] * x[n - taps + 1 + k]; x holds taps - 1
  /// frames of history before x[0].
  void (*fir)(const double *x, const double *reversed, size_t taps, double *y,
              size_t frames);

  /**
   * @brief Interleaved stereo through @p sections biquads in transposed direct
   * form II. Per section, @p coefficients holds b0, b1, b2, a1, a2 as left and
   * right pairs, and @p state holds z1 and z2 the same way.
   */
  void (*biquad)(const double *in, double *out, size_t frames,
                 const double *coefficients, double *state, size_t sections);
};

/// Every mechanism compiled for this architecture, the scalar reference first.
std::vector<SimdKernels> simdKernels();

SimdKernels scalarKernels();
#if defined(__x86_64__)
SimdKernels sse2Kernels();
SimdKernels avx2Kernels();
SimdKernels experimentalSse2Kernels();
SimdKernels experimentalAvx2Kernels();
#elif defined(__aarch64__)
SimdKernels neonKernels();
SimdKernels experimentalNeonKernels();
#endif
#ifdef KALINKA_BENCH_HIGHWAY
SimdKernels highwayKernels();
/**
 * @brief The biquad cascade with each section a frame behind the one before
 * it, so the sections of one step run side by side.
 * @note Pipelined for ten sections, the count the bench runs; any other count
 * runs the plain cascade. Not available on SVE, whose vectors cannot be held
 * in an array.
 */
SimdKernels highwayPipelinedKernels();
#endif

} // namespace bench

#endif
