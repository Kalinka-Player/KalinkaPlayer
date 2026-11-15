#include "RealFft.h"

#include <pocketfft_hdronly.h>

#include <algorithm>

namespace {

/**
 * @brief pocketfft's one-dimensional real plan, the class its own r2c() and
 * c2r() build for every call.
 *
 * The plan transforms in place into FFTPACK's half-complex order
 * (r0, r1, i1, …, r[n/2]), so each call first copies its input to the output,
 * as r2c() and c2r() do too.
 *
 * @note pocketfft_r is in pocketfft::detail: the header's public API is the
 * multidimensional r2c()/c2r(), which plans on every call unless the plan
 * cache is compiled in.
 */
template <typename T> class PocketFft final : public RealFft<T> {
public:
  explicit PocketFft(size_t n) : n(n), plan(n) {}

  void forward(const T *in, T *spectrum) override {
    std::copy_n(in, n, spectrum);
    plan.exec(spectrum, T(1), pocketfft::FORWARD);
  }

  void inverse(const T *spectrum, T *out) override {
    std::copy_n(spectrum, n, out);
    plan.exec(out, T(1), pocketfft::BACKWARD);
  }

  void toBins(const T *spectrum, T *bins) override {
    bins[0] = spectrum[0];
    bins[1] = 0;
    std::copy_n(spectrum + 1, n - 1, bins + 2);
    bins[n + 1] = 0;
  }

  std::string simd() const override { return "none (scalar 1-D code)"; }

private:
  const size_t n;
  pocketfft::detail::pocketfft_r<T> plan;
};

} // namespace

template <typename T> FftLibrary<T> pocketfftLibrary() {
  return {"pocketfft", FFT_BENCH_POCKETFFT_REF,
          [](size_t n) -> std::unique_ptr<RealFft<T>> {
            return std::make_unique<PocketFft<T>>(n);
          }};
}

template FftLibrary<float> pocketfftLibrary<float>();
template FftLibrary<double> pocketfftLibrary<double>();
