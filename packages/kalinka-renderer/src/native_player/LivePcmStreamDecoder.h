#pragma once

#include "LivePcmClock.h"
#include "SourceStreamDecoder.h"
#include <deque>
#include <mutex>
#include <optional>
#include <vector>

/// @brief Reads framed PCM v1 and maps drift-corrected output to source frames.
class LivePcmStreamDecoder final : public SourceStreamDecoder {
public:
  explicit LivePcmStreamDecoder(std::optional<StreamId> id);
  ~LivePcmStreamDecoder() override;
  size_t read(void *dest, size_t bytes) override;
  std::optional<uint64_t> liveSourceFrame() const override;

private:
  std::string name() const override { return "Live PCM"; }
  void forget() override;
  StreamInfo open() override;
  bool decodeNext() override;
  void seekSource(uint64_t) override;
  std::optional<uint64_t> nextFrame;
  std::vector<uint8_t> samples;
  std::unique_ptr<LivePcmClock> clock;
  struct Mapping {
    uint64_t outputBegin, outputEnd;
    double sourceBegin, sourceEnd;
  };
  mutable std::mutex mapMutex;
  std::deque<Mapping> mappings;
  uint64_t outputFrames = 0;
  double mappedSource = 0;
  std::optional<uint64_t> readSourceFrame;
};
