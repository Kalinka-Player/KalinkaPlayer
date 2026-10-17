#include "Mp4StreamDecoder.h"
#include "FragmentedMp4.h"
#include "Mp4SampleTable.h"

#include <algorithm>
#include <array>
#include <stdexcept>
#include <utility>

namespace {
constexpr uint64_t maxIndexBytes = uint64_t(64) << 20;
constexpr uint64_t maxFragmentBytes = uint64_t(32) << 20;
constexpr uint64_t maxReadThroughBytes = uint64_t(256) << 10;

[[noreturn]] void truncated() {
  throw std::runtime_error("Truncated MP4 file");
}
} // namespace

/// @brief Where a movie's access units lie and when they play.
/// @note Reads through the decoder it is given, on the decoder's worker.
class Mp4StreamDecoder::Layout {
public:
  virtual ~Layout() = default;
  /// The timeline's length in output frames; 0 when the file does not say.
  virtual uint64_t length() const = 0;
  /// Makes next() start with the unit holding `frame`.
  virtual void seek(Mp4StreamDecoder &file, uint64_t frame) = 0;
  /// The next unit in decoding order; nothing once the audio ends.
  virtual std::optional<Unit> next(Mp4StreamDecoder &file) = 0;
};

class Mp4StreamDecoder::Progressive final : public Layout {
public:
  Progressive(Mp4SampleTable sampleTable, unsigned rate)
      : table(std::move(sampleTable)) {
    starts.reserve(table.times.size() + 1);
    for (const auto time : table.times)
      starts.push_back(samplesAt(time, table.track.timescale, rate));
    starts.push_back(samplesAt(table.end, table.track.timescale, rate));
  }

  uint64_t length() const override { return starts.back(); }

  void seek(Mp4StreamDecoder &, uint64_t frame) override {
    index = frame >= length()
                ? table.sizes.size()
                : std::upper_bound(starts.begin(), starts.end(), frame) -
                      starts.begin() - 1;
  }

  std::optional<Unit> next(Mp4StreamDecoder &file) override {
    if (index == table.sizes.size())
      return std::nullopt;
    file.moveTo(table.offsets[index]);
    bytes.resize(table.sizes[index]);
    file.readExact(bytes.data(), bytes.size());
    const Unit unit{bytes, starts[index], starts[index + 1] - starts[index]};
    ++index;
    return unit;
  }

private:
  const Mp4SampleTable table;
  std::vector<uint64_t> starts;
  size_t index = 0;
  std::vector<uint8_t> bytes;
};

class Mp4StreamDecoder::Fragmented final : public Layout {
public:
  /// @param listed The fragments a `sidx` or `mfra` index lists, in output
  /// frames; empty when the file has neither.
  Fragmented(FragmentedMp4 initialization, unsigned rate, uint64_t first,
             const std::vector<FragmentedMp4::Reference> &listed)
      : init(std::move(initialization)), rate(rate), firstFragment(first),
        nextOffset(first), complete(!listed.empty()) {
    for (const auto &reference : listed)
      index.push_back({reference.offset, reference.time});
    if (complete) {
      const auto &last = listed.back();
      total = last.time + last.duration;
    } else {
      // A header's length only bounds seeking; it need not land on a frame.
      total = samplesNear(init.track.duration, init.track.timescale, rate);
    }
  }

  uint64_t length() const override { return total; }

  void seek(Mp4StreamDecoder &file, uint64_t frame) override {
    if (!complete && (index.empty() || frame >= index.back().time))
      scan(file, frame);
    auto at = std::upper_bound(
        index.begin(), index.end(), frame,
        [](uint64_t value, const auto &entry) { return value < entry.time; });
    if (at != index.begin())
      --at;
    ended = at == index.end();
    if (!ended) {
      nextOffset = at->offset;
      expected = at->time;
    }
    fragment = {};
    unit = 0;
  }

  /// Starts over at the first fragment, which must begin at `time`.
  void rewind(uint64_t time) {
    nextOffset = firstFragment;
    expected = time;
    ended = false;
    fragment = {};
    unit = 0;
  }

