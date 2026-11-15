#ifndef REAL_FFT_H
#define REAL_FFT_H

#include <cstddef>
#include <memory>
#include <string>

/**
 * @brief A real-input FFT of one length, planned once at construction.
 *
 * Neither direction scales, so inverse(forward(x)) is n·x times the
 * library's FftLibrary::roundTripScale, and neither may change its input.
 * The spectrum stays in whatever layout the library's fastest call produces,
 * at most n + 2 values; toBins() rewrites it into the common one for
 * comparison.
 *
 * @note forward() and inverse() are the calls an audio thread would make:
 * whatever they allocate is what the benchmark is there to find out, so an
 * adapter adds no buffering the library itself does not need.
 */
template <typename T> class RealFft {
public:
  virtual ~RealFft() = default;

  virtual void forward(const T *in, T *spectrum) = 0;
  virtual void inverse(const T *spectrum, T *out) = 0;

  /// Rewrites @p spectrum as n/2 + 1 interleaved (re, im) pairs, DC first.
  virtual void toBins(const T *spectrum, T *bins) = 0;

  /// The instruction set the transform runs on, as the library reports it.
  virtual std::string simd() const = 0;
};

/// One library under test: its name in the tables, and how to plan it.
template <typename T> struct FftLibrary {
  std::string name;
  std::string version;
  /// Throws when the library cannot transform @p n points.
  std::unique_ptr<RealFft<T>> (*plan)(size_t n);
  /// inverse(forward(x)) over n·x: 1 for every library but one.
  double roundTripScale = 1;
};

template <typename T> FftLibrary<T> fftwLibrary();
template <typename T> FftLibrary<T> fftwPreserveInputLibrary();
template <typename T> FftLibrary<T> pffftLibrary();
template <typename T> FftLibrary<T> pffftUnorderedLibrary();
template <typename T> FftLibrary<T> pocketfftLibrary();
template <typename T> FftLibrary<T> kfrLibrary();
template <typename T> FftLibrary<T> zldspLibrary();

#endif
