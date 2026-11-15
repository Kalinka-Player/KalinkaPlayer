#include "RealFft.h"

#include <pffft/pffft.h>
#include <pffft/pffft_double.h>

#include <algorithm>
#include <stdexcept>

namespace {

template <typename T> struct Pffft;

template <> struct Pffft<float> {
  using Setup = PFFFT_Setup;
  static constexpr auto newSetup = pffft_new_setup;
  static constexpr auto destroySetup = pffft_destroy_setup;
  static constexpr auto transform = pffft_transform;
  static constexpr auto transformOrdered = pffft_transform_ordered;
  static constexpr auto reorder = pffft_zreorder;
  static constexpr auto allocate = pffft_aligned_malloc;
  static constexpr auto release = pffft_aligned_free;
  static constexpr auto simdArch = pffft_simd_arch;
};

template <> struct Pffft<double> {
  using Setup = PFFFTD_Setup;
  static constexpr auto newSetup = pffftd_new_setup;
  static constexpr auto destroySetup = pffftd_destroy_setup;
  static constexpr auto transform = pffftd_transform;
  static constexpr auto transformOrdered = pffftd_transform_ordered;
  static constexpr auto reorder = pffftd_zreorder;
  static constexpr auto allocate = pffftd_aligned_malloc;
  static constexpr auto release = pffftd_aligned_free;
  static constexpr auto simdArch = pffftd_simd_arch;
};

/**
 * @brief PFFFT with a work buffer it owns, so no call falls back to the stack.
 *
 * Ordered, the spectrum is interleaved with bin n/2's real part in bin 0's
 * imaginary slot. Unordered is PFFFT's internal z-domain order: faster, and
 * what pffft_zconvolve_accumulate() multiplies in, so it is the layout a
 * convolver built on PFFFT would keep its partitions in.
 */
template <typename T> class PffftFft final : public RealFft<T> {
  using Api = Pffft<T>;

public:
  PffftFft(size_t n, bool ordered)
      : n(n), ordered(ordered),
        setup(Api::newSetup(static_cast<int>(n), PFFFT_REAL)),
        work(static_cast<T *>(Api::allocate(sizeof(T) * n))) {
    if (!setup) {
      throw std::runtime_error("PFFFT cannot transform this length");
    }
  }

  void forward(const T *in, T *spectrum) override {
    run(in, spectrum, PFFFT_FORWARD);
  }

  void inverse(const T *spectrum, T *out) override {
    run(spectrum, out, PFFFT_BACKWARD);
  }

  void toBins(const T *spectrum, T *bins) override {
    if (ordered) {
      std::copy_n(spectrum, n, bins);
    } else {
      Api::reorder(setup.get(), spectrum, bins, PFFFT_FORWARD);
    }
    bins[n] = bins[1];
    bins[1] = 0;
    bins[n + 1] = 0;
  }

  std::string simd() const override { return Api::simdArch(); }

private:
  struct DestroySetup {
    void operator()(typename Api::Setup *setup) const {
      Api::destroySetup(setup);
    }
  };
  struct Release {
    void operator()(T *buffer) const { Api::release(buffer); }
  };

  void run(const T *in, T *out, pffft_direction_t direction) {
    (ordered ? Api::transformOrdered : Api::transform)(setup.get(), in, out,
                                                       work.get(), direction);
  }

  const size_t n;
  const bool ordered;
  std::unique_ptr<typename Api::Setup, DestroySetup> setup;
  std::unique_ptr<T, Release> work;
};

} // namespace

template <typename T> FftLibrary<T> pffftLibrary() {
  return {"PFFFT", FFT_BENCH_PFFFT_REF,
          [](size_t n) -> std::unique_ptr<RealFft<T>> {
            return std::make_unique<PffftFft<T>>(n, true);
          }};
}

template <typename T> FftLibrary<T> pffftUnorderedLibrary() {
  return {"PFFFT unordered", FFT_BENCH_PFFFT_REF,
          [](size_t n) -> std::unique_ptr<RealFft<T>> {
            return std::make_unique<PffftFft<T>>(n, false);
          }};
}

template FftLibrary<float> pffftLibrary<float>();
template FftLibrary<double> pffftLibrary<double>();
template FftLibrary<float> pffftUnorderedLibrary<float>();
template FftLibrary<double> pffftUnorderedLibrary<double>();
