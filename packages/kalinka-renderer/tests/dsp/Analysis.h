#ifndef HARNESS_ANALYSIS_H
#define HARNESS_ANALYSIS_H

#include <complex>
#include <cstddef>
#include <span>
#include <vector>

namespace harness {

/**
 * @brief The discrete Fourier transform of @p data, whose size is a power of
 * two: a plain radix-2 transform with exact twiddles, for reference, not speed.
 */
std::vector<std::complex<double>> fft(std::vector<std::complex<double>> data);

/// Bins 0 to size/2 of @p signal's spectrum, zero-padded to @p size.
std::vector<std::complex<double>> realSpectrum(std::span<const double> signal,
                                               size_t size);

/// The 7-term Blackman-Harris window, its sidelobes about 180 dB down.
std::vector<double> blackmanHarris7(size_t size);

/**
 * @brief Mean power of @p signal from @p low to @p high Hz, in dB against a
 * full-scale sine (AES17: a full-scale sine is 0 dBFS).
 *
 * Windowed with blackmanHarris7(), so energy far outside the band, such as
 * DSD's shaped noise, does not leak into it.
 */
double bandPowerDb(std::span<const double> signal, double rate, double low,
                   double high);

/// Gain in dB at @p frequency of the filter with impulse response @p impulse.
double gainDb(std::span<const double> impulse, double frequency, double rate);

/// Phase in radians at @p frequency of the filter @p impulse describes.
double phaseAt(std::span<const double> impulse, double frequency, double rate);

/// A filter's response on an FFT grid, phase unwrapped.
struct Response {
  std::vector<double> frequency;
  std::vector<double> magnitudeDb;
  std::vector<double> phase;
};

/// The response of the filter @p impulse describes, on @p size points.
Response response(std::span<const double> impulse, double rate, size_t size);

/// The fundamental and what is left beside it.
struct Distortion {
  double fundamentalDbfs; ///< The fitted tone's amplitude, 20 log10.
  double thdnDb;          ///< The rest, in band, against the fundamental.
};

/**
 * @brief THD+N by fitting the tone at @p frequency, removing it, and measuring
 * what remains from @p low to @p high Hz (IEEE 1241 sine fit).
 */
Distortion thdPlusN(std::span<const double> signal, double rate,
                    double frequency, double low = 20.0,
                    double high = 20000.0);

/// What remains when one signal is aligned to another and subtracted.
struct Null {
  long lag;   ///< measured[n + lag] lines up with reference[n].
  double gain;
  double peakDbfs; ///< 20 log10 of the largest residual sample.
  double rmsDbfs;  ///< The residual's power against a full-scale sine.
};

/**
 * @brief Finds the lag within ±@p maxLag at which @p measured best matches
 * @p reference and subtracts; with @p matchGain, after least-squares scaling.
 */
Null nullTest(std::span<const double> reference,
              std::span<const double> measured, long maxLag, bool matchGain);

/// Steps between neighbouring samples that a clean tone cannot make.
struct Clicks {
  double largestStep;
  double allowedStep;
  std::vector<size_t> at; ///< Where a step exceeds what is allowed.
};

/**
 * @brief Bounds every sample-to-sample step of @p signal by the largest a
 * sine of @p amplitude at @p frequency makes, plus @p slack for rounding.
 */
Clicks findClicks(std::span<const double> signal, double frequency,
                  double rate, double amplitude, double slack = 1e-9);

} // namespace harness

#endif
