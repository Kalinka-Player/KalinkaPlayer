#pragma once

#include "dsp/DspProcessor.h"

#include <array>
#include <memory>
#include <optional>
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
  /// Fixed hops used for the longest responses.
  static constexpr Partitions kPartitions{640, 6400};
  static constexpr size_t kMaxTaps = 262144;

  // Imported WAVs retain the original limit. Allow their duration to grow
  // when converted across the standard PCM rates, plus SRC filter support.
  static constexpr size_t kConversionLookahead = kPartitions.head / 2;
  static constexpr size_t kMaxConvertedTaps = 16 * kMaxTaps + kPartitions.head;
  static std::shared_ptr<FirRoomCorrection>
  converted(unsigned rate, std::span<const double> left,
            std::span<const double> right);

  /// Default FFT hops for the longer channel after trimming exact trailing
  /// zeros.
  static Partitions partitionsFor(size_t activeTaps);
  FirRoomCorrection(unsigned rate, std::span<const double> left,
                    std::span<const double> right);
  FirRoomCorrection(unsigned rate, std::span<const double> left,
                    std::span<const double> right, Partitions partitions);
  ~FirRoomCorrection() override;

  bool handles(const StreamAudioFormat &format) const override;
  Output output(const StreamAudioFormat &format) const override;
  SignalPath describe(const StreamAudioFormat &format) const override;
  size_t start(const StreamAudioFormat &format) override;
  bool changeInputFormat(const StreamAudioFormat &format) override;
  size_t preparedHistory() const override;
  size_t process(const uint8_t *in, uint8_t *out, size_t frames) override;

private:
  FirRoomCorrection(unsigned rate, std::span<const double> left,
                    std::span<const double> right,
                    std::optional<Partitions> requested,
                    size_t limit = kMaxTaps);
  class Channel;
  const unsigned rate;
  const size_t taps, latency;
  size_t historyFrames;
  size_t pending = 0;
  std::array<std::unique_ptr<Channel>, 2> channels;
  AudioSampleFormat inputFormat = PCM_FLOAT64_LE;
  void reset();
};
