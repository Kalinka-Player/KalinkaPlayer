#pragma once

#include "SampleDecoder.h"

#include <array>
#include <memory>

/**
 * @brief Decodes FLAC frames one access unit at a time, as MP4 carries them,
 * with libFLAC.
 *
 * Every frame stands alone, so there is no delay and reset() only forgets a
 * frame cut short. Output keeps the stream's own depth.
 */
class FlacFrameDecoder final : public SampleDecoder {
public:
  /// @param streamInfo The 34-byte STREAMINFO block; mono or stereo, 16, 24
  /// or 32 bits.
  explicit FlacFrameDecoder(std::span<const uint8_t> streamInfo);
  ~FlacFrameDecoder() override;

  StreamAudioFormat format() const override { return audio; }
  uint64_t delay() const override { return 0; }
  void reset() override;
  std::span<const uint8_t> decode(std::span<const uint8_t> unit) override;

private:
  class Stream;
  StreamAudioFormat audio{};
  std::unique_ptr<Stream> stream;
};
