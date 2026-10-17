#pragma once

#include "BufferedStreamDecoder.h"

#include <FLAC/format.h>
#include <chrono>
#include <memory>
#include <stop_token>
#include <string>
#include <vector>

/** @brief Fetches static MPEG-DASH audio and supplies its original PCM samples.
 * @note Owns the demuxer and the network requests. Seeks use absolute sample
 * frames; destruction interrupts outstanding network requests.
 */
class DashStreamDecoder final : public BufferedStreamDecoder {
public:
  DashStreamDecoder(
      std::optional<StreamId> id, std::string url, size_t bufferSize,
      size_t startOffsetMs = 0,
      std::chrono::milliseconds timeout = std::chrono::seconds(15),
      long maxRedirects = 3);
  ~DashStreamDecoder() override;

private:
  struct Demux;
  struct Prefetch;
  const std::string url;
  const std::chrono::milliseconds timeout;
  const long maxRedirects;
  std::unique_ptr<Demux> demux;
  uint64_t discardBefore = 0, nextFrame = 0;
  std::vector<uint8_t> pcm;

  std::string name() const override { return "DASH"; }
  StreamInfo open() override;
  bool decodeNext() override;
  void seekSource(uint64_t frame) override;
  void close() override;
  std::vector<uint8_t> segmentBytes(size_t index);
  void writeFrame(const FLAC__Frame &frame, const FLAC__int32 *const samples[],
                  uint64_t first);
  std::vector<uint8_t> fetch(const std::string &source, size_t limit,
                             std::stop_token cancel = {});
};
