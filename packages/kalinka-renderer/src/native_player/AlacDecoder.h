#pragma once

#include "SampleDecoder.h"

#include <memory>

/**
 * @brief Decodes Apple Lossless (ALAC) frames one access unit at a time, as
 * MP4 carries them.
 *
 * Every frame stands alone, so there is no delay and reset() has nothing to
 * forget. Output keeps the stream's own depth. Every read is checked against
 * the unit, and every buffer is sized from the configuration, never from
 * what a frame claims.
 */
class AlacDecoder final : public SampleDecoder {
public:
  /// @param config The 24-byte ALACSpecificConfig; mono or stereo, 16, 24 or
  /// 32 bits.
  explicit AlacDecoder(std::span<const uint8_t> config);
  ~AlacDecoder() override;

  StreamAudioFormat format() const override;
  uint64_t delay() const override { return 0; }
  bool padded() const override { return false; }
  void reset() override {}
  std::span<const uint8_t> decode(std::span<const uint8_t> unit) override;

private:
  class Frames;
  std::unique_ptr<Frames> frames;
};
