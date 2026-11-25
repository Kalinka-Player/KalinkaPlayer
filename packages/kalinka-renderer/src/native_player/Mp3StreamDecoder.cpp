// No MINIMP3_ONLY_SIMD: minimp3 sets it where SIMD is certain; a Pi 1 has none.
#define MINIMP3_IMPLEMENTATION

#include "Mp3StreamDecoder.h"

#include "minimp3/minimp3_ex.h"

#include <algorithm>
#include <array>
#include <cstring>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <utility>

namespace {
bool atId3Trailer(const mp3dec_ex_t &decoder, uint64_t sourcePosition,
                  uint64_t sourceSize) {
  // A failed frame search may consume the trailer and the last audio frame
  // together. Locate that search's input, which minimp3 still holds, rather
  // than trusting its sample estimate (the scan can omit frames near tags).
  if (sourcePosition < decoder.input_filled)
    return false;
  const auto bufferStart = sourcePosition - decoder.input_filled;
  if (decoder.offset < bufferStart || decoder.offset >= sourcePosition)
    return false;
  auto offset = decoder.offset;
  auto tail = std::span(decoder.file.buffer, decoder.input_filled)
                  .subspan(offset - bufferStart);
  if (tail.size() >= HDR_SIZE && hdr_valid(tail.data())) {
    const auto *header = tail.data();
    if (hdr_sample_rate_hz(header) != unsigned(decoder.info.hz) ||
        4 - HDR_GET_LAYER(header) != decoder.info.layer ||
        (HDR_IS_MONO(header) ? 1 : 2) != decoder.info.channels)
      return false;
    const auto bytes = hdr_frame_bytes(header, decoder.free_format_bytes) +
                       hdr_padding(header);
    if (bytes <= 0 || size_t(bytes) > tail.size())
      return false;
    tail = tail.subspan(bytes);
    offset += bytes;
  }
  if (tail.size() < MINIMP3_ID3_DETECT_SIZE || tail[3] < 2 || tail[3] > 4 ||
      tail[4] == 0xff || (tail[3] != 4 && (tail[5] & 0x10)))
    return false;
  const auto tagBytes = mp3dec_skip_id3v2(tail.data(), tail.size());
  return tagBytes && offset <= sourceSize && tagBytes == sourceSize - offset;
}

struct GaplessInfo {
  uint64_t remainingFrames = 0;
  unsigned delay = 0;
  unsigned padding = 0;

  std::pair<size_t, size_t> trim(size_t frames) {
    const auto skip = std::min<size_t>(delay, frames);
    delay -= skip;
    auto end = frames;
    if (remainingFrames) {
      end = std::min<uint64_t>(
          frames,
          remainingFrames - std::min<uint64_t>(remainingFrames, padding));
      remainingFrames -= std::min<uint64_t>(remainingFrames, frames);
    }
    // The tag describes this encoded track, not the lifetime of a broadcast.
    // If audio continues beyond it, keep playing without trimming that audio.
    return {skip, end > skip ? end - skip : 0};
  }
};

// The same Xing/Info and LAME/Lavc fields used by minimp3's file API, read
// within this frame's bounds. No duration scan, rewind or PCM tail buffer.
std::optional<GaplessInfo> readGaplessTag(std::span<const uint8_t> frame) {
  if (frame.size() < HDR_SIZE)
    return std::nullopt;
  const auto *header = frame.data();
  size_t at = HDR_SIZE + (HDR_IS_CRC(header) ? 2 : 0) +
              (HDR_TEST_MPEG1(header) ? (HDR_IS_MONO(header) ? 17 : 32)
                                      : (HDR_IS_MONO(header) ? 9 : 17));
  if (frame.size() < at + 8 || (std::memcmp(header + at, "Xing", 4) &&
                                std::memcmp(header + at, "Info", 4)))
    return std::nullopt;
  const auto flags = frame[at + 7];
  if (!(flags & FRAMES_FLAG))
    return GaplessInfo{}; // A tag without a length still isn't audio.
  at += 8;
  if (frame.size() < at + 4)
    return std::nullopt;
  const auto count = uint32_t(frame[at]) << 24 | uint32_t(frame[at + 1]) << 16 |
                     uint32_t(frame[at + 2]) << 8 | frame[at + 3];
  GaplessInfo info{uint64_t(count) * hdr_frame_samples(header)};
  at += 4 + (flags & BYTES_FLAG ? 4 : 0) + (flags & TOC_FLAG ? 100 : 0) +
        (flags & VBR_SCALE_FLAG ? 4 : 0);
  if (at >= frame.size())
    return std::nullopt;
  if (frame[at]) {
    if (frame.size() - at < 36)
      return std::nullopt;
    at += 21;
    // minimp3's synthesis delay is 529 samples in addition to the encoder's.
    info.delay = (unsigned(frame[at]) << 4 | frame[at + 1] >> 4) + 529;
    info.padding =
        std::max(0, ((frame[at + 1] & 15) << 8 | frame[at + 2]) - 529);
  }
  if (uint64_t(info.delay) + info.padding > info.remainingFrames)
    return GaplessInfo{}; // Ignore inconsistent lengths, but skip the tag.
  return info;
}
} // namespace

