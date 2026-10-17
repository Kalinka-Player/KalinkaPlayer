#include "FragmentedFlac.h"

#include <algorithm>
#include <stdexcept>
#include <string_view>

namespace {
using Bytes = std::span<const uint8_t>;
[[noreturn]] void invalid() {
  throw std::runtime_error("Unsupported or invalid fragmented FLAC container");
}
uint64_t integer(Bytes bytes, size_t offset, size_t width) {
  if (offset > bytes.size() || width > bytes.size() - offset)
    invalid();
  uint64_t value = 0;
  for (size_t i = 0; i < width; ++i)
    value = (value << 8) | bytes[offset + i];
  return value;
}
struct Box {
  std::string_view type;
  Bytes payload;
  size_t offset, header;
};
std::vector<Box> boxes(Bytes bytes, size_t origin = 0) {
  std::vector<Box> result;
  size_t position = 0;
  while (position < bytes.size()) {
    auto size = integer(bytes, position, 4);
    integer(bytes, position, 8);
    const auto type = std::string_view(
        reinterpret_cast<const char *>(bytes.data() + position + 4), 4);
    size_t header = 8;
    if (size == 1) {
      size = integer(bytes, position + 8, 8);
      header = 16;
    } else if (!size)
      size = bytes.size() - position;
    if (size < header || size > bytes.size() - position ||
        result.size() >= 100000)
      invalid();
    result.push_back({type, bytes.subspan(position + header, size - header),
                      origin + position, header});
    position += size;
  }
  return result;
}
Box one(const std::vector<Box> &items, std::string_view type) {
  std::optional<Box> result;
  for (const auto &item : items)
    if (item.type == type) {
      if (result)
        invalid();
      result = item;
    }
  if (!result)
    invalid();
  return *result;
}
Box inside(const Box &box, std::string_view type) {
  return one(boxes(box.payload, box.offset + box.header), type);
}
uint64_t read(Bytes data, size_t &position, size_t width = 4) {
  const auto value = integer(data, position, width);
  position += width;
  return value;
}
void encrypted(const std::vector<Box> &items) {
  for (const auto &item : items)
    if (item.type == "senc" || item.type == "saiz" || item.type == "saio" ||
        item.type == "sinf" || item.type == "enca")
      throw std::runtime_error("Encrypted DASH audio is not supported");
}
} // namespace

FragmentedFlac FragmentedFlac::parseInitialization(Bytes bytes) {
  const auto moov = one(boxes(bytes), "moov");
  const auto trak = inside(moov, "trak");
  const auto tkhd = inside(trak, "tkhd").payload;
  const auto media = inside(trak, "mdia");
  const auto mdhd = inside(media, "mdhd").payload;
  if (integer(tkhd, 0, 1) > 1 || integer(mdhd, 0, 1) > 1)
    invalid();
  FragmentedFlac result;
  result.trackId = integer(tkhd, tkhd[0] ? 20 : 12, 4);
  result.timescale = integer(mdhd, mdhd[0] ? 20 : 12, 4);
  if (!result.trackId || !result.timescale || result.timescale > 1000000000)
    invalid();
  const auto handler = inside(media, "hdlr").payload;
  if (integer(handler, 8, 4) != 0x736f756e)
    invalid(); // 'soun'
  const auto minf = inside(media, "minf");
  const auto dref = inside(inside(minf, "dinf"), "dref").payload;
  if (integer(dref, 4, 4) != 1 || dref.size() < 8)
    invalid();
  const auto reference = one(boxes(dref.subspan(8)), "url ").payload;
  if (integer(reference, 0, 4) != 1)
    invalid();
  const auto stsd = inside(inside(minf, "stbl"), "stsd").payload;
  if (integer(stsd, 4, 4) != 1 || stsd.size() < 8)
    invalid();
  const auto entries = boxes(stsd.subspan(8));
  encrypted(entries);
  const auto sample = one(entries, "fLaC").payload;
  if (sample.size() < 28 || integer(sample, 6, 2) != 1 ||
      integer(sample, 8, 2) != 0)
    invalid();
  const auto extensions = boxes(sample.subspan(28));
  encrypted(extensions);
  const auto config = one(extensions, "dfLa").payload;
  if (config.size() < 42 || integer(config, 0, 4) != 0 ||
      (config[4] & 0x7f) != 0 || integer(config, 5, 3) != 34)
    invalid();
  std::copy_n(config.begin() + 8, 34, result.streamInfo.begin());
  const auto packed = integer(result.streamInfo, 10, 8);
  const auto rate = unsigned(packed >> 44);
  const auto channels = unsigned((packed >> 41) & 7) + 1;
  const auto bits = unsigned((packed >> 36) & 31) + 1;
  if (!rate || rate > 768000 || (channels != 1 && channels != 2) ||
      (bits != 16 && bits != 24 && bits != 32))
    invalid();
  result.format = {rate, channels, bits,
                   bits == 16   ? PCM16_LE
                   : bits == 24 ? PCM24_LE
                                : PCM32_LE};
  const auto trex = inside(inside(moov, "mvex"), "trex").payload;
  if (integer(trex, 0, 4) != 0 || integer(trex, 4, 4) != result.trackId ||
      integer(trex, 8, 4) != 1)
    invalid();
  result.defaultDuration = integer(trex, 12, 4);
  result.defaultSize = integer(trex, 16, 4);
  // Whole-file length and MD5 do not apply to independently decoded fragments.
  result.streamInfo[13] &= 0xf0;
  std::fill(result.streamInfo.begin() + 14, result.streamInfo.end(), 0);
  return result;
}

