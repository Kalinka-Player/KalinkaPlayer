#ifndef MP3STREAMDECODER_H
#define MP3STREAMDECODER_H

#include "SourceStreamDecoder.h"

#include <exception>
#include <memory>
#include <vector>

/**
 * @brief MPEG audio input, decoded to interleaved signed 16-bit PCM.
 *
 * A source of known length is a file: minimp3 indexes it, so it seeks and
 * trims its gapless padding. One of unknown length is a broadcast, decoded
 * frame by frame as it arrives; it does not seek, and plays on past the
 * length a gapless tag gives. minimp3 and everything it reads belong to the
 * worker.
 */
class Mp3StreamDecoder final : public SourceStreamDecoder {
public:
  /// @param startOffsetMs Where decoding begins, instead of at the start.
  Mp3StreamDecoder(std::optional<StreamId> streamId, size_t bufferSize,
                   size_t startOffsetMs = 0);
  ~Mp3StreamDecoder() override;

private:
  /// How PCM is made: one way for a file, one for a broadcast.
  class Codec;
  class FileCodec;
  class BroadcastCodec;

  std::unique_ptr<Codec> codec;
  /// Read to learn what the source is, and played back before it.
  std::vector<uint8_t> prefix;
  size_t prefixUsed = 0;
  /// What one of minimp3's callbacks caught, rethrown once it has returned.
  std::exception_ptr callbackException;

  std::string name() const override { return "MP3"; }
  StreamInfo open() override;
  bool decodeNext() override;
  void seekSource(uint64_t frame) override;
  void close() override;
  void forget() override;

  /// Reads as readSome() does, or as readAvailable() when @p partial, taking
  /// the prefix first.
  size_t readInput(void *dest, size_t bytes, bool partial);
  /// Where the next byte read comes from.
  uint64_t inputPosition() const;
  void rethrowCallbackException();
};

#endif // MP3STREAMDECODER_H
