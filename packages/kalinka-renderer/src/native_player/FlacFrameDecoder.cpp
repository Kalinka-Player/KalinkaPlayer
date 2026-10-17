#include "FlacFrameDecoder.h"

#include <FLAC++/decoder.h>

#include <algorithm>
#include <cstring>
#include <exception>
#include <stdexcept>
#include <utility>
#include <vector>

namespace {
[[noreturn]] void invalidAudio() {
  throw std::runtime_error("Invalid FLAC audio in MP4");
}
} // namespace

/// libFLAC fed one frame per decode(), after the stream header once.
class FlacFrameDecoder::Stream final : private FLAC::Decoder::Stream {
public:
  Stream(std::span<const uint8_t> streamInfo, const StreamAudioFormat &format)
      : format(format) {
    input = {'f', 'L', 'a', 'C', 0x80, 0, 0, 34};
    input.insert(input.end(), streamInfo.begin(), streamInfo.end());
    // Whole-file length and MD5 do not apply to frames decoded on their own.
    input[8 + 13] &= 0xf0;
    std::fill(input.begin() + 8 + 14, input.end(), 0);
    set_md5_checking(false);
    if (init() != FLAC__STREAM_DECODER_INIT_STATUS_OK ||
        !process_until_end_of_metadata() || failed)
      throw std::runtime_error("Could not initialize the FLAC decoder");
  }

  std::span<const uint8_t> decode(std::span<const uint8_t> unit) {
    input.assign(unit.begin(), unit.end());
    position = 0;
    written = false;
    const auto ok = process_single();
    if (exception)
      std::rethrow_exception(std::exchange(exception, nullptr));
    if (!ok || failed || !written || position != input.size()) {
      restart();
      invalidAudio();
    }
    return pcm;
  }

  void restart() {
    failed = false;
    flush();
  }

private:
  const StreamAudioFormat format;
  std::vector<uint8_t> input, pcm;
  size_t position = 0;
  bool failed = false, written = false;
  std::exception_ptr exception;

  FLAC__StreamDecoderReadStatus read_callback(FLAC__byte *data,
                                              size_t *count) override {
    *count = std::min(*count, input.size() - position);
    if (!*count)
      return FLAC__STREAM_DECODER_READ_STATUS_END_OF_STREAM;
    std::memcpy(data, input.data() + position, *count);
    position += *count;
    return FLAC__STREAM_DECODER_READ_STATUS_CONTINUE;
  }

  FLAC__StreamDecoderWriteStatus
  write_callback(const FLAC__Frame *frame,
                 const FLAC__int32 *const samples[]) override {
    try {
      pack(*frame, samples);
      written = true;
    } catch (...) {
      exception = std::current_exception();
      return FLAC__STREAM_DECODER_WRITE_STATUS_ABORT;
    }
    return FLAC__STREAM_DECODER_WRITE_STATUS_CONTINUE;
  }

  void error_callback(FLAC__StreamDecoderErrorStatus) override {
    failed = true;
  }

  void pack(const FLAC__Frame &frame, const FLAC__int32 *const samples[]) {
    const auto &header = frame.header;
    if (written || header.sample_rate != format.sampleRate ||
        header.channels != format.channels ||
        header.bits_per_sample != format.bitsPerSample || !header.blocksize ||
        header.blocksize > 65536)
      throw std::runtime_error("FLAC audio format changed during playback");
    const size_t width = sampleSize(format.sampleFormat);
    // A padded container leaves its spare high byte clear, as FLAC does.
    const uint32_t mask =
        format.bitsPerSample < 32 ? (1u << format.bitsPerSample) - 1 : ~0u;
    pcm.resize(size_t(header.blocksize) * format.channels * width);
    for (size_t i = 0; i < header.blocksize; ++i)
      for (unsigned channel = 0; channel < format.channels; ++channel) {
        const auto sample = uint32_t(samples[channel][i]) & mask;
        auto *dest = pcm.data() + (i * format.channels + channel) * width;
        for (size_t byte = 0; byte < width; ++byte)
          dest[byte] = sample >> (8 * byte);
      }
  }
};

FlacFrameDecoder::FlacFrameDecoder(std::span<const uint8_t> streamInfo) {
  if (streamInfo.size() != 34)
    invalidAudio();
  uint64_t packed = 0;
  for (size_t i = 10; i < 18; ++i)
    packed = (packed << 8) | streamInfo[i];
  const auto rate = unsigned(packed >> 44);
  const auto channels = unsigned((packed >> 41) & 7) + 1;
  const auto bits = unsigned((packed >> 36) & 31) + 1;
  if (!rate || rate > 768000 || (channels != 1 && channels != 2) ||
      (bits != 16 && bits != 24 && bits != 32))
    throw std::runtime_error("Unsupported or invalid MP4 audio");
  audio = {rate, channels, bits,
           bits == 16   ? PCM16_LE
           : bits == 24 ? PCM24_LE
                        : PCM32_LE};
  stream = std::make_unique<Stream>(streamInfo, audio);
}

FlacFrameDecoder::~FlacFrameDecoder() = default;

void FlacFrameDecoder::reset() { stream->restart(); }

std::span<const uint8_t>
FlacFrameDecoder::decode(std::span<const uint8_t> unit) {
  return stream->decode(unit);
}
