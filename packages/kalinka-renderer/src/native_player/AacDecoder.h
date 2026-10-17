#pragma once

#include "SampleDecoder.h"

#include <memory>
#include <vector>

/**
 * @brief Decodes raw AAC access units (AAC-LC, HE-AAC v1 and v2) with faad2,
 * to 16-bit stereo at the rate the configuration implies.
 *
 * More channels are downmixed and mono is upmixed, so the output format never
 * changes mid-stream. Like every AAC decoder it outputs nothing for the first
 * unit, whose overlap only primes the next one: delay() is one unit long.
 */
class AacDecoder final : public SampleDecoder {
public:
  /// @param config The AudioSpecificConfig, as MP4 and DASH carry it.
  explicit AacDecoder(std::span<const uint8_t> config);
  ~AacDecoder() override;

  StreamAudioFormat format() const override;
  uint64_t delay() const override { return frameLength; }
  void reset() override;
  std::span<const uint8_t> decode(std::span<const uint8_t> unit) override;

private:
  struct Close {
    void operator()(void *handle) const;
  };
  std::vector<uint8_t> config;
  std::unique_ptr<void, Close> handle;
  unsigned long rate = 0;
  uint64_t frameLength = 0;
  std::vector<uint8_t> pcm;

  void open();
};
