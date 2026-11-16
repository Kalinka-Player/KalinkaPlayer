#pragma once

#include <cstddef>
#include <cstdint>

/**
 * @brief The output stage's arithmetic, vectorised across samples and
 * dispatched at run time to the widest instruction set the CPU has.
 */
namespace output_kernels {

/**
 * @brief Scales, dithers and rounds one channel's samples to integers:
 * out[i] = clamp(round(x[i] (from + step (i + 1)) scale + dither[i]), lo, hi).
 * @return How many samples were clamped.
 */
size_t quantise(const double *x, size_t count, double from, double step,
                double scale, const double *dither, int32_t lo, int32_t hi,
                int32_t *out);

} // namespace output_kernels
