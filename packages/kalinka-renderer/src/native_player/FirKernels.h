#pragma once

#include <cstddef>

/**
 * @brief Linear-phase FIR loops, vectorised across consecutive outputs and
 * dispatched at run time to the widest instruction set the CPU has.
 *
 * Every output is computed alike, in a full vector, the last ones too, so a
 * result does not depend on where a block ends. Inputs must stay readable for
 * kSlack samples past the last one used; what is read there does not reach a
 * stored output.
 */
namespace fir_kernels {

/// Samples past the end a kernel may read: the widest vector's lanes.
inline constexpr size_t kSlack = 16;

/**
 * @brief out[t] = taps[c] x[t - c] + sum over j < c of
 * taps[j] (x[t - j] + x[t - 2c + j]), c = half - 1.
 * @param x x[t] is output t's newest sample; the 2c samples before x[0] are
 * history.
 */
void symmetric(const double *taps, size_t half, const double *x, size_t count,
               double *out);

/**
 * @brief A half-band on its two input phases: out[m] = 0.5 centre[m]
 * + sum over i of side[i] (other[m - i] + other[m + i + 1]).
 * @param other Readable from other[-(sides - 1)] on.
 */
void halfBand(const double *side, size_t sides, const double *centre,
              const double *other, size_t count, double *out);

} // namespace fir_kernels
