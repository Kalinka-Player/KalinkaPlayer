#include "RealFft.h"

#include <fftw3.h>

#include <algorithm>
#include <cstdlib>
#include <regex>
#include <set>
#include <stdexcept>
#include <type_traits>

namespace {

template <typename T> struct Fftw;

template <> struct Fftw<float> {
  using Plan = fftwf_plan;
  using Complex = fftwf_complex;
  static constexpr auto planForward = fftwf_plan_dft_r2c_1d;
  static constexpr auto planInverse = fftwf_plan_dft_c2r_1d;
  static constexpr auto executeForward = fftwf_execute_dft_r2c;
  static constexpr auto executeInverse = fftwf_execute_dft_c2r;
  static constexpr auto destroy = fftwf_destroy_plan;
  static constexpr auto allocate = fftwf_malloc;
  static constexpr auto release = fftwf_free;
  static constexpr auto describe = fftwf_sprint_plan;
  static constexpr const char *version = fftwf_version;
  static constexpr auto forgetWisdom = fftwf_forget_wisdom;
};

template <> struct Fftw<double> {
  using Plan = fftw_plan;
  using Complex = fftw_complex;
  static constexpr auto planForward = fftw_plan_dft_r2c_1d;
  static constexpr auto planInverse = fftw_plan_dft_c2r_1d;
  static constexpr auto executeForward = fftw_execute_dft_r2c;
  static constexpr auto executeInverse = fftw_execute_dft_c2r;
  static constexpr auto destroy = fftw_destroy_plan;
  static constexpr auto allocate = fftw_malloc;
  static constexpr auto release = fftw_free;
  static constexpr auto describe = fftw_sprint_plan;
  static constexpr const char *version = fftw_version;
  static constexpr auto forgetWisdom = fftw_forget_wisdom;
};

/**
 * @brief The system FFTW, planned with FFTW_MEASURE and run through the
 * new-array execute calls, as a convolver with its own buffers would.
 *
 * FFTW's complex-to-real transform overwrites its input unless planned with
 * FFTW_PRESERVE_INPUT, and so planned it copies into a buffer it allocates on
 * every call. Without @p preserveInput the inverse instead copies into scratch
 * held here and lets FFTW overwrite that.
 *
 * Each plan forgets the wisdom it gathered, so every plan is measured cold
 * and what it holds is the plan alone, not FFTW's record of how it chose it.
 */
template <typename T> class FftwFft final : public RealFft<T> {
  using Api = Fftw<T>;

  struct DestroyPlan {
    void operator()(typename Api::Plan plan) const { Api::destroy(plan); }
  };
  struct Release {
    void operator()(void *buffer) const { Api::release(buffer); }
  };
  using Plan =
      std::unique_ptr<std::remove_pointer_t<typename Api::Plan>, DestroyPlan>;
  using Complex = typename Api::Complex;

public:
  FftwFft(size_t n, bool preserveInput)
      : n(n), preserveInput(preserveInput),
        scratch(static_cast<Complex *>(
            Api::allocate(sizeof(Complex) * (n / 2 + 1)))) {
    const std::unique_ptr<T, Release> real(
        static_cast<T *>(Api::allocate(sizeof(T) * n)));
    const int length = static_cast<int>(n);
    forwardPlan.reset(
        Api::planForward(length, real.get(), scratch.get(), FFTW_MEASURE));
    inversePlan.reset(Api::planInverse(
        length, scratch.get(), real.get(),
        FFTW_MEASURE | (preserveInput ? FFTW_PRESERVE_INPUT : 0)));
    Api::forgetWisdom();
    if (!forwardPlan || !inversePlan) {
      throw std::runtime_error("FFTW could not plan the transform");
    }
    if (preserveInput) {
      scratch.reset();
    }
  }

  void forward(const T *in, T *spectrum) override {
    Api::executeForward(forwardPlan.get(), const_cast<T *>(in),
                        reinterpret_cast<Complex *>(spectrum));
  }

  void inverse(const T *spectrum, T *out) override {
    Complex *in = reinterpret_cast<Complex *>(const_cast<T *>(spectrum));
    if (!preserveInput) {
      std::copy_n(spectrum, n + 2, reinterpret_cast<T *>(scratch.get()));
      in = scratch.get();
    }
    Api::executeInverse(inversePlan.get(), in, out);
  }

  void toBins(const T *spectrum, T *bins) override {
    std::copy_n(spectrum, n + 2, bins);
  }

  std::string simd() const override {
    return codeletFamilies(forwardPlan.get()) + " / " +
           codeletFamilies(inversePlan.get());
  }

private:
  /// The SIMD codelet families named in FFTW's own description of @p plan.
  static std::string codeletFamilies(typename Api::Plan plan) {
    const std::unique_ptr<char, decltype(&std::free)> text(Api::describe(plan),
                                                           &std::free);
    const std::string description = text.get();
    static const std::regex suffix(
        "_(avx512|avx2_128|avx2|avx_128_fma|avx|sse2|neon|generic_simd128|"
        "generic_simd256|altivec|vsx)\"");
    std::set<std::string> families;
    for (auto match = std::sregex_iterator(description.begin(),
                                           description.end(), suffix);
         match != std::sregex_iterator(); ++match) {
      families.insert((*match)[1]);
    }
    std::string joined;
    for (const std::string &family : families) {
      joined += (joined.empty() ? "" : "+") + family;
    }
    return joined.empty() ? "scalar" : joined;
  }

  const size_t n;
  const bool preserveInput;
  std::unique_ptr<Complex, Release> scratch;
  Plan forwardPlan;
  Plan inversePlan;
};

} // namespace

template <typename T> FftLibrary<T> fftwLibrary() {
  return {"FFTW", Fftw<T>::version,
          [](size_t n) -> std::unique_ptr<RealFft<T>> {
            return std::make_unique<FftwFft<T>>(n, false);
          }};
}

template <typename T> FftLibrary<T> fftwPreserveInputLibrary() {
  return {"FFTW preserve-input", Fftw<T>::version,
          [](size_t n) -> std::unique_ptr<RealFft<T>> {
            return std::make_unique<FftwFft<T>>(n, true);
          }};
}

template FftLibrary<float> fftwLibrary<float>();
template FftLibrary<double> fftwLibrary<double>();
template FftLibrary<float> fftwPreserveInputLibrary<float>();
template FftLibrary<double> fftwPreserveInputLibrary<double>();
