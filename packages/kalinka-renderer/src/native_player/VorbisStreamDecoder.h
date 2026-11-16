#pragma once

#include "SourceStreamDecoder.h"

#include <vorbis/vorbisfile.h>

/**
 * @brief Ogg/Vorbis input, decoded to interleaved signed 16-bit little-endian
 * PCM.
 *
 * Mono and stereo; every link of a chained stream must keep the first one's
 * format. libvorbisfile and everything it reads belong to the worker.
 */
class VorbisStreamDecoder final : public SourceStreamDecoder {
public:
  /// @param startOffsetMs Where decoding begins, instead of at the start.
  VorbisStreamDecoder(std::optional<StreamId> streamId, size_t bufferSize,
                      size_t startOffsetMs = 0);
  ~VorbisStreamDecoder() override;

private:
  OggVorbis_File file{};
  bool opened = false;
  /// An interrupted seek can leave libvorbisfile's decode state cleared.
  bool resetBeforeSeek = false;

  std::string name() const override { return "Vorbis"; }
  StreamInfo open() override;
  bool decodeNext() override;
  void seekSource(uint64_t frame) override;
  void close() override;
  void forget() override;

  size_t readInput(void *data, size_t size, size_t count);
  int seekInput(ogg_int64_t offset, int whence);
  static size_t readCallback(void *data, size_t size, size_t count,
                             void *source) noexcept;
  static int seekCallback(void *source, ogg_int64_t offset,
                          int whence) noexcept;
  static long tellCallback(void *source) noexcept;
  /// Rethrows what a callback caught, then fails on a negative @p result.
  void check(long result, const char *operation);
  void validateFormat(const vorbis_info *info,
                      const StreamAudioFormat &format) const;
};
