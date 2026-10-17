#include "AdtsStreamDecoder.h"

#include <algorithm>
#include <array>
#include <stdexcept>

namespace {
/// How far past junk to look for the next frame before giving up.
constexpr size_t maxScan = 64 * 1024;
/// Frames read ahead to estimate the length of a stream of known size.
constexpr size_t sampleFrames = 64;
} // namespace

struct AdtsStreamDecoder::Header {
  unsigned profile, rateIndex, channels, blocks;
  size_t length, size;
  uint32_t fixed;
};

AdtsStreamDecoder::AdtsStreamDecoder(std::optional<StreamId> id,
                                     size_t bufferSize, size_t startOffsetMs)
    : AudioGraphNode(id), SourceStreamDecoder(bufferSize, startOffsetMs) {}

AdtsStreamDecoder::~AdtsStreamDecoder() { stop(); }

uint64_t AdtsStreamDecoder::consumed() const {
  return sourcePosition() - (window.size() - windowStart);
}

bool AdtsStreamDecoder::fill(size_t bytes) {
  while (window.size() - windowStart < bytes) {
    if (windowStart && windowStart >= window.size() / 2) {
      window.erase(window.begin(), window.begin() + windowStart);
      windowStart = 0;
    }
    const auto held = window.size();
    window.resize(held + 4096);
    const auto count = readSome(window.data() + held, 4096);
    window.resize(held + count);
    if (!count)
      return false;
  }
  return true;
}

void AdtsStreamDecoder::consume(size_t bytes) {
  const auto held = std::min(bytes, window.size() - windowStart);
  windowStart += held;
  bytes -= held;
  std::array<uint8_t, 4096> scratch;
  while (bytes) {
    const auto count = readSome(scratch.data(), std::min(bytes, scratch.size()));
    if (!count)
      return;
    bytes -= count;
  }
}

void AdtsStreamDecoder::skipTags() {
  while (fill(10)) {
    const auto *tag = window.data() + windowStart;
    if (tag[0] != 'I' || tag[1] != 'D' || tag[2] != '3')
      return;
    size_t size = 0;
    for (int i = 6; i < 10; ++i)
      size = (size << 7) | (tag[i] & 0x7f);
    // A footer, when flagged, repeats the 10-byte header after the tag.
    consume(10 + size + (tag[5] & 0x10 ? 10 : 0));
  }
}

std::optional<AdtsStreamDecoder::Header>
AdtsStreamDecoder::header(size_t at) const {
  const auto *b = window.data() + windowStart + at;
  if (b[0] != 0xff || (b[1] & 0xf6) != 0xf0)
    return std::nullopt;
  const Header result{
      unsigned(b[2] >> 6),
      unsigned((b[2] >> 2) & 15),
      unsigned(((b[2] & 1) << 2) | (b[3] >> 6)),
      unsigned(b[6] & 3),
      size_t(((b[3] & 3) << 11) | (b[4] << 3) | (b[5] >> 5)),
      size_t(b[1] & 1 ? 7 : 9),
      uint32_t(b[1] << 16 | (b[2] & 0xfd) << 8 | (b[3] & 0xf0)),
  };
  if (result.rateIndex > 12 || result.length < result.size)
    return std::nullopt;
  return result;
}

bool AdtsStreamDecoder::sync(bool confirm) {
  for (size_t scanned = 0;; ++scanned) {
    if (scanned > maxScan)
      throw std::runtime_error("Lost ADTS synchronization");
    if (!fill(7))
      return false;
    const auto found = header(0);
    if (found && (!fixed || found->fixed == *fixed)) {
      if (!confirm && !scanned)
        return true;
      if (!fill(found->length + 7))
        return fill(found->length);
      const auto after = header(found->length);
      if (after && after->fixed == found->fixed)
        return true;
    }
    consume(1);
  }
}

StreamInfo AdtsStreamDecoder::open() {
  window.clear();
  windowStart = 0;
  discard = 0;
  fixed.reset();
  skipTags();
  if (!sync(true))
    throw std::runtime_error("No ADTS audio found");
  // An HTTP source knows its length once its first bytes have arrived.
  sourceSize = sourceLength();
  const auto first = *header(0);
  if (!first.channels)
    throw std::runtime_error("Unsupported AAC configuration");
  fixed = first.fixed;
  dataStart = consumed();
  const unsigned object = first.profile + 1;
  const std::array<uint8_t, 2> config{
      uint8_t(object << 3 | first.rateIndex >> 1),
      uint8_t((first.rateIndex & 1) << 7 | first.channels << 3)};
  decoder = std::make_unique<AacDecoder>(config);
  const auto delay = decoder->delay();
  uint64_t length = 0;
  if (sourceSize > dataStart) {
    uint64_t bytes = 0, frames = 0;
    for (size_t at = 0; frames < sampleFrames && fill(at + 7); ++frames) {
      const auto next = header(at);
      if (!next || next->fixed != *fixed || !fill(at + next->length))
        break;
      bytes += next->length;
      at += next->length;
    }
    unitBytes = frames ? bytes / frames : first.length;
    length = (sourceSize - dataStart) / unitBytes * delay;
  }
  // Probe past the start: an HTTP source restarts at 0 even without ranges.
  const auto probe = dataStart ? dataStart : first.length;
  const auto seeks = reposition(probe);
  if (seeks) {
    if (probe != dataStart && !reposition(dataStart))
      throw std::runtime_error("ADTS stream seek failed");
    window.clear();
    windowStart = 0;
  }
  setSeekable(seeks && length > delay);
  return {decoder->format(), StreamType::FRAMES,
          static_cast<unsigned long>(length > delay ? length - delay : 0)};
}

bool AdtsStreamDecoder::decodeNext() {
  if (!sync(false))
    return false;
  const auto frame = *header(0);
  if (frame.blocks)
    throw std::runtime_error(
        "ADTS frames holding several AAC blocks are not supported");
  if (!fill(frame.length))
    return false;
  auto pcm = decoder->decode(std::span<const uint8_t>(window).subspan(
      windowStart + frame.size, frame.length - frame.size));
  consume(frame.length);
  const auto frameBytes =
      decoder->format().channels * sampleSize(decoder->format().sampleFormat);
  const auto skip = std::min<uint64_t>(discard, pcm.size() / frameBytes);
  discard -= skip;
  write(pcm.subspan(skip * frameBytes));
  return true;
}

void AdtsStreamDecoder::seekSource(uint64_t frame) {
  // Unit k only primes the decoder; unit k + 1 plays from frame k * delay.
  const auto delay = decoder->delay();
  const auto unit = frame / delay;
  // The last byte at most: a seek to the end of an HTTP source can fail.
  const auto offset = std::min(sourceSize - 1, dataStart + unit * unitBytes);
  if (!reposition(offset))
    throw std::runtime_error("ADTS stream seek failed");
  window.clear();
  windowStart = 0;
  decoder->reset();
  discard = frame - unit * delay;
}

void AdtsStreamDecoder::close() { decoder.reset(); }