class Mp3StreamDecoder::Codec {
public:
  virtual ~Codec() = default;
  virtual StreamInfo open() = 0;
  /// Appends the next block through write(); false once the source is done.
  virtual bool decodeNext() = 0;
  /// Repositions to @p frame. Asked only of a codec whose open() made the
  /// decoder seekable.
  virtual void seek(uint64_t frame) = 0;
};

/// minimp3's extended API, which indexes the whole source as it opens.
class Mp3StreamDecoder::FileCodec final : public Codec {
public:
  explicit FileCodec(Mp3StreamDecoder &owner) : owner(owner) {}
  ~FileCodec() override { mp3dec_ex_close(&mp3); }

  StreamInfo open() override {
    const int result = mp3dec_ex_open_cb(&mp3, &io, MP3D_SEEK_TO_SAMPLE);
    owner.rethrowCallbackFailure();
    if (result != 0)
      throw std::runtime_error("Failed to open MP3 decoder");
    if (!mp3.info.channels || !mp3.info.hz)
      throw std::runtime_error("No MP3 audio found");
    owner.setSeekable(true);
    return {.format = {.sampleRate = unsigned(mp3.info.hz),
                       .channels = unsigned(mp3.info.channels),
                       .bitsPerSample = 16,
                       .sampleFormat = AudioSampleFormat::PCM16_LE},
            .streamType = StreamType::FRAMES,
            .streamSize = mp3.samples / mp3.info.channels};
  }

  bool decodeNext() override {
    if (atEnd)
      return false;
    const size_t samples = mp3dec_ex_read(&mp3, pcm.data(), pcm.size());
    owner.rethrowCallbackFailure();
    if (samples == 0) {
      // minimp3 also reports E_DECODE for non-audio trailers such as ID3v2.4.
      // Accept a tag covering the remainder of the file, while preserving
      // actual audio decoding and source failures.
      const bool trailer =
          mp3.last_error == MP3D_E_DECODE &&
          atId3Trailer(mp3, owner.inputPosition(), owner.sourceLength());
      if (mp3.last_error && !trailer)
        throw std::runtime_error("MP3 decode failure: " +
                                 std::to_string(mp3.last_error));
      return false;
    }
    owner.write(std::span(reinterpret_cast<const uint8_t *>(pcm.data()),
                          samples * sizeof(mp3d_sample_t)));
    return true;
  }

