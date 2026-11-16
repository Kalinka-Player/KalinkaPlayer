#ifndef FLAC_STREAM_DECODER_H
#define FLAC_STREAM_DECODER_H

#include "SourceStreamDecoder.h"

#include <FLAC++/decoder.h>

#include <exception>
#include <optional>
#include <vector>

/**
 * @brief FLAC input, decoded to interleaved 16- or 24-bit stereo PCM.
 *
 * libFLAC and everything it reads belong to the worker. Its callbacks never
 * let an exception through the C library: what they meet is kept, they
 * abort, and it is rethrown once libFLAC has returned.
 */
class FlacStreamDecoder final : public SourceStreamDecoder,
                                private FLAC::Decoder::Stream {
public:
  /// @param startOffsetMs Where decoding begins, instead of at the start.
  FlacStreamDecoder(std::optional<StreamId> streamId, size_t bufferSize,
                    size_t startOffsetMs = 0);
  ~FlacStreamDecoder() override;

private:
  std::optional<FLAC__StreamMetadata_StreamInfo> flacInfo;
  /// Set by a seek to the end: nothing is left to decode.
  bool atEnd = false;
  std::vector<uint8_t> converted;
  std::exception_ptr callbackException;

  std::string name() const override { return "FLAC"; }
  StreamInfo open() override;
  bool decodeNext() override;
  void seekSource(uint64_t frame) override;
  void forget() override;

  ::FLAC__StreamDecoderWriteStatus
  write_callback(const ::FLAC__Frame *frame,
                 const FLAC__int32 *const buffer[]) override;
  ::FLAC__StreamDecoderReadStatus read_callback(FLAC__byte buffer[],
                                                size_t *bytes) override;
  void error_callback(::FLAC__StreamDecoderErrorStatus status) override;
  void metadata_callback(const ::FLAC__StreamMetadata *metadata) override;
  ::FLAC__StreamDecoderSeekStatus
  seek_callback(FLAC__uint64 absolute_byte_offset) override;
  ::FLAC__StreamDecoderTellStatus
  tell_callback(FLAC__uint64 *absolute_byte_offset) override;
  ::FLAC__StreamDecoderLengthStatus
  length_callback(FLAC__uint64 *stream_length) override;
  bool eof_callback() override;

  /// Rethrows what a callback kept, then fails when libFLAC did not succeed.
  void check(bool succeeded, const char *operation);
};

#endif
