#pragma once

#include "Mp4.h"
#include "StreamState.h"

#include <cstdint>
#include <memory>
#include <span>

/**
 * @brief Decodes the access units of one audio track, in order, to
 * interleaved little-endian PCM in format().
 *
 * Throws std::runtime_error for audio it cannot decode; the message is what
 * the listener sees. Used from one thread at a time.
 */
class SampleDecoder {
public:
  virtual ~SampleDecoder() = default;
  virtual StreamAudioFormat format() const = 0;
  /// Output frames the decoder holds back after construction or reset():
  /// the first access unit then only primes it and yields nothing.
  virtual uint64_t delay() const = 0;
  /// Whether a unit may decode to more than it lasts: encoder padding the
  /// container trims. A codec without it must fill each unit exactly.
  virtual bool padded() const = 0;
  /// Forgets every unit decoded so far, as a seek needs.
  virtual void reset() = 0;
  /// @return The unit's frames; valid until the next call, empty while
  /// priming.
  virtual std::span<const uint8_t> decode(std::span<const uint8_t> unit) = 0;
};

/// @brief The decoder for an MP4 track's codec and configuration.
std::unique_ptr<SampleDecoder> sampleDecoder(const mp4::AudioTrack &track);
