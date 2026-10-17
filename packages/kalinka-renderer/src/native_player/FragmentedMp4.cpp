#include "FragmentedMp4.h"

#include <algorithm>
#include <optional>
#include <stdexcept>

using mp4::Bytes;
using mp4::integer;
using mp4::invalid;

namespace {
uint64_t read(Bytes data, size_t &position, size_t width = 4) {
  const auto value = integer(data, position, width);
  position += width;
  return value;
}

/// A track fragment's decode time, sample defaults and runs.
struct TrackFragment {
  uint64_t time = 0, duration = 0, size = 0;
  std::vector<mp4::Box> runs;
};

TrackFragment trackFragment(const mp4::Box &moof, const FragmentedMp4 &init) {
  const auto traf = mp4::inside(moof, "traf");
  const auto items = mp4::boxes(traf.payload, traf.offset + traf.header);
  mp4::refuseEncrypted(items);
  const auto header = mp4::one(items, "tfhd").payload;
  const auto flags = integer(header, 0, 4);
  if (flags & ~uint32_t(0x02003a) || integer(header, 4, 4) != init.track.id)
    invalid();
  size_t position = 8;
  if ((flags & 2) && read(header, position) != 1)
    invalid();
  TrackFragment result;
  result.duration = flags & 8 ? read(header, position) : init.defaultDuration;
  result.size = flags & 16 ? read(header, position) : init.defaultSize;
  if (flags & 32)
    read(header, position);
  if (position != header.size())
    invalid();
  const auto tfdt = mp4::one(items, "tfdt").payload;
  const auto version = integer(tfdt, 0, 4);
  if (version != 0 && version != 0x01000000)
    invalid();
  result.time = integer(tfdt, 4, version ? 8 : 4);
  for (const auto &item : items)
    if (item.type == "trun")
      result.runs.push_back(item);
  return result;
}

/// One `trun` box: where its data starts, if it says, and its samples.
struct Run {
  struct Sample {
    uint64_t duration, size;
  };
  std::optional<int32_t> dataOffset;
  std::vector<Sample> samples;
};

/// @param budget Samples left to a segment; a run may not exceed it.
Run run(Bytes trun, const TrackFragment &fragment, size_t &budget) {
  const auto flags = integer(trun, 0, 4);
  if (flags & ~uint32_t(0x01000f05))
    invalid();
  const auto count = integer(trun, 4, 4);
  if (!count || count > budget)
    invalid();
  budget -= count;
  size_t position = 8;
  Run result;
  if (flags & 1)
    result.dataOffset = int32_t(read(trun, position));
  if (flags & 4)
    read(trun, position);
  for (uint64_t i = 0; i < count; ++i) {
    Run::Sample sample{flags & 0x100 ? read(trun, position) : fragment.duration,
                       flags & 0x200 ? read(trun, position) : fragment.size};
    if (flags & 0x400)
      read(trun, position);
    if (((flags & 0x800) && read(trun, position) != 0) || !sample.duration ||
        !sample.size)
      invalid();
    result.samples.push_back(sample);
  }
  if (position != trun.size())
    invalid();
  return result;
}

constexpr size_t maxSamples = 100000;
} // namespace

FragmentedMp4 FragmentedMp4::parseInitialization(Bytes bytes) {
  const auto moov = mp4::one(mp4::boxes(bytes), "moov");
  FragmentedMp4 result;
  result.track = mp4::audioTrack(moov);
  const auto trex = mp4::inside(mp4::inside(moov, "mvex"), "trex").payload;
  if (integer(trex, 0, 4) != 0 || integer(trex, 4, 4) != result.track.id ||
      integer(trex, 8, 4) != 1)
    invalid();
  result.defaultDuration = integer(trex, 12, 4);
  result.defaultSize = integer(trex, 16, 4);
  return result;
}