  std::optional<Unit> next(Mp4StreamDecoder &file) override {
    while (unit == fragment.sizes.size())
      if (!load(file))
        return std::nullopt;
    const auto bytes = std::span<const uint8_t>(fragment.data)
                           .subspan(dataAt, fragment.sizes[unit]);
    const Unit result{
        bytes, unitTime,
        samplesAt(fragment.durations[unit], init.track.timescale, rate)};
    dataAt += bytes.size();
    unitTime += result.frames;
    ++unit;
    return result;
  }

private:
  struct Entry {
    uint64_t offset, time;
  };
  const FragmentedMp4 init;
  const unsigned rate;
  const uint64_t firstFragment;
  uint64_t total = 0, nextOffset = 0, unitTime = 0;
  std::vector<Entry> index;
  bool complete, ended = false;
  std::optional<uint64_t> expected;
  FragmentedMp4::Fragment fragment;
  size_t unit = 0, dataAt = 0;

  uint64_t frames(uint64_t time) const {
    return samplesAt(time, init.track.timescale, rate);
  }

  void record(uint64_t offset, uint64_t time) {
    if (index.empty() || offset > index.back().offset)
      index.push_back({offset, time});
  }

  /// Finds fragments past the index by their headers alone, until one
  /// starts after `frame` or the file ends.
  void scan(Mp4StreamDecoder &file, uint64_t frame) {
    file.moveTo(index.empty() ? firstFragment : index.back().offset);
    while (const auto box = file.nextBox()) {
      if (box->type == "moof") {
        const auto time =
            frames(init.timing(file.readPayload(*box, maxFragmentBytes)).time);
        record(box->offset, time);
        if (time > frame)
          return;
      }
      file.moveTo(box->offset + box->size);
    }
    complete = true;
  }

  bool load(Mp4StreamDecoder &file) {
    if (ended)
      return false;
    file.moveTo(nextOffset);
    while (const auto box = file.nextBox()) {
      if (box->type != "moof") {
        file.moveTo(box->offset + box->size);
        continue;
      }
      auto bytes = file.readBox(*box, maxFragmentBytes);
      const auto media = file.nextBox();
      if (!media || media->type != "mdat" || box->size > maxFragmentBytes ||
          media->size > maxFragmentBytes - box->size)
        mp4::invalid();
      const auto data = file.readBox(*media, maxFragmentBytes);
      bytes.insert(bytes.end(), data.begin(), data.end());
      auto fragments = init.parseSegments(bytes, box->offset);
      fragment = std::move(fragments.front());
      unitTime = frames(fragment.time);
      if (expected && unitTime != *expected)
        throw std::runtime_error("MP4 fragments are out of order");
      record(box->offset, unitTime);
      uint64_t length = 0;
      for (const auto duration : fragment.durations)
        length += samplesAt(duration, init.track.timescale, rate);
      expected = unitTime + length;
      nextOffset = media->offset + media->size;
      unit = dataAt = 0;
      return true;
    }
    ended = true;
    return false;
  }
};

/// A fragmented movie whose header lists its first units itself.
class Mp4StreamDecoder::Leading final : public Layout {
public:
  Leading(std::unique_ptr<Progressive> listed,
          std::unique_ptr<Fragmented> fragments)
      : head(std::move(listed)), tail(std::move(fragments)) {
    tail->rewind(head->length());
  }

  uint64_t length() const override { return tail->length(); }

  void seek(Mp4StreamDecoder &file, uint64_t frame) override {
    inHead = frame < head->length();
    if (!inHead)
      return tail->seek(file, frame);
    head->seek(file, frame);
    tail->rewind(head->length());
  }

  std::optional<Unit> next(Mp4StreamDecoder &file) override {
    if (inHead) {
      if (auto unit = head->next(file))
        return unit;
      inHead = false;
    }
    return tail->next(file);
  }

private:
  const std::unique_ptr<Progressive> head;
  const std::unique_ptr<Fragmented> tail;
  bool inHead = true;
};

