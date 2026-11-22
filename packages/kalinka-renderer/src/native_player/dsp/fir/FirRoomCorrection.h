#pragma once

#include "dsp/DspProcessor.h"

#include <array>
#include <memory>
#include <span>

/**
 * @brief Stereo PCM room correction for an impulse response at one sample rate.
 * Construct off the playback thread. The processor owns its prepared filters.
 * Equal partition sizes select uniform convolution. The first partition sets
 * the buffering delay. The impulse is causal, with tap zero at time zero.
 */
class FirRoomCorrection final : public DspProcessor {
public:
  /// FFT hops in frames. The tail hop must be a multiple of the head hop.
  struct Partitions {
    size_t head, tail;
  };
  static constexpr Partitions kPartitions{640, 6400};
  static constexpr size_t kMaxTaps = 262144;

  FirRoomCorrection(unsigned rate, std::span<const double> left,
                    std::span<const double> right,
                    Partitions partitions = kPartitions);
  ~FirRoomCorrection() override;

  bool handles(const StreamAudioFormat &format) const override;
  Output output(const StreamAudioFormat &format) const override;
  SignalPath describe(const StreamAudioFormat &format) const override;
  size_t start(const StreamAudioFormat &format) override;
  bool changeInputFormat(const StreamAudioFormat &format) override;
  size_t preparedHistory() const override;
  size_t process(const uint8_t *in, uint8_t *out, size_t frames) override;

private:
  class Channel;
  const unsigned rate;
  const size_t taps, latency;
  std::array<std::unique_ptr<Channel>, 2> channels;
  AudioSampleFormat inputFormat = PCM_FLOAT64_LE;
};