  void seek(uint64_t frame) override {
    // minimp3 reads its way to the target, which a stalled source can hold
    // up: the seek is answered now, and a later one can cut it short.
    owner.answerSeek();
    atEnd = frame >= mp3.samples / mp3.info.channels;
    if (atEnd)
      return;
    // minimp3 counts every channel's sample.
    const int result = mp3dec_ex_seek(&mp3, frame * mp3.info.channels);
    owner.rethrowCallbackFailure();
    if (result != 0)
      throw std::runtime_error("MP3 decoder seek failure");
  }

private:
  Mp3StreamDecoder &owner;
  mp3dec_ex_t mp3{};
  bool atEnd = false;
  std::array<mp3d_sample_t, MINIMP3_MAX_SAMPLES_PER_FRAME> pcm;
  // minimp3 is compiled here, but its state after an unwound read is its own
  // business: a callback keeps what it meets and reports a short read.
  mp3dec_io_t io{.read = [](void *buffer, size_t size, void *self) -> size_t {
                   auto &owner = static_cast<FileCodec *>(self)->owner;
                   try {
                     return owner.readInput(buffer, size, false);
                   } catch (...) {
                     owner.keepCallbackFailure();
                     return 0;
                   }
                 },
                 .read_data = this,
                 .seek = [](uint64_t position, void *self) -> int {
                   auto &owner = static_cast<FileCodec *>(self)->owner;
                   try {
                     // The prefix holds the source's first bytes: rewinding
                     // into it spares the source a seek.
                     if (position <= owner.prefix.size() &&
                         owner.sourcePosition() == owner.prefix.size()) {
                       owner.prefixUsed = position;
                       return 0;
                     }
                     owner.prefixUsed = owner.prefix.size();
                     return owner.reposition(position) ? 0 : -1;
                   } catch (...) {
                     owner.keepCallbackFailure();
                     return -1;
                   }
                 },
                 .seek_data = this};
};

/// minimp3's frame API, decoding a stream it can neither measure nor rewind.
class Mp3StreamDecoder::BroadcastCodec final : public Codec {
public:
  explicit BroadcastCodec(Mp3StreamDecoder &owner) : owner(owner) {
    mp3dec_init(&decoder);
  }

  StreamInfo open() override {
    pending = nextAudio();
    if (!pending)
      throw std::runtime_error("No MP3 audio found");
    format = pending->format;
    return {.format = *format,
            .streamType = StreamType::FRAMES,
            .prebufferMs = owner.sourcePrebufferMs()};
  }

  bool decodeNext() override {
    auto audio = std::exchange(pending, std::nullopt);
    if (!audio)
      audio = nextAudio();
    if (!audio)
      return false;
    if (audio->format != *format)
      throw std::runtime_error("MP3 audio format changed during playback");
    owner.write(
        std::span(reinterpret_cast<const uint8_t *>(pcm.data() + audio->skip),
                  audio->samples * sizeof(mp3d_sample_t)));
    return true;
  }

  void seek(uint64_t) override {
    throw std::logic_error("An MP3 broadcast does not seek");
  }

private:
  /// What one frame left to play, in samples of every channel.
  struct Audio {
    StreamAudioFormat format;
    size_t skip;
    size_t samples;
  };

  Mp3StreamDecoder &owner;
  // minimp3's recommended lookahead holds ten frames even at the largest
  // bitrate.
  std::array<uint8_t, MINIMP3_BUF_SIZE> encoded;
  std::array<uint8_t, HDR_SIZE> previousHeader{};
  std::array<mp3d_sample_t, MINIMP3_MAX_SAMPLES_PER_FRAME> pcm;
  mp3dec_t decoder{};
  size_t held = 0;
  bool eof = false;
  bool firstFrame = true;
  std::optional<GaplessInfo> gapless;
  std::optional<StreamAudioFormat> format;
  /// The first audible frame, decoded by open() and played first.
  std::optional<Audio> pending;

  // With a matching header at the expected boundary, the next frame complete.
  size_t completeFrameBytes() const {
    if (firstFrame || held < HDR_SIZE ||
        !hdr_compare(previousHeader.data(), encoded.data()))
      return 0;
    const auto bytes =
        hdr_frame_bytes(encoded.data(), decoder.free_format_bytes) +
        hdr_padding(encoded.data());
    return bytes > 0 && held >= size_t(bytes) ? bytes : 0;
  }