uint64_t samplesAt(uint64_t time, uint64_t timescale, unsigned rate) {
  if (!timescale)
    throw std::runtime_error("DASH timing has no timescale");
  const auto whole = time / timescale;
  const auto remainder = time % timescale;
  if (whole > 7 * 86400 || (remainder * rate) % timescale)
    throw std::runtime_error("DASH timing does not match the audio samples");
  return whole * rate + remainder * rate / timescale;
}

uint64_t FragmentedFlac::samplePosition(uint64_t time) const {
  return samplesAt(time, timescale, format.sampleRate);
}

std::vector<FragmentedFlac::Fragment>
FragmentedFlac::parseSegments(Bytes bytes) const {
  const auto top = boxes(bytes);
  std::vector<std::pair<size_t, size_t>> media;
  for (const auto &box : top)
    if (box.type == "mdat")
      media.emplace_back(box.offset + box.header,
                         box.offset + box.header + box.payload.size());
  std::vector<Fragment> result;
  size_t totalSamples = 0, totalBytes = 0;
  for (const auto &moof : top) {
    if (moof.type != "moof")
      continue;
    const auto traf = inside(moof, "traf");
    const auto items = boxes(traf.payload, traf.offset + traf.header);
    encrypted(items);
    const auto header = one(items, "tfhd").payload;
    const auto flags = integer(header, 0, 4);
    if (flags & ~uint32_t(0x02003a) || integer(header, 4, 4) != trackId)
      invalid();
    size_t position = 8;
    if ((flags & 2) && read(header, position) != 1)
      invalid();
    const auto duration = flags & 8 ? read(header, position) : defaultDuration;
    const auto size = flags & 16 ? read(header, position) : defaultSize;
    if (flags & 32)
      read(header, position);
    if (position != header.size())
      invalid();
    const auto tfdt = one(items, "tfdt").payload;
    const auto version = integer(tfdt, 0, 4);
    if (version != 0 && version != 0x01000000)
      invalid();
    Fragment fragment;
    fragment.time = integer(tfdt, 4, version ? 8 : 4);
    samplePosition(fragment.time);
    size_t dataPosition = 0;
    for (const auto &run : items) {
      if (run.type != "trun")
        continue;
      const auto data = run.payload;
      const auto runFlags = integer(data, 0, 4);
      if (runFlags & ~uint32_t(0x01000f05))
        invalid();
      const auto count = integer(data, 4, 4);
      if (!count || count > 100000 - totalSamples)
        invalid();
      totalSamples += count;
      position = 8;
      if (runFlags & 1) {
        const auto offset = int32_t(read(data, position));
        const auto absolute = int64_t(moof.offset) + offset;
        if (absolute < 0 || uint64_t(absolute) > bytes.size())
          invalid();
        dataPosition = size_t(absolute);
      } else if (!dataPosition)
        invalid();
      if (runFlags & 4)
        read(data, position);
      for (uint64_t i = 0; i < count; ++i) {
        const auto sampleDuration =
            runFlags & 0x100 ? read(data, position) : duration;
        const auto sampleSize = runFlags & 0x200 ? read(data, position) : size;
        if (runFlags & 0x400)
          read(data, position);
        if ((runFlags & 0x800) && read(data, position) != 0)
          invalid();
        if (!sampleDuration || !sampleSize ||
            sampleSize > bytes.size() - dataPosition ||
            sampleSize > bytes.size() - totalBytes)
          invalid();
        const auto after =
            std::upper_bound(media.begin(), media.end(), dataPosition,
                             [](size_t offset, const auto &range) {
                               return offset < range.first;
                             });
        if (after == media.begin() || dataPosition > std::prev(after)->second ||
            sampleSize > std::prev(after)->second - dataPosition)
          invalid();
        totalBytes += sampleSize;
        fragment.durations.push_back(sampleDuration);
        fragment.frames.insert(fragment.frames.end(),
                               bytes.begin() + dataPosition,
                               bytes.begin() + dataPosition + sampleSize);
        dataPosition += sampleSize;
      }
      if (position != data.size())
        invalid();
    }
    if (fragment.frames.empty())
      invalid();
    result.push_back(std::move(fragment));
  }
  if (result.empty())
    invalid();
  return result;
}
