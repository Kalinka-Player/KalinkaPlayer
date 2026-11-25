#pragma once

#include "dsp/fir/FirFilterBank.h"
#include "dsp/output/GainRamp.h"
#include "dsp/output/PlanarPcm.h"

#include <atomic>
#include <vector>

// A fixed frame contract for the duration of playback. Dry PCM goes through
// the same delay as the convolver so a crossfade never mixes different times.
class LiveFir final : public DspProcessor {
public:
  void select(std::shared_ptr<const FirFilterBank> bank);
  unsigned lastRate() const { return runRate.load(); }
  bool handles(const StreamAudioFormat &format) const override;
  Output output(const StreamAudioFormat &format) const override;
  SignalPath describe(const StreamAudioFormat &format) const override;
  size_t start(const StreamAudioFormat &format) override;
  bool changeInputFormat(const StreamAudioFormat &format) override;
  size_t historyFor(const StreamAudioFormat &format) const override;
  size_t history() const override { return historyFor(input); }
  size_t settledOutputFrames() const override;
  bool needsSuccessorInput() const override { return true; }
  /// Interleaved float64 PCM may be filtered in place: readPlanar and the
  /// convolver consume each whole block before its output overwrites it.
  size_t process(const uint8_t *in, uint8_t *out, size_t frames) override;

private:
  static constexpr size_t lag = FirRoomCorrection::kPartitions.head;
  std::atomic<std::shared_ptr<const FirFilterBank>> requested;
  std::shared_ptr<const FirVariant>
  variantFor(const StreamAudioFormat &format) const;
  std::mutex setupMutex;
  std::atomic<unsigned> runRate{0};
  std::atomic<std::shared_ptr<const FirVariant>> desired;
  // Keep outgoing plans alive through the fade. Reclaim on the control thread.
  std::vector<std::shared_ptr<const FirVariant>> retired;
  std::shared_ptr<const FirVariant> active;
  StreamAudioFormat input{};
  GainRamp wet{std::chrono::milliseconds(25)};
  std::array<std::array<double, 2>, lag> dry{};
  size_t cursor = 0, warmup = 0;
  uint64_t settledFrames = 0;
  PlanarScratch scratch;
  std::array<std::array<double, 2>, kPlanarFrames> filtered{};
};
