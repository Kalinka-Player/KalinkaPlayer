#include "Mp4SampleTable.h"

#include <algorithm>

using mp4::Bytes;
using mp4::integer;
using mp4::invalid;

namespace {
/// About 13 hours of AAC at 44.1 kHz, so a forged count cannot exhaust memory.
constexpr uint64_t maxUnits = uint64_t(1) << 21;
constexpr uint64_t maxUnitBytes = uint64_t(1) << 24;

std::vector<uint32_t> unitSizes(Bytes stsz) {
  const auto uniform = integer(stsz, 4, 4);
  const auto count = integer(stsz, 8, 4);
  if (integer(stsz, 0, 4) != 0 || !count || count > maxUnits ||
      uniform > maxUnitBytes || stsz.size() != (uniform ? 12 : 12 + 4 * count))
    invalid();
  std::vector<uint32_t> sizes(count, uint32_t(uniform));
  if (!uniform)
    for (size_t i = 0; i < count; ++i) {
      sizes[i] = integer(stsz, 12 + 4 * i, 4);
      if (!sizes[i] || sizes[i] > maxUnitBytes)
        invalid();
    }
  return sizes;
}

std::vector<uint64_t> chunkOffsets(const std::vector<mp4::Box> &items) {
  const auto wide = std::ranges::any_of(
      items, [](const auto &item) { return item.type == "co64"; });
  const auto table = mp4::one(items, wide ? "co64" : "stco").payload;
  const size_t width = wide ? 8 : 4;
  const auto count = integer(table, 4, 4);
  if (integer(table, 0, 4) != 0 || !count || count > maxUnits ||
      table.size() != 8 + width * count)
    invalid();
  std::vector<uint64_t> offsets(count);
  for (size_t i = 0; i < count; ++i)
    offsets[i] = integer(table, 8 + width * i, width);
  return offsets;
}

/// Each chunk's unit count, from the runs `stsc` describes.
std::vector<uint64_t> unitsPerChunk(Bytes stsc, size_t chunks) {
  const auto runs = integer(stsc, 4, 4);
  if (integer(stsc, 0, 4) != 0 || !runs || stsc.size() != 8 + 12 * runs)
    invalid();
  std::vector<uint64_t> result(chunks);
  for (size_t run = 0; run < runs; ++run) {
    const auto first = integer(stsc, 8 + 12 * run, 4);
    const auto units = integer(stsc, 12 + 12 * run, 4);
    const auto last =
        run + 1 < runs ? integer(stsc, 8 + 12 * (run + 1), 4) : chunks + 1;
    if ((run == 0 && first != 1) || first >= last || last > chunks + 1 ||
        !units || units > maxUnits || integer(stsc, 16 + 12 * run, 4) != 1)
      invalid();
    for (auto chunk = first; chunk < last; ++chunk)
      result[chunk - 1] = units;
  }
  return result;
}

std::vector<mp4::Box> sampleTables(const mp4::Box &moov) {
  const auto stbl = mp4::inside(
      mp4::inside(mp4::inside(mp4::soundTrack(moov), "mdia"), "minf"), "stbl");
  return mp4::boxes(stbl.payload, stbl.offset + stbl.header);
}
} // namespace

bool Mp4SampleTable::listsSamples(Bytes bytes) {
  for (const auto &item : sampleTables(mp4::one(mp4::boxes(bytes), "moov")))
    if (item.type == "stsz")
      return integer(item.payload, 8, 4) > 0;
  return false;
}

Mp4SampleTable Mp4SampleTable::parse(Bytes bytes) {
  const auto moov = mp4::one(mp4::boxes(bytes), "moov");
  Mp4SampleTable table;
  table.track = mp4::audioTrack(moov);
  const auto items = sampleTables(moov);
  table.sizes = unitSizes(mp4::one(items, "stsz").payload);
  const auto count = table.sizes.size();

  const auto chunks = chunkOffsets(items);
  const auto perChunk =
      unitsPerChunk(mp4::one(items, "stsc").payload, chunks.size());
  table.offsets.reserve(count);
  for (size_t chunk = 0; chunk < chunks.size(); ++chunk) {
    auto offset = chunks[chunk];
    for (uint64_t i = 0; i < perChunk[chunk]; ++i) {
      if (table.offsets.size() == count)
        invalid();
      table.offsets.push_back(offset);
      offset += table.sizes[table.offsets.size() - 1];
    }
  }
  if (table.offsets.size() != count)
    invalid();

  const auto stts = mp4::one(items, "stts").payload;
  const auto runs = integer(stts, 4, 4);
  if (integer(stts, 0, 4) != 0 || stts.size() != 8 + 8 * runs)
    invalid();
  table.times.reserve(count);
  for (size_t run = 0; run < runs; ++run) {
    const auto units = integer(stts, 8 + 8 * run, 4);
    const auto delta = integer(stts, 12 + 8 * run, 4);
    if (!delta || units > count - table.times.size())
      invalid();
    for (uint64_t i = 0; i < units; ++i) {
      table.times.push_back(table.end);
      table.end += delta;
    }
  }
  if (table.times.size() != count)
    invalid();
  return table;
}
