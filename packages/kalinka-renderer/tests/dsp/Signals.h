#ifndef HARNESS_SIGNALS_H
#define HARNESS_SIGNALS_H

#include <cstddef>
#include <span>
#include <vector>

/// Test signals and their analysis, in double, with full scale at ±1.
namespace harness {

/// A sine of @p amplitude at @p frequency Hz, sampled at @p rate.
std::vector<double> sine(double frequency, double rate, size_t frames,
                         double amplitude = 1.0, double phase = 0.0);

/**
 * @brief Equal tones at @p frequencies, with Schroeder phases to keep the
 * crest factor low, scaled so the sum peaks at exactly @p peak.
 */
std::vector<double> multitone(std::span<const double> frequencies, double rate,
                              size_t frames, double peak);

/// Silence with one sample of @p amplitude at frame @p at.
std::vector<double> impulse(size_t frames, size_t at = 0,
                            double amplitude = 1.0);

/// An exponential sweep from @p from to @p to Hz over @p frames (Farina).
std::vector<double> logSweep(double from, double to, double rate,
                             size_t frames, double amplitude = 1.0);

} // namespace harness

#endif
