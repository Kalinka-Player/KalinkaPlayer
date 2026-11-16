#include "VorbisStreamDecoder.h"

#include <array>
#include <cerrno>
#include <climits>
#include <limits>
#include <span>
#include <stdexcept>
#include <utility>

VorbisStreamDecoder::VorbisStreamDecoder(std::optional<StreamId> streamId,
                                         size_t bufferSize,
                                         size_t startOffsetMs)
    : AudioGraphNode(streamId), SourceStreamDecoder(bufferSize, startOffsetMs) {
  if (bufferSize == 0) {
    throw std::invalid_argument("Vorbis decoder buffer must not be empty");
  }
}

VorbisStreamDecoder::~VorbisStreamDecoder() { stop(); }

void VorbisStreamDecoder::forget() {
  file = {};
  opened = false;
  resetBeforeSeek = false;
  callbackException = nullptr;
}

StreamInfo VorbisStreamDecoder::open() {
  // The seekability probe runs before libvorbisfile's first read. Prime the
  // input so an HTTP source has received its headers and published length.
  std::array<char, 4096> initial;
  const auto initialBytes = readAvailable(initial.data(), initial.size());
  const ov_callbacks callbacks{readCallback, seekCallback, nullptr,
                               tellCallback};
  const int result = ov_open_callbacks(this, &file, initial.data(),
                                       long(initialBytes), callbacks);
  opened = result == 0;
  check(result, "open");
  const auto *info = ov_info(&file, -1);
  if (!info) {
    throw std::runtime_error("Vorbis stream info is missing");
  }
  const bool seekable = ov_seekable(&file) != 0;
  const auto totalFrames = seekable ? ov_pcm_total(&file, -1) : 0;
  check(long(totalFrames), "length");
  const StreamAudioFormat format{.sampleRate = unsigned(info->rate),
                                 .channels = unsigned(info->channels),
                                 .bitsPerSample = 16,
                                 .sampleFormat = AudioSampleFormat::PCM16_LE};
  validateFormat(info, format);
  if (seekable) {
    for (int link = 0; link < ov_streams(&file); ++link) {
      validateFormat(ov_info(&file, link), format);
    }
  }
  setSeekable(seekable);
  return {.format = format,
          .streamType = StreamType::FRAMES,
          .streamSize = static_cast<unsigned long>(totalFrames)};
}

bool VorbisStreamDecoder::decodeNext() {
  std::array<char, 8192> pcm;
  int section = 0;
  const auto bytes = ov_read(&file, pcm.data(), pcm.size(), 0, 2, 1, &section);
  // A missing or corrupt page is recoverable: libvorbisfile has moved on to
  // the next one.
  if (bytes == OV_HOLE && !callbackException) {
    return true;
  }
  check(bytes, "decode");
  if (bytes == 0) {
    return false;
  }
  validateFormat(ov_info(&file, -1), streamInfo().format);
  write(
      std::span(reinterpret_cast<const uint8_t *>(pcm.data()), size_t(bytes)));
  return true;
}

void VorbisStreamDecoder::seekSource(uint64_t frame) {
  // libvorbisfile reads its way to the target, which a stalled source can
  // hold up: the seek is answered now, and a later one can cut it short.
  answerSeek();
  if (resetBeforeSeek) {
    check(ov_raw_seek(&file, 0), "reset seek");
    resetBeforeSeek = false;
  }
  resetBeforeSeek = true;
  check(ov_pcm_seek(&file, ogg_int64_t(frame)), "seek");
  resetBeforeSeek = false;
}

void VorbisStreamDecoder::close() {
  if (opened) {
    ov_clear(&file);
    opened = false;
  }
}

size_t VorbisStreamDecoder::readInput(void *data, size_t size, size_t count) {
  errno = 0;
  if (size == 0 || count == 0) {
    return 0;
  }
  if (count > std::numeric_limits<size_t>::max() / size) {
    throw std::runtime_error("Vorbis input read is too large");
  }
  // libvorbisfile asks in bytes (size == 1); preserve fread's item count.
  return readAvailable(data, size * count) / size;
}

size_t VorbisStreamDecoder::readCallback(void *data, size_t size, size_t count,
                                         void *source) noexcept {
  auto &self = *static_cast<VorbisStreamDecoder *>(source);
  try {
    return self.readInput(data, size, count);
  } catch (...) {
    self.callbackException = std::current_exception();
    errno = EIO;
    return 0;
  }
}

int VorbisStreamDecoder::seekInput(ogg_int64_t offset, int whence) {
  // Without a length, SEEK_END cannot be implemented. Failing the initial
  // seek probe makes libvorbisfile decode sequentially.
  const auto length = sourceLength();
  if (!length || length > LONG_MAX) {
    return -1;
  }
  ogg_int64_t base;
  switch (whence) {
  case SEEK_SET:
    base = 0;
    break;
  case SEEK_CUR:
    base = ogg_int64_t(sourcePosition());
    break;
  case SEEK_END:
    base = ogg_int64_t(length);
    break;
  default:
    return -1;
  }
  if (offset < -base || offset > ogg_int64_t(length) - base) {
    return -1;
  }
  return reposition(uint64_t(base + offset)) ? 0 : -1;
}

int VorbisStreamDecoder::seekCallback(void *source, ogg_int64_t offset,
                                      int whence) noexcept {
  auto &self = *static_cast<VorbisStreamDecoder *>(source);
  try {
    return self.seekInput(offset, whence);
  } catch (...) {
    self.callbackException = std::current_exception();
    return -1;
  }
}

long VorbisStreamDecoder::tellCallback(void *source) noexcept {
  const auto position =
      static_cast<VorbisStreamDecoder *>(source)->sourcePosition();
  return position > LONG_MAX ? -1 : long(position);
}

void VorbisStreamDecoder::check(long result, const char *operation) {
  if (callbackException) {
    std::rethrow_exception(std::exchange(callbackException, nullptr));
  }
  if (result < 0) {
    throw std::runtime_error(std::string("Vorbis ") + operation + " failed (" +
                             std::to_string(result) + ")");
  }
}

void VorbisStreamDecoder::validateFormat(
    const vorbis_info *info, const StreamAudioFormat &format) const {
  if (!info || info->rate <= 0 || info->channels < 1 || info->channels > 2) {
    throw std::runtime_error("Vorbis decoder supports mono and stereo audio");
  }
  if (unsigned(info->rate) != format.sampleRate ||
      unsigned(info->channels) != format.channels) {
    throw std::runtime_error(
        "Vorbis chained streams must use the same audio format");
  }
}
