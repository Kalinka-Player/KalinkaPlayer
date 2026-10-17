#include "Mp4.h"

#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>

uint64_t samplesAt(uint64_t time, uint64_t timescale, unsigned rate) {
  if (!timescale)
    throw std::runtime_error("MP4 timing has no timescale");
  const auto whole = time / timescale;
  const auto remainder = time % timescale;
  if (whole > 7 * 86400 || (remainder * rate) % timescale)
    throw std::runtime_error("MP4 timing does not match the audio samples");
  return whole * rate + remainder * rate / timescale;
}

uint64_t samplesNear(uint64_t time, uint64_t timescale, unsigned rate) {
  return timescale
             ? time / timescale * rate + time % timescale * rate / timescale
             : 0;
}

namespace mp4 {
void invalid() { throw std::runtime_error("Unsupported or invalid MP4 audio"); }

uint64_t integer(Bytes bytes, size_t offset, size_t width) {
  if (offset > bytes.size() || width > bytes.size() - offset)
    invalid();
  uint64_t value = 0;
  for (size_t i = 0; i < width; ++i)
    value = (value << 8) | bytes[offset + i];
  return value;
}

std::vector<Box> boxes(Bytes bytes, size_t origin) {
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

void refuseEncrypted(const std::vector<Box> &items) {
  for (const auto &item : items)
    if (item.type == "senc" || item.type == "saiz" || item.type == "saio" ||
        item.type == "sinf" || item.type == "enca")
      throw std::runtime_error("Encrypted MP4 audio is not supported");
}

namespace {
/// An MPEG-4 descriptor (ISO/IEC 14496-1): a tag, then a 7-bit-per-byte size.
struct Descriptor {
  uint8_t tag;
  Bytes body;
};

Descriptor descriptor(Bytes bytes, size_t &position) {
  const auto tag = uint8_t(integer(bytes, position++, 1));
  size_t size = 0;
  for (int i = 0;; ++i) {
    if (i == 4)
      invalid();
    const auto byte = integer(bytes, position++, 1);
    size = (size << 7) | (byte & 0x7f);
    if (!(byte & 0x80))
      break;
  }
  if (size > bytes.size() - position)
    invalid();
  const Descriptor result{tag, bytes.subspan(position, size)};
  position += size;
  return result;
}

std::vector<uint8_t> audioSpecificConfig(Bytes esds) {
  if (integer(esds, 0, 4) != 0)
    invalid();
  size_t position = 4;
  const auto es = descriptor(esds, position);
  if (es.tag != 0x03)
    invalid();
  const auto flags = integer(es.body, 2, 1);
  position = 3;
  if (flags & 0x80)
    position += 2;
  if (flags & 0x40)
    position += 1 + integer(es.body, position, 1);
  if (flags & 0x20)
    position += 2;
  const auto decoder = descriptor(es.body, position);
  // MPEG-4 audio, or MPEG-2 AAC Main, LC or SSR.
  const auto object = integer(decoder.body, 0, 1);
  if (decoder.tag != 0x04 ||
      (object != 0x40 && (object < 0x66 || object > 0x68)))
    invalid();
  position = 13;
  const auto specific = descriptor(decoder.body, position);
  if (specific.tag != 0x05 || specific.body.empty() ||
      specific.body.size() > 256)
    invalid();
  return {specific.body.begin(), specific.body.end()};
}

std::vector<uint8_t> flacStreamInfo(Bytes dfla) {
  if (dfla.size() < 42 || integer(dfla, 0, 4) != 0 || (dfla[4] & 0x7f) != 0 ||
      integer(dfla, 5, 3) != 34)
    invalid();
  return {dfla.begin() + 8, dfla.begin() + 42};
}

/// Reads one AudioSampleEntry into `track`.
void sampleEntry(const Box &entry, AudioTrack &track) {
  const auto sample = entry.payload;
  if (sample.size() < 28 || integer(sample, 6, 2) != 1)
    invalid();
  // QuickTime sound description version 1 adds 16 bytes before the children.
  const auto version = integer(sample, 8, 2);
  if (version > 1 || (version && entry.type != "mp4a"))
    invalid();
  const auto extensions = boxes(sample.subspan(version ? 44 : 28));
  refuseEncrypted(extensions);
  if (entry.type == "fLaC") {
    track.codec = AudioTrack::Codec::FLAC;
    track.config = flacStreamInfo(one(extensions, "dfLa").payload);
  } else if (entry.type == "mp4a") {
    track.codec = AudioTrack::Codec::AAC;
    track.config = audioSpecificConfig(one(extensions, "esds").payload);
  } else
    invalid();
}

bool isSound(const Box &trak) {
  const auto handler = inside(inside(trak, "mdia"), "hdlr").payload;
  return integer(handler, 8, 4) == 0x736f756e; // 'soun'
}

/// A full box's duration field, at `narrow` in version 0 and `wide` in
/// version 1, with the all-ones value that means "unknown" read as 0.
uint64_t duration(Bytes full, size_t narrow, size_t wide) {
  const auto version1 = integer(full, 0, 1) == 1;
  const auto value = integer(full, version1 ? wide : narrow, version1 ? 8 : 4);
  const auto unknown = version1 ? ~uint64_t(0) : uint64_t(0xffffffff);
  return value == unknown ? 0 : value;
}

uint64_t movieTimescale(const Box &moov) {
  const auto mvhd = inside(moov, "mvhd").payload;
  return integer(mvhd, integer(mvhd, 0, 1) == 1 ? 20 : 12, 4);
}

/// `time` in `from` units as `to` units, rounded; 0 where it would overflow.
uint64_t rescale(uint64_t time, uint64_t from, uint64_t to) {
  if (!from || time > (~uint64_t(0) - from / 2) / to)
    return 0;
  return (time * to + from / 2) / from;
}

std::optional<Box> child(const Box &box, std::string_view type) {
  for (const auto &item : boxes(box.payload, box.offset + box.header))
    if (item.type == type)
      return item;
  return std::nullopt;
}

struct Edit {
  uint64_t start, length;
};

/// The one edit that plays media at normal rate, after any empty edits;
/// nothing for an edit list this player cannot follow.
std::optional<Edit> editList(const Box &moov, const Box &trak,
                             uint32_t timescale) {
  const auto edts = child(trak, "edts");
  if (!edts)
    return std::nullopt;
  // Edit lengths count in the movie's timescale, not the track's.
  const auto movieScale = movieTimescale(moov);
  const auto elst = inside(*edts, "elst").payload;
  const auto version = integer(elst, 0, 1);
  const size_t width = version ? 8 : 4;
  const auto count = integer(elst, 4, 4);
  if (version > 1 || elst.size() != 8 + count * (2 * width + 4))
    invalid();
  std::optional<Edit> found;
  for (size_t i = 0; i < count; ++i) {
    const auto at = 8 + i * (2 * width + 4);
    const auto length = integer(elst, at, width);
    const auto time = integer(elst, at + width, width);
    const auto empty = time == (version ? ~uint64_t(0) : 0xffffffffu);
    if (empty && !found)
      continue;
    const auto negative = time >> (8 * width - 1);
    if (empty || negative || found ||
        integer(elst, at + 2 * width, 4) != 0x00010000)
      return std::nullopt;
    found = Edit{time, rescale(length, movieScale, timescale)};
  }
  return found;
}

/// The priming and length iTunes' `iTunSMPB` tag gives, for files without
/// an edit list.
std::optional<Edit> gaplessTag(const Box &moov) {
  try {
    const auto udta = child(moov, "udta");
    const auto meta = udta ? child(*udta, "meta") : std::nullopt;
    if (!meta)
      return std::nullopt;
    // iTunes' `meta` is a full box: its children follow version and flags.
    const auto items = boxes(meta->payload.subspan(4));
    const auto ilst = one(items, "ilst");
    for (const auto &item : boxes(ilst.payload)) {
      if (item.type != "----")
        continue;
      const auto fields = boxes(item.payload);
      const auto name = one(fields, "name").payload.subspan(4);
      if (std::string_view(reinterpret_cast<const char *>(name.data()),
                           name.size()) != "iTunSMPB")
        continue;
      const auto data = one(fields, "data").payload.subspan(8);
      std::istringstream text(std::string(
          reinterpret_cast<const char *>(data.data()), data.size()));
      uint64_t zero, priming, padding, length;
      if (text >> std::hex >> zero >> priming >> padding >> length)
        return Edit{priming, length};
    }
  } catch (const std::runtime_error &) {
  }
  // A tag it cannot read leaves the track as it is, merely not gapless.
  return std::nullopt;
}

/// The movie's own length in track units, for fragmented files whose
/// track header leaves it out.
uint64_t movieDuration(const Box &moov, uint32_t timescale) {
  const auto children = boxes(moov.payload, moov.offset + moov.header);
  const auto movieScale = movieTimescale(moov);
  auto length = duration(one(children, "mvhd").payload, 16, 24);
  for (const auto &child : children)
    if (child.type == "mvex")
      for (const auto &item : boxes(child.payload, child.offset + child.header))
        if (item.type == "mehd")
          length = duration(item.payload, 4, 4);
  if (!movieScale || length > ~uint64_t(0) / timescale)
    return 0;
  return length * timescale / movieScale;
}
} // namespace

Box soundTrack(const Box &moov) {
  for (const auto &trak : boxes(moov.payload, moov.offset + moov.header))
    if (trak.type == "trak" && isSound(trak))
      return trak;
  throw std::runtime_error("The MP4 file has no audio track");
}

AudioTrack audioTrack(const Box &moov) {
  const auto sound = soundTrack(moov);
  const auto tkhd = inside(sound, "tkhd").payload;
  const auto media = inside(sound, "mdia");
  const auto mdhd = inside(media, "mdhd").payload;
  if (integer(tkhd, 0, 1) > 1 || integer(mdhd, 0, 1) > 1)
    invalid();
  AudioTrack track;
  track.id = integer(tkhd, tkhd[0] ? 20 : 12, 4);
  track.timescale = integer(mdhd, mdhd[0] ? 20 : 12, 4);
  if (!track.id || !track.timescale || track.timescale > 1000000000)
    invalid();
  const auto minf = inside(media, "minf");
  const auto dref = inside(inside(minf, "dinf"), "dref").payload;
  if (integer(dref, 4, 4) != 1 || dref.size() < 8)
    invalid();
  // QuickTime writers name the movie's own file with an `alis` entry instead.
  const auto references = boxes(dref.subspan(8));
  if (references.size() != 1 ||
      (references.front().type != "url " &&
       references.front().type != "alis") ||
      integer(references.front().payload, 0, 4) != 1)
    invalid();
  const auto stsd = inside(inside(minf, "stbl"), "stsd").payload;
  if (integer(stsd, 4, 4) != 1 || stsd.size() < 8)
    invalid();
  const auto entries = boxes(stsd.subspan(8));
  refuseEncrypted(entries);
  if (entries.size() != 1)
    invalid();
  sampleEntry(entries.front(), track);
  track.duration = duration(mdhd, 16, 24);
  if (!track.duration)
    track.duration = movieDuration(moov, track.timescale);
  auto edit = editList(moov, sound, track.timescale);
  if (!edit && !child(sound, "edts"))
    edit = gaplessTag(moov);
  if (edit) {
    track.editStart = edit->start;
    track.editLength = edit->length;
  }
  return track;
}
} // namespace mp4