std::vector<uint8_t> Mp4StreamDecoder::BoxHeader::bytes() const {
  std::vector<uint8_t> result;
  const auto wide = header == 16;
  const auto field = wide ? 1 : size;
  for (int shift = 24; shift >= 0; shift -= 8)
    result.push_back(uint8_t(field >> shift));
  result.insert(result.end(), type.begin(), type.end());
  if (wide)
    for (int shift = 56; shift >= 0; shift -= 8)
      result.push_back(uint8_t(size >> shift));
  return result;
}

Mp4StreamDecoder::Mp4StreamDecoder(std::optional<StreamId> id,
                                   size_t bufferSize, size_t startOffsetMs)
    : AudioGraphNode(id), SourceStreamDecoder(bufferSize, startOffsetMs) {}

Mp4StreamDecoder::~Mp4StreamDecoder() { stop(); }

std::vector<uint8_t> Mp4StreamDecoder::readPayload(const BoxHeader &box,
                                                   uint64_t limit) {
  if (box.size - box.header > limit)
    mp4::invalid();
  std::vector<uint8_t> payload(box.size - box.header);
  readExact(payload.data(), payload.size());
  return payload;
}

std::vector<uint8_t> Mp4StreamDecoder::readBox(const BoxHeader &box,
                                               uint64_t limit) {
  auto bytes = box.bytes();
  const auto payload = readPayload(box, limit);
  bytes.insert(bytes.end(), payload.begin(), payload.end());
  return bytes;
}

std::optional<Mp4StreamDecoder::BoxHeader> Mp4StreamDecoder::nextBox() {
  if (lookahead)
    return std::exchange(lookahead, std::nullopt);
  if (sourceSize && sourcePosition() >= sourceSize)
    return std::nullopt;
  std::array<uint8_t, 16> header;
  const auto count = readSome(header.data(), 8);
  if (!count)
    return std::nullopt;
  if (count < 8)
    truncated();
  BoxHeader box{std::string(reinterpret_cast<const char *>(&header[4]), 4),
                sourcePosition() - 8, mp4::integer(header, 0, 4), 8};
  if (box.size == 1) {
    readExact(&header[8], 8);
    box.size = mp4::integer(header, 8, 8);
    box.header = 16;
  } else if (!box.size) {
    if (!sourceSize)
      throw std::runtime_error("MP4 box of unknown length");
    box.size = sourceSize - box.offset;
  }
  if (box.size < box.header ||
      (sourceSize && box.size > sourceSize - box.offset))
    mp4::invalid();
  return box;
}

void Mp4StreamDecoder::moveTo(uint64_t offset) {
  if (lookahead && offset == lookahead->offset &&
      sourcePosition() == offset + lookahead->header)
    return;
  lookahead.reset();
  const auto position = sourcePosition();
  if (offset == position)
    return;
  // A seek drops what an HTTP source has buffered: read through short gaps.
  if (sourceSeeks &&
      (offset < position || offset - position > maxReadThroughBytes)) {
    if (!reposition(offset))
      throw std::runtime_error("MP4 stream seek failed");
    return;
  }
  if (offset < position)
    throw std::runtime_error("This MP4 file needs a seekable source");
  std::array<uint8_t, 4096> scratch;
  while (sourcePosition() < offset)
    readExact(scratch.data(),
              std::min<uint64_t>(offset - sourcePosition(), scratch.size()));
}

std::vector<FragmentedMp4::Reference>
Mp4StreamDecoder::randomAccess(const FragmentedMp4 &init) {
  try {
    std::array<uint8_t, 16> tail;
    if (sourceSize < tail.size())
      return {};
    moveTo(sourceSize - tail.size());
    readExact(tail.data(), tail.size());
    const auto size = mp4::integer(tail, 12, 4);
    if (mp4::integer(tail, 0, 8) != 0x000000106d66726full ||
        size > sourceSize || size > maxIndexBytes)
      return {};
    moveTo(sourceSize - size);
    const auto mfra = nextBox();
    if (!mfra || mfra->type != "mfra")
      return {};
    auto listed = init.parseRandomAccess(readPayload(*mfra, maxIndexBytes));
    if (listed.empty())
      return {};
    moveTo(listed.back().offset);
    const auto last = nextBox();
    if (!last || last->type != "moof")
      return {};
    const auto timing = init.timing(readPayload(*last, maxFragmentBytes));
    if (timing.time != listed.back().time)
      return {};
    listed.back().duration = timing.duration;
    return listed;
  } catch (const std::runtime_error &) {
    // The index only helps seeking; the fragments still play without it.
    return {};
  }
}

