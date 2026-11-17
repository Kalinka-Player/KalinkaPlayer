#pragma once

#include <cstddef>
#include <cstdint>

/**
 * @brief DSF's planar channel blocks turned into interleaved frames,
 * vectorised and dispatched at run time to the widest instruction set the CPU
 * has.
 */
namespace dsd_block_kernels {

/**
 * @brief Interleaves the first @p bytes of each channel's block:
 * out[b * channels + c] is byte b of the block at blocks + c * stride, with
 * its bits reversed when @p reverseBits.
 * @param channels 1 or 2, all a DSD container admits.
 * @note Reads nothing of a block past its first @p bytes and writes nothing of
 * @p out past its first bytes * channels, whatever the vector width. @p out
 * must not overlap the blocks.
 */
void interleave(const uint8_t *blocks, size_t stride, unsigned channels,
                size_t bytes, bool reverseBits, uint8_t *out);

} // namespace dsd_block_kernels