  /// Decodes on to the next frame with something to play, or the end.
  std::optional<Audio> nextAudio() {
    while (true) {
      // Once synchronized, decode complete buffered frames before asking for
      // more network data. In particular, skipping a Xing frame must not add
      // another full lookahead wait before the first audible samples.
      while (!completeFrameBytes() && held < encoded.size() && !eof) {
        const auto count =
            owner.readInput(encoded.data() + held, encoded.size() - held, true);
        held += count;
        eof = count == 0;
      }
      if (!held)
        return std::nullopt;
      mp3dec_frame_info_t info{};
      // Exactly one frame where its header says it ends. Otherwise minimp3
      // can reject the final audio frame because an ID3 trailer, rather than
      // another MPEG header, follows it.
      const auto frameBytes = completeFrameBytes();
      const auto frames = mp3dec_decode_frame(&decoder, encoded.data(),
                                              frameBytes ? frameBytes : held,
                                              pcm.data(), &info);
      if (!info.frame_bytes)
        return std::nullopt;
      bool tag = false;
      unsigned encodedFrames = 0;
      if (info.hz) {
        const auto frame = std::span(encoded).subspan(
            info.frame_offset, info.frame_bytes - info.frame_offset);
        std::copy_n(frame.data(), HDR_SIZE, previousHeader.data());
        encodedFrames = hdr_frame_samples(frame.data());
        if (firstFrame) {
          firstFrame = false;
          if (info.layer == 3)
            gapless = readGaplessTag(frame);
          tag = gapless.has_value();
        }
      }
      held -= info.frame_bytes;
      std::memmove(encoded.data(), encoded.data() + info.frame_bytes, held);
      if (tag) {
        mp3dec_init(&decoder); // File decoding also starts after the tag frame.
        continue;
      }
      const auto [skip, count] = gapless ? gapless->trim(encodedFrames)
                                         : std::pair<size_t, size_t>{0, frames};
      if (!frames)
        continue; // Skip junk or prime a stream joined mid-reservoir.
      const auto channels = unsigned(info.channels);
      return Audio{
          {unsigned(info.hz), channels, 16, AudioSampleFormat::PCM16_LE},
          skip * channels,
          count * channels};
    }
  }
};

Mp3StreamDecoder::Mp3StreamDecoder(std::optional<StreamId> streamId,
                                   size_t bufferSize, size_t startOffsetMs)
    : AudioGraphNode(streamId), SourceStreamDecoder(bufferSize, startOffsetMs) {
}

Mp3StreamDecoder::~Mp3StreamDecoder() { stop(); }

void Mp3StreamDecoder::forget() {
  codec.reset();
  prefix.clear();
  prefixUsed = 0;
}

StreamInfo Mp3StreamDecoder::open() {
  // An HTTP source names its length only once its first bytes have arrived.
  prefix.resize(4096);
  prefix.resize(readAvailable(prefix.data(), prefix.size()));
  prefixUsed = 0;
  if (sourceLength())
    codec = std::make_unique<FileCodec>(*this);
  else
    codec = std::make_unique<BroadcastCodec>(*this);
  return codec->open();
}

bool Mp3StreamDecoder::decodeNext() { return codec->decodeNext(); }

void Mp3StreamDecoder::seekSource(uint64_t frame) { codec->seek(frame); }

void Mp3StreamDecoder::close() { codec.reset(); }

size_t Mp3StreamDecoder::readInput(void *dest, size_t bytes, bool partial) {
  auto *out = static_cast<uint8_t *>(dest);
  const auto held = std::min(bytes, prefix.size() - prefixUsed);
  std::copy_n(prefix.data() + prefixUsed, held, out);
  prefixUsed += held;
  if (held == bytes || (partial && held))
    return held;
  return held + (partial ? readAvailable(out + held, bytes - held)
                         : readSome(out + held, bytes - held));
}

uint64_t Mp3StreamDecoder::inputPosition() const {
  return sourcePosition() - (prefix.size() - prefixUsed);
}

