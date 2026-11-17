#pragma once

/// What a DSD-to-PCM conversion keeps above 20 kHz.
enum class DsdFilter {
  /// Flat to 20 kHz, 110 dB down from 40 kHz.
  Cut30k,
  /// Flat to 40 kHz, 110 dB down from 60 kHz; wide where that is past 0.45 of
  /// the output rate.
  Cut50k,
  /// Flat to 0.45 of the output rate, or less straight out of stage 1.
  Wide,
};

/// Which bit of a DSD byte holds its earliest sample.
enum class DsdBitOrder { MsbFirst, LsbFirst };

/**
 * @brief Which loops stage 1 runs. Reference is the plain code the others are
 * checked against; the variants are kept to be measured against each other.
 * Rows are built for the stage 1 sizes of the 30 kHz and 50 kHz filters and
 * run as channel pairs at any other. Fast is the one the converter uses.
 */
enum class DsdKernel { Reference, Sums, ChannelPair, Nibbles, Rows, Fast };
