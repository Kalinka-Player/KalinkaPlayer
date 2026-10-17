#pragma once

#include "SampleDecoder.h"

#include <cstdint>
#include <memory>
#include <span>

/**
 * @brief Decodes an MP4 track's access units in timeline order and keeps the
 * frames from the latest restart target on.
 *
 * Positions are output frames on the track's timeline. A codec with a delay
 * yields nothing for the first unit after a restart, so the caller starts
 * that much early: the stream plays from the timeline's delay() onwards.
 * A padded codec's unit lasting less than its audio keeps only its duration,
 * as MP4 trims the encoder's padding. Throws when a unit leaves a gap, or
 * its audio does not match its duration where the codec cannot pad.
 */
class TrackDecoder {
public:
  explicit TrackDecoder(std::unique_ptr<SampleDecoder> decoder);

  StreamAudioFormat format() const { return decoder->format(); }
  uint64_t delay() const { return decoder->delay(); }
  /// Resets the codec and drops every frame before `frame`.
  void restart(uint64_t frame);
  /// @param first Where the unit starts. @param frames How long it lasts.
  /// @return Its frames at or after the target; valid until the next call.
  std::span<const uint8_t> decode(std::span<const uint8_t> unit, uint64_t first,
                                  uint64_t frames);

private:
  const std::unique_ptr<SampleDecoder> decoder;
  const size_t frameBytes;
  uint64_t target = 0, next = 0;
};
