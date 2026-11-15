#include "RealFft.h"

#include <kfr/dft.hpp>
#include <kfr/version.hpp>

#include <algorithm>
#include <vector>

namespace {

/**
 * @brief KFR's real DFT plan in its CCs layout (n/2 + 1 complex bins), with
 * the scratch buffer it asks for held here, since a null one is allocated per
 * call.
 */
template <typename T> class KfrFft final : public RealFft<T> {
public:
  explicit KfrFft(size_t n) : n(n), plan(n), temp(plan.temp_size) {}

  void forward(const T *in, T *spectrum) override {
    plan.execute(reinterpret_cast<kfr::complex<T> *>(spectrum), in,
                 temp.data());
  }

  void inverse(const T *spectrum, T *out) override {
    plan.execute(out, reinterpret_cast<const kfr::complex<T> *>(spectrum),
                 temp.data());
  }

  void toBins(const T *spectrum, T *bins) override {
    std::copy_n(spectrum, n + 2, bins);
  }

  std::string simd() const override { return kfr::cpu_runtime(); }

private:
  const size_t n;
  kfr::dft_plan_real<T> plan;
  std::vector<kfr::u8> temp;
};

} // namespace

template <typename T> FftLibrary<T> kfrLibrary() {
  return {"KFR", kfr::library_version(),
          [](size_t n) -> std::unique_ptr<RealFft<T>> {
            return std::make_unique<KfrFft<T>>(n);
          }};
}

template FftLibrary<float> kfrLibrary<float>();
template FftLibrary<double> kfrLibrary<double>();