StreamInfo Mp4StreamDecoder::open() {
  sourceSize = 0;
  lookahead.reset();
  std::vector<uint8_t> moov, sidx;
  uint64_t sidxEnd = 0;
  std::optional<BoxHeader> media;
  auto box = nextBox();
  // An HTTP source knows its length once its first bytes have arrived.
  sourceSize = sourceLength();
  if (box && sourceSize && box->size > sourceSize)
    mp4::invalid();
  // Probe past the start: an HTTP source restarts at 0 even without ranges.
  sourceSeeks = box && reposition(sourcePosition());
  for (; box; box = nextBox()) {
    if (box->type == "moov" && moov.empty()) {
      moov = readBox(*box, maxIndexBytes);
      continue;
    }
    if (box->type == "mdat" || box->type == "moof") {
      if (!moov.empty()) {
        media = lookahead = box;
        break;
      }
      if (!sourceSeeks)
        throw std::runtime_error(
            "This MP4 file has its index after the audio and needs a "
            "seekable source");
    } else if (box->type == "sidx" && !moov.empty() && sidx.empty()) {
      sidx = readPayload(*box, maxIndexBytes);
      sidxEnd = box->offset + box->size;
      continue;
    }
    moveTo(box->offset + box->size);
  }
  if (moov.empty())
    throw std::runtime_error("The MP4 file has no movie index");

  const auto movie = mp4::one(mp4::boxes(moov), "moov");
  const auto children = mp4::boxes(movie.payload, movie.header);
  if (std::ranges::any_of(children,
                          [](const auto &box) { return box.type == "mvex"; })) {
    auto init = FragmentedMp4::parseInitialization(moov);
    track = TrackDecoder::forTrack(init.track);
    const auto rate = track->format().sampleRate;
    // Units the header lists lie in the media box that follows it.
    const auto leading = Mp4SampleTable::listsSamples(moov);
    const auto first = !media ? sourcePosition()
                       : leading && media->type == "mdat"
                           ? media->offset + media->size
                           : media->offset;
    std::vector<FragmentedMp4::Reference> listed;
    try {
      if (!sidx.empty())
        listed = init.inFrames(init.parseIndex(sidx, sidxEnd), rate);
    } catch (const std::runtime_error &) {
      // As with `mfra`, a broken index costs seeking, not playback.
    }
    if (listed.empty() && sourceSeeks && sourceSize)
      listed = init.inFrames(randomAccess(init), rate);
    auto fragments =
        std::make_unique<Fragmented>(std::move(init), rate, first, listed);
    if (leading)
      layout = std::make_unique<Leading>(
          std::make_unique<Progressive>(Mp4SampleTable::parse(moov), rate),
          std::move(fragments));
    else
      layout = std::move(fragments);
  } else {
    auto table = Mp4SampleTable::parse(moov);
    track = TrackDecoder::forTrack(table.track);
    layout = std::make_unique<Progressive>(std::move(table),
                                           track->format().sampleRate);
  }
  const auto length = track->setEnd(layout->length());
  setSeekable(sourceSeeks && length);
  // The layout starts at the first unit, which comes no later than the edit.
  track->restart(0);
  return {track->format(), StreamType::FRAMES,
          static_cast<unsigned long>(length)};
}

bool Mp4StreamDecoder::decodeNext() {
  const auto unit = layout->next(*this);
  if (!unit)
    return false;
  write(track->decode(unit->bytes, unit->first, unit->frames));
  return true;
}

void Mp4StreamDecoder::seekSource(uint64_t frame) {
  layout->seek(*this, track->restart(frame));
}

void Mp4StreamDecoder::close() {
  layout.reset();
  track.reset();
}
