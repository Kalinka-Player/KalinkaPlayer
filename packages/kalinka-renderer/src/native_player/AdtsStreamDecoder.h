#pragma once

#include "AacDecoder.h"
#include "BufferedStreamDecoder.h"

#include <atomic>
#include <memory>
#include <optional>
#include <string>
#include <vector>

/**
 * @brief Plays raw AAC in ADTS frames, as `.aac` files and internet radio
 * carry it, from a byte source.
 *
 * Skips leading ID3v2 tags and finds its way back to the frames over junk. A
 * seekable source of known length is seekable by the average frame size, so
 * a seek lands near its target rather than on it and the length is an
 * estimate. A stream that ends mid-frame just ends; a malformed or
 * unsupported one ends in the ERROR state.
 */
class AdtsStreamDecoder final : public BufferedStreamDecoder,
                                public AudioGraphInputNode {
public:
  /// @param bufferSize Output buffer in bytes; it must hold a frame.
  AdtsStreamDecoder(std::optional<StreamId> id, size_t bufferSize,
                    size_t startOffsetMs = 0);
  ~AdtsStreamDecoder() override;
  void connectTo(std::shared_ptr<AudioGraphOutputNode> source) override;
  void disconnect(std::shared_ptr<AudioGraphOutputNode> source) override;

private:
  struct Header;

  std::shared_ptr<AudioGraphOutputNode> input;
  std::unique_ptr<AacDecoder> decoder;
  std::atomic<bool> seekable{false};
  bool sourceSeeks = false;
  uint64_t sourceSize = 0, dataStart = 0, unitBytes = 0, discard = 0;
  /// Bytes read ahead of `position`: what the next frame is parsed from.
  std::vector<uint8_t> window;
  size_t windowStart = 0;
  uint64_t position = 0;
  /// The header bits every frame of the stream repeats.
  std::optional<uint32_t> fixed;

  std::string name() const override { return "ADTS"; }
  StreamInfo open() override;
  bool decodeNext() override;
  void seekSource(uint64_t frame) override;
  bool canSeek() const override { return seekable; }
  void close() override;

  bool fill(size_t bytes);
  void consume(size_t bytes);
  void skipTags();
  std::optional<Header> header(size_t at) const;
  /// Moves to the next frame boundary; false where the stream ends first.
  /// @param confirm Also require the frame after it, against false syncs.
  bool sync(bool confirm);
};
