#pragma once

#include "dsp/fir/FirRoomCorrection.h"
#include "dsp/output/GainRamp.h"
#include "dsp/output/PlanarPcm.h"

#include <atomic>
#include <map>
#include <string>
#include <vector>

struct FirFilterBank {
  std::string name;
  std::map<unsigned, std::shared_ptr<FirRoomCorrection>> filters;
};

// A fixed frame contract for the duration of playback. Dry PCM goes through
// the same delay as the convolver so a crossfade never mixes different times.
class LiveFir final : public DspProcessor {
public:
  void select(std::shared_ptr<const FirFilterBank> bank);
  bool handles(const StreamAudioFormat &format) const override;
  Output output(const StreamAudioFormat &format) const override;
  SignalPath describe(const StreamAudioFormat &format) const override;
  size_t start(const StreamAudioFormat &format) override;
  bool changeInputFormat(const StreamAudioFormat &format) override;
  size_t historyFor(const StreamAudioFormat &format) const override;
  size_t history() const override { return historyFor(input); }
  size_t settledOutputFrames() const override;
  bool needsSuccessorInput() const override { return true; }
  size_t process(const uint8_t *in, uint8_t *out, size_t frames) override;

private:
  static constexpr size_t lag = FirRoomCorrection::kPartitions.head;
  std::atomic<std::shared_ptr<const FirFilterBank>> requested;
  std::shared_ptr<FirRoomCorrection>
  filterFor(const StreamAudioFormat &format) const;
  // Retain old plans on the control thread until the audio thread is done.
  std::vector<std::shared_ptr<const FirFilterBank>> retained;
  std::shared_ptr<FirRoomCorrection> active;
  StreamAudioFormat input{};
  GainRamp wet{std::chrono::milliseconds(25)};
  std::array<std::array<double, 2>, lag> dry{};
  size_t cursor = 0, warmup = 0;
  uint64_t settledFrames = 0;
  PlanarScratch scratch;
  std::array<std::array<double, 2>, kPlanarFrames> filtered{};
};
