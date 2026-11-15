// zldsp_fft has kernels for 128- and 256-bit vectors only.
#if defined(__x86_64__) || defined(__i386__)
#define HWY_DISABLED_TARGETS (~(HWY_SSE2 | HWY_SSE4 | HWY_AVX2))
#elif defined(__aarch64__)
#define HWY_DISABLED_TARGETS                                                   \
  (~(HWY_NEON_WITHOUT_AES | HWY_NEON | HWY_NEON_BF16))
#endif

#undef HWY_TARGET_INCLUDE
#define HWY_TARGET_INCLUDE "ZldspFft.cpp"
#include <hwy/foreach_target.h>

#include <hwy/highway.h>
#include <hwy/targets.h>
#include <zldsp_fft_rfft-inl.hpp>

#include "RealFft.h"

#include <algorithm>
#include <bit>
#include <complex>
#include <type_traits>

HWY_BEFORE_NAMESPACE();

namespace fft_bench::HWY_NAMESPACE {

/**
 * @brief zldsp_fft's real FFT for the Highway target this copy is compiled
 * for, in its interleaved layout (n/2 + 1 complex bins).
 *
 * @note zldsp_fft takes its inputs by non-const pointer; the benchmark's
 * input check is what shows it leaves them as they were.
 */
template <typename T> class ZldspFft final : public RealFft<T> {
public:
  explicit ZldspFft(size_t n) : n(n), plan(std::countr_zero(n)) {}

  void forward(const T *in, T *spectrum) override {
    plan.forward(const_cast<T *>(in),
                 reinterpret_cast<std::complex<T> *>(spectrum));
  }

  void inverse(const T *spectrum, T *out) override {
    plan.backward(
        reinterpret_cast<std::complex<T> *>(const_cast<T *>(spectrum)), out);
  }

  void toBins(const T *spectrum, T *bins) override {
    std::copy_n(spectrum, n + 2, bins);
  }

  std::string simd() const override { return hwy::TargetName(HWY_TARGET); }

private:
  const size_t n;
  zldsp::fft::HWY_NAMESPACE::RFFT<T> plan;
};

std::unique_ptr<RealFft<float>> planFloat(size_t n) {
  return std::make_unique<ZldspFft<float>>(n);
}

std::unique_ptr<RealFft<double>> planDouble(size_t n) {
  return std::make_unique<ZldspFft<double>>(n);
}

} // namespace fft_bench::HWY_NAMESPACE

HWY_AFTER_NAMESPACE();

#if HWY_ONCE

namespace fft_bench {

HWY_EXPORT(planFloat);
HWY_EXPORT(planDouble);

template <typename T> std::unique_ptr<RealFft<T>> planBestTarget(size_t n) {
  if constexpr (std::is_same_v<T, float>) {
    return HWY_DYNAMIC_DISPATCH(planFloat)(n);
  } else {
    return HWY_DYNAMIC_DISPATCH(planDouble)(n);
  }
}

} // namespace fft_bench

template <typename T> FftLibrary<T> zldspLibrary() {
  return {"zldsp_fft", FFT_BENCH_ZLDSP_REF, fft_bench::planBestTarget<T>, 0.5};
}

template FftLibrary<float> zldspLibrary<float>();
template FftLibrary<double> zldspLibrary<double>();

#endif
