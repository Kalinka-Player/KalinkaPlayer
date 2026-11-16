#include "FlacStreamDecoder.h"

#include "AudioSampleFormat.h"

#include <span>
#include <stdexcept>
#include <string>

namespace {
AudioSampleFormat bitsToFormat(unsigned bits) {
  switch (bits) {
  case 16:
    return AudioSampleFormat::PCM16_LE;
  case 24:
    return AudioSampleFormat::PCM24_LE;
  case 32:
    throw std::runtime_error("32-bit PCM not supported");
  default:
    throw std::runtime_error("Unsupported bits depth per sample");
  }
}
} // namespace

FlacStreamDecoder::FlacStreamDecoder(std::optional<StreamId> streamId,
                                     size_t bufferSize, size_t startOffsetMs)
    : AudioGraphNode(streamId), SourceStreamDecoder(bufferSize, startOffsetMs) {
}

FlacStreamDecoder::~FlacStreamDecoder() { stop(); }

void FlacStreamDecoder::forget() {
  finish();
  if (init() != FLAC__STREAM_DECODER_INIT_STATUS_OK) {
    throw std::runtime_error("FLAC decoder could not be initialised");
  }
  flacInfo.reset();
  atEnd = false;
}

StreamInfo FlacStreamDecoder::open() {
  check(process_until_end_of_metadata(), "metadata");
  if (!flacInfo) {
    throw std::runtime_error("FLAC stream info is missing");
  }
  if (flacInfo->channels != 2) {
    throw std::runtime_error("FLAC decoder supports stereo audio");
  }
  const auto format = bitsToFormat(flacInfo->bits_per_sample);
  // Probe without moving: libFLAC has read exactly up to here. Without a
  // length, every seek would be clamped to the start.
  setSeekable(flacInfo->total_samples && reposition(sourcePosition()));
  return {.format = {.sampleRate = flacInfo->sample_rate,
                     .channels = flacInfo->channels,
                     .bitsPerSample = flacInfo->bits_per_sample,
                     .sampleFormat = format},
          .streamType = StreamType::FRAMES,
          .streamSize = flacInfo->total_samples,
          .prebufferMs = sourcePrebufferMs()};
}

bool FlacStreamDecoder::decodeNext() {
  if (atEnd) {
    return false;
  }
  check(process_single(), "decode");
  return get_state() != FLAC__STREAM_DECODER_END_OF_STREAM;
}

void FlacStreamDecoder::seekSource(uint64_t frame) {
  // libFLAC reads its way to the target, which a stalled source can hold up:
  // the seek is answered now, and a later one can cut it short.
  answerSeek();
  atEnd = flacInfo->total_samples && frame >= flacInfo->total_samples;
  if (atEnd) {
    return;
  }
  const auto state = get_state();
  if (state == FLAC__STREAM_DECODER_SEEK_ERROR ||
      state == FLAC__STREAM_DECODER_ABORTED) {
    check(flush(), "flush");
  }
  check(seek_absolute(frame), "seek");
}

::FLAC__StreamDecoderWriteStatus
FlacStreamDecoder::write_callback(const ::FLAC__Frame *frame,
                                  const FLAC__int32 *const buffer[]) {
  try {
    if (frame->header.channels != 2) {
      throw std::runtime_error("FLAC decoder supports stereo audio");
    }
    const auto format = bitsToFormat(frame->header.bits_per_sample);
    const auto blockSize = frame->header.blocksize;
    const auto bytes = sampleSize(format) * blockSize * frame->header.channels;
    if (bytes > converted.size()) {
      converted.resize(bytes);
    }
    convertToFormat(converted.data(), buffer, blockSize, format);
    write(std::span(converted.data(), bytes));
    return FLAC__STREAM_DECODER_WRITE_STATUS_CONTINUE;
  } catch (...) {
    keepCallbackFailure();
    return FLAC__STREAM_DECODER_WRITE_STATUS_ABORT;
  }
}

::FLAC__StreamDecoderReadStatus
FlacStreamDecoder::read_callback(FLAC__byte buffer[], size_t *bytes) {
  try {
    *bytes = readAvailable(buffer, *bytes);
    return *bytes ? FLAC__STREAM_DECODER_READ_STATUS_CONTINUE
                  : FLAC__STREAM_DECODER_READ_STATUS_END_OF_STREAM;
  } catch (...) {
    keepCallbackFailure();
    *bytes = 0;
    return FLAC__STREAM_DECODER_READ_STATUS_ABORT;
  }
}

void FlacStreamDecoder::error_callback(
    ::FLAC__StreamDecoderErrorStatus status) {
  if (!callbackFailed()) {
    keepCallbackFailure(std::make_exception_ptr(
        std::runtime_error(std::string("Flac decoder error: ") +
                           FLAC__StreamDecoderErrorStatusString[status])));
  }
}

void FlacStreamDecoder::metadata_callback(
    const ::FLAC__StreamMetadata *metadata) {
  if (metadata->type == FLAC__METADATA_TYPE_STREAMINFO) {
    flacInfo = metadata->data.stream_info;
  }
}

::FLAC__StreamDecoderSeekStatus
FlacStreamDecoder::seek_callback(FLAC__uint64 absolute_byte_offset) {
  try {
    return reposition(absolute_byte_offset)
               ? FLAC__STREAM_DECODER_SEEK_STATUS_OK
               : FLAC__STREAM_DECODER_SEEK_STATUS_UNSUPPORTED;
  } catch (...) {
    keepCallbackFailure();
    return FLAC__STREAM_DECODER_SEEK_STATUS_ERROR;
  }
}

::FLAC__StreamDecoderTellStatus
FlacStreamDecoder::tell_callback(FLAC__uint64 *absolute_byte_offset) {
  *absolute_byte_offset = sourcePosition();
  return FLAC__STREAM_DECODER_TELL_STATUS_OK;
}

::FLAC__StreamDecoderLengthStatus
FlacStreamDecoder::length_callback(FLAC__uint64 *stream_length) {
  try {
    *stream_length = sourceLength();
  } catch (...) {
    keepCallbackFailure();
    return FLAC__STREAM_DECODER_LENGTH_STATUS_ERROR;
  }
  return *stream_length ? FLAC__STREAM_DECODER_LENGTH_STATUS_OK
                        : FLAC__STREAM_DECODER_LENGTH_STATUS_ERROR;
}

bool FlacStreamDecoder::eof_callback() {
  try {
    const auto length = sourceLength();
    return length && sourcePosition() >= length;
  } catch (...) {
    return false;
  }
}

void FlacStreamDecoder::check(bool succeeded, const char *operation) {
  rethrowCallbackFailure();
  if (!succeeded) {
    throw std::runtime_error(std::string("FLAC ") + operation +
                             " failed: " + get_state().as_cstring());
  }
}