std::vector<FragmentedMp4::Fragment>
FragmentedMp4::parseSegments(Bytes bytes) const {
  const auto top = mp4::boxes(bytes);
  std::vector<std::pair<size_t, size_t>> media;
  for (const auto &box : top)
    if (box.type == "mdat")
      media.emplace_back(box.offset + box.header,
                         box.offset + box.header + box.payload.size());
  std::vector<Fragment> result;
  size_t budget = maxSamples, totalBytes = 0;
  for (const auto &moof : top) {
    if (moof.type != "moof")
      continue;
    const auto header = trackFragment(moof, *this);
    Fragment fragment;
    fragment.time = header.time;
    size_t dataPosition = 0;
    for (const auto &box : header.runs) {
      const auto samples = run(box.payload, header, budget);
      if (samples.dataOffset) {
        const auto absolute = int64_t(moof.offset) + *samples.dataOffset;
        if (absolute < 0 || uint64_t(absolute) > bytes.size())
          invalid();
        dataPosition = size_t(absolute);
      } else if (!dataPosition)
        invalid();
      for (const auto &sample : samples.samples) {
        if (sample.size > bytes.size() - dataPosition ||
            sample.size > bytes.size() - totalBytes)
          invalid();
        const auto after =
            std::upper_bound(media.begin(), media.end(), dataPosition,
                             [](size_t offset, const auto &range) {
                               return offset < range.first;
                             });
        if (after == media.begin() || dataPosition > std::prev(after)->second ||
            sample.size > std::prev(after)->second - dataPosition)
          invalid();
        totalBytes += sample.size;
        fragment.durations.push_back(sample.duration);
        fragment.sizes.push_back(sample.size);
        fragment.data.insert(fragment.data.end(), bytes.begin() + dataPosition,
                             bytes.begin() + dataPosition + sample.size);
        dataPosition += sample.size;
      }
    }
    if (fragment.data.empty())
      invalid();
    result.push_back(std::move(fragment));
  }
  if (result.empty())
    invalid();
  return result;
}

std::vector<FragmentedMp4::Reference>
FragmentedMp4::parseIndex(Bytes sidx, uint64_t anchor) const {
  const auto version = integer(sidx, 0, 1);
  if (version > 1)
    invalid();
  if (integer(sidx, 4, 4) != track.id || integer(sidx, 8, 4) != track.timescale)
    return {};
  const size_t width = version ? 8 : 4;
  size_t position = 12;
  auto time = read(sidx, position, width);
  auto offset = anchor + read(sidx, position, width);
  position += 2;
  const auto count = read(sidx, position, 2);
  if (sidx.size() != position + 12 * count)
    invalid();
  std::vector<Reference> references;
  for (size_t i = 0; i < count; ++i) {
    const auto size = read(sidx, position);
    const auto duration = read(sidx, position);
    read(sidx, position);
    if (size >> 31)
      return {};
    references.push_back({offset, time, duration});
    offset += size;
    time += duration;
  }
  return references;
}

FragmentedMp4::Reference FragmentedMp4::timing(Bytes moof) const {
  const auto header = trackFragment({"moof", moof, 0, 0}, *this);
  Reference result{0, header.time, 0};
  size_t budget = maxSamples;
  for (const auto &box : header.runs)
    for (const auto &sample : run(box.payload, header, budget).samples)
      result.duration += sample.duration;
  return result;
}

std::vector<FragmentedMp4::Reference>
FragmentedMp4::parseRandomAccess(Bytes mfra) const {
  std::vector<Reference> references;
  for (const auto &tfra : mp4::boxes(mfra)) {
    if (tfra.type != "tfra" || integer(tfra.payload, 4, 4) != track.id)
      continue;
    const auto version = integer(tfra.payload, 0, 1);
    if (version > 1)
      invalid();
    const auto sizes = integer(tfra.payload, 8, 4);
    const size_t width = version ? 8 : 4;
    // Each entry ends with traf, trun and sample numbers of 1 to 4 bytes.
    const size_t numbers =
        ((sizes >> 4) & 3) + ((sizes >> 2) & 3) + (sizes & 3) + 3;
    const auto count = integer(tfra.payload, 12, 4);
    if (count > maxSamples ||
        tfra.payload.size() != 16 + count * (2 * width + numbers))
      invalid();
    for (size_t i = 0; i < count; ++i) {
      size_t position = 16 + i * (2 * width + numbers);
      const auto time = read(tfra.payload, position, width);
      const auto offset = read(tfra.payload, position, width);
      if (!references.empty() && offset <= references.back().offset) {
        if (offset == references.back().offset)
          continue;
        invalid();
      }
      references.push_back({offset, time, 0});
    }
  }
  return references;
}
