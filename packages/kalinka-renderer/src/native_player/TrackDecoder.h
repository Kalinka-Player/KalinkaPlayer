#pragma once

#include "Mp4.h"
#include "SampleDecoder.h"

#include <cstdint>
#include <memory>
#include <span>

/**
 * @brief Decodes an MP4 track's access units in timeline order and keeps
 * the frames that play: those the track's edit selects, from the latest
 * restart target on.
 *
 * Timeline positions are output frames from the track's first unit. What
 * plays starts at the edit, or later where the codec's delay swallows more,
 * and ends at the edit's end or the timeline's. A padded codec's unit
 * lasting less than its audio keeps only its duration, as MP4 trims the
 * encoder's padding. Throws when a unit leaves a gap, or its audio does not
 * match its duration where the codec cannot pad.
 */
class TrackDecoder {
public:
  /// @param start Where what plays begins on the timeline.
  /// @param length How long it lasts; 0 means to the timeline's end.
  explicit TrackDecoder(std::unique_ptr<SampleDecoder> decoder,
                        uint64_t start = 0, uint64_t length = 0);
  /// The decoder and edit an MP4 track describes.
  static std::unique_ptr<TrackDecoder> forTrack(const mp4::AudioTrack &track);

  StreamAudioFormat format() const { return decoder->format(); }
  /// Notes where the timeline ends, 0 when unknown.
  /// @return How many frames play; 0 when unknown.
  uint64_t setEnd(uint64_t timelineEnd);
  /// Resets the codec so play resumes `frame` frames into what plays.
  /// @return The timeline frame whose unit decoding must start with.
  uint64_t restart(uint64_t frame);
  /// @param first Where the unit starts. @param frames How long it lasts.
  /// @return Its frames that play; valid until the next call.
  std::span<const uint8_t> decode(std::span<const uint8_t> unit, uint64_t first,
                                  uint64_t frames);

private:
  const std::unique_ptr<SampleDecoder> decoder;
  const size_t frameBytes;
  const uint64_t begin, editEnd;
  uint64_t end = ~uint64_t(0), target = 0, next = 0;
};
