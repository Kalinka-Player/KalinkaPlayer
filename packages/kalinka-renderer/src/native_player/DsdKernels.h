#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

/**
 * @brief DsdToPcm's stage 1 loops: per-byte tables take one-bit DSD to samples
 * at an eighth of its rate. The vector ones are dispatched at run time to the
 * widest instruction set the CPU has.
 */
namespace dsd_kernels {

/// One group of 8 stage 1 taps, looked up by byte: whole numbers.
using Stage1Table = std::array<double, 256>;

/**
 * @brief Stage 1 for one channel, from four partial sums at a time: out[m] =
 * scale (sum over g < stored of tables[g][direct[k - g]] + sum over
 * stored <= g < groups of tables[groups - 1 - g][mirror[k - g]]),
 * k = groups - 1 + m.
 * @param stored @p groups for whole tables, or the first half that symmetry
 * lets stand for the rest.
 * @param mirror The same bytes as @p direct, each with its bits reversed;
 * unread when every group is stored.
 * @note The sums are whole numbers under 2^53, so they are exact in any order
 * and every variant gives the same output, bit for bit.
 */
void stage1(const Stage1Table *tables, size_t stored, size_t groups,
            const uint8_t *direct, const uint8_t *mirror, size_t count,
            double scale, double *out);

/// Bytes past the end a stage 1 kernel may read: the widest byte vector.
inline constexpr size_t kByteSlack = 64;

/// Byte planes of a stage 1 nibble table: whole numbers, biased positive.
inline constexpr size_t kNibblePlanes = 5;
/// What every nibble entry was raised by, under the 2^40 the planes hold.
inline constexpr double kNibbleBias = 137438953472.0; // 2^37

/// One group's taps, by nibble: entry n of plane p is byte p of the biased
/// sum the 4 bits of n select, low nibble for its first 4 taps, high for the
/// rest.
struct NibbleTables {
  uint8_t low[kNibblePlanes][16];
  uint8_t high[kNibblePlanes][16];
};

/**
 * @brief Stage 1 by nibble lookups, as many outputs at once as a vector has
 * bytes: out[m] = scale (sum over g of the group's two nibble entries for
 * direct[k - g], unbiased), k = groups - 1 + m.
 * @param direct In the order the tables were built in; reads up to
 * kByteSlack bytes past the last used.
 */
void stage1Nibbles(const NibbleTables *tables, size_t groups,
                   const uint8_t *direct, size_t count, double scale,
                   double *out);

/// stage1() for two channels at once, their sums side by side in one vector.
void stage1Pair(const Stage1Table *tables, size_t stored, size_t groups,
                const uint8_t *const direct[2], const uint8_t *const mirror[2],
                size_t count, double scale, double *const out[2]);

} // namespace dsd_kernels
