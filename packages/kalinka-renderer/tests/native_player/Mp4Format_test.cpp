#include "AacTestData.h"
#include "DashTestData.h"
#include "FragmentedMp4.h"
#include "Mp4SampleTable.h"

#include <gtest/gtest.h>

#include <numeric>
#include <string_view>

namespace {
using Bytes = std::vector<uint8_t>;

Bytes number(uint64_t value, int width) {
  Bytes bytes;
  for (int shift = 8 * (width - 1); shift >= 0; shift -= 8)
    bytes.push_back(uint8_t(value >> shift));
  return bytes;
}

Bytes join(std::initializer_list<Bytes> parts) {
  Bytes result;
  for (const auto &part : parts)
    result.insert(result.end(), part.begin(), part.end());
  return result;
}

Bytes box(std::string_view type, const Bytes &payload) {
  return join({number(payload.size() + 8, 4), Bytes(type.begin(), type.end()),
               payload});
}

Bytes zeros(size_t count) { return Bytes(count, 0); }

/// An AAC-LC 44.1 kHz stereo sample entry, its esds carrying `flags`' extras.
Bytes aacEntry(uint8_t flags = 0) {
  auto es = join({number(1, 2), number(flags, 1)});
  if (flags & 0x80)
    es = join({es, number(7, 2)});
  if (flags & 0x40)
    es = join({es, number(3, 1), {'u', 'r', 'l'}});
  const auto config = join({{0x05, 0x02, 0x12, 0x10}});
  const auto decoder = join(
      {{0x04, uint8_t(13 + config.size()), 0x40, 0x15}, zeros(11), config});
  es = join({es, decoder});
  const auto esds =
      box("esds", join({zeros(4), {0x03, uint8_t(es.size())}, es}));
  return box("mp4a",
             join({zeros(6), number(1, 2), zeros(8), number(2, 2),
                   number(16, 2), zeros(4), number(44100 << 16, 4), esds}));
}

/// What a test movie has beyond its sample entry and tables.
struct Extras {
  /// The data reference entry naming the file the data lies in.
  std::string_view reference = "url ";
  uint32_t referenceFlags = 1;
  /// Boxes added to the track and to the movie.
  Bytes trak, moov;
};

/// A movie whose one sound track has `entry` and the sample tables `tables`.
Bytes movie(const Bytes &entry, const Bytes &tables,
            const Extras &extras = {}) {
  const auto stsd = box("stsd", join({zeros(4), number(1, 4), entry}));
  const auto dref = box(
      "dref", join({zeros(4), number(1, 4),
                    box(extras.reference, number(extras.referenceFlags, 4))}));
  const auto minf =
      box("minf", join({box("dinf", dref), box("stbl", join({stsd, tables}))}));
  const auto hdlr =
      box("hdlr", join({zeros(8), {'s', 'o', 'u', 'n'}, zeros(13)}));
  const auto mdhd =
      box("mdhd",
          join({zeros(12), number(44100, 4), number(5 * 1024, 4), zeros(4)}));
  const auto tkhd = box("tkhd", join({zeros(12), number(1, 4), zeros(64)}));
  const auto trak = box(
      "trak", join({tkhd, extras.trak, box("mdia", join({mdhd, hdlr, minf}))}));
  return box("moov", join({extras.moov, trak}));
}

/// A movie header counting in milliseconds.
Bytes mvhd() {
  return box("mvhd", join({zeros(12), number(1000, 4), zeros(84)}));
}

/// One edit-list entry of version 0: a length in movie milliseconds, a
/// media time in track units (-1 for an empty edit) and a rate.
Bytes edit(uint32_t length, int32_t time, uint32_t rate = 0x00010000) {
  return join({number(length, 4), number(uint32_t(time), 4), number(rate, 4)});
}

Bytes editList(std::initializer_list<Bytes> entries) {
  Bytes list = join({zeros(4), number(entries.size(), 4)});
  for (const auto &entry : entries)
    list = join({list, entry});
  return box("edts", box("elst", list));
}

/// iTunes' gapless tag, its hex fields as `text`.
Bytes gaplessTag(const std::string &text) {
  const auto item = box(
      "----", join({box("mean", join({zeros(4), Bytes{'c', 'o', 'm'}})),
                    box("name", join({zeros(4), Bytes{'i', 'T', 'u', 'n', 'S',
                                                      'M', 'P', 'B'}})),
                    box("data", join({number(1, 4), zeros(4),
                                      Bytes(text.begin(), text.end())}))}));
  return box("udta", box("meta", join({zeros(4), box("ilst", item)})));
}

/// Five units in three chunks of 2, 2 and 1, at 64-bit offsets.
Bytes chunkedTables() {
  const auto stts = box(
      "stts", join({zeros(4), number(1, 4), number(5, 4), number(1024, 4)}));
  const auto stsc = box(
      "stsc", join({zeros(4), number(2, 4), number(1, 4), number(2, 4),
                    number(1, 4), number(3, 4), number(1, 4), number(1, 4)}));
  const auto stsz =
      box("stsz", join({zeros(8), number(5, 4), number(10, 4), number(11, 4),
                        number(12, 4), number(13, 4), number(14, 4)}));
  const auto co64 =
      box("co64", join({zeros(4), number(3, 4), number(1ull << 33, 8),
                        number(5000, 8), number(9000, 8)}));
  return join({stts, stsc, stsz, co64});
}
} // namespace

TEST(Mp4SampleTable, ExpandsChunksIntoUnitOffsets) {
  const auto table = Mp4SampleTable::parse(movie(aacEntry(), chunkedTables()));
  EXPECT_EQ(table.track.codec, mp4::AudioTrack::Codec::AAC);
  EXPECT_EQ(table.track.config, (Bytes{0x12, 0x10}));
  EXPECT_EQ(table.track.timescale, 44100u);
  EXPECT_EQ(table.offsets, (std::vector<uint64_t>{1ull << 33, (1ull << 33) + 10,
                                                  5000, 5012, 9000}));
  EXPECT_EQ(table.sizes, (std::vector<uint32_t>{10, 11, 12, 13, 14}));
  EXPECT_EQ(table.times, (std::vector<uint64_t>{0, 1024, 2048, 3072, 4096}));
  EXPECT_EQ(table.end, 5u * 1024);
}

TEST(Mp4SampleTable, ReadsAudioInTheMovieFileWhicheverWayItIsNamed) {
  for (const auto *reference : {"url ", "alis"})
    EXPECT_NO_THROW(
        Mp4SampleTable::parse(movie(aacEntry(), chunkedTables(), {reference})))
        << reference;
  for (const auto &bad : {movie(aacEntry(), chunkedTables(), {"url ", 0}),
                          movie(aacEntry(), chunkedTables(), {"alis", 0}),
                          movie(aacEntry(), chunkedTables(), {"urn "})})
    EXPECT_THROW(Mp4SampleTable::parse(bad), std::runtime_error);
}

TEST(Mp4SampleTable, ReadsEsdsWithOptionalFields) {
  const auto table =
      Mp4SampleTable::parse(movie(aacEntry(0xc0), chunkedTables()));
  EXPECT_EQ(table.track.config, (Bytes{0x12, 0x10}));
}

TEST(Mp4SampleTable, ReadsTheFixturesWhereverTheirIndexLies) {
  for (const auto *name : {"tones.m4a", "tones-index-last.m4a"}) {
    SCOPED_TRACE(name);
    const auto file = aacFixture(name);
    const auto table = Mp4SampleTable::parse(file);
    ASSERT_EQ(table.sizes.size(), 55u);
    EXPECT_EQ(table.end, 55u * 1024 - 171);
    for (size_t i = 0; i < table.sizes.size(); ++i)
      EXPECT_LE(table.offsets[i] + table.sizes[i], file.size());
  }
}

TEST(Mp4SampleTable, RefusesInconsistentTables) {
  const auto tables = chunkedTables();
  auto withCount = [&](size_t at, uint8_t value) {
    auto changed = tables;
    changed[at] = value;
    return movie(aacEntry(), changed);
  };
  const size_t stsz =
      std::search(tables.begin(), tables.end(), "stsz", std::next("stsz", 4)) -
      tables.begin() - 4;
  for (const auto &bad : {
           withCount(19, 6),       // stts lists six units for five
           withCount(55, 0),       // stsc's second run starts at chunk 0
           withCount(stsz + 19, 6) // stsz counts six units
       })
    EXPECT_THROW(Mp4SampleTable::parse(bad), std::runtime_error);
  const auto full = movie(aacEntry(), tables);
  for (size_t size = 0; size < full.size(); ++size)
    EXPECT_THROW(Mp4SampleTable::parse(std::span(full).first(size)),
                 std::runtime_error)
        << size;
}

TEST(Mp4AudioTrack, RefusesOtherCodecsEncryptionAndSilentMovies) {
  const auto alac = box("alac", join({zeros(6), number(1, 2), zeros(20)}));
  auto encrypted = aacEntry();
  std::copy_n("enca", 4, encrypted.begin() + 4);
  auto video = movie(aacEntry(), chunkedTables());
  const auto soun =
      std::search(video.begin(), video.end(), "soun", std::next("soun", 4));
  std::copy_n("vide", 4, soun);
  for (const auto &bad :
       {movie(alac, chunkedTables()), movie(encrypted, chunkedTables()), video})
    EXPECT_THROW(Mp4SampleTable::parse(bad), std::runtime_error);
  EXPECT_THROW(Mp4SampleTable::parse(aacFixture("alac.m4a")),
               std::runtime_error);
}

TEST(FragmentedMp4, ReadsAacInitializationAndSegments) {
  const auto fixture = dashFixtures("aac");
  const auto &init = fixture.at("/init-stream0.m4s");
  const auto parsed = FragmentedMp4::parseInitialization(
      std::span(reinterpret_cast<const uint8_t *>(init.data()), init.size()));
  EXPECT_EQ(parsed.track.codec, mp4::AudioTrack::Codec::AAC);
  // AAC-LC at 44.1 kHz in stereo, then an explicit "no SBR" extension.
  EXPECT_EQ(parsed.track.config, (Bytes{0x12, 0x10, 0x56, 0xe5, 0x00}));
  uint64_t time = 0;
  for (int number = 1; number <= 5; ++number) {
    const auto &chunk =
        fixture.at("/chunk-stream0-0000" + std::to_string(number) + ".m4s");
    const std::span bytes(reinterpret_cast<const uint8_t *>(chunk.data()),
                          chunk.size());
    for (const auto &fragment : parsed.parseSegments(bytes)) {
      EXPECT_EQ(fragment.time, time);
      time += std::accumulate(fragment.durations.begin(),
                              fragment.durations.end(), uint64_t(0));
    }
  }
  EXPECT_EQ(time, 55u * 1024 - 171);
}

TEST(FragmentedMp4, ListsFragmentsFromMfra) {
  for (const auto *name :
       {"tones-fragmented.m4a", "tones-fragmented-sidx.m4a"}) {
    SCOPED_TRACE(name);
    const auto file = aacFixture(name);
    const auto top = mp4::boxes(file);
    const auto moov = *std::ranges::find_if(
        top, [](const auto &item) { return item.type == "moov"; });
    const auto mfra = top.back();
    ASSERT_EQ(mfra.type, "mfra");
    const auto parsed =
        FragmentedMp4::parseInitialization(std::span(file).subspan(
            moov.offset, moov.header + moov.payload.size()));
    const auto listed = parsed.parseRandomAccess(mfra.payload);
    ASSERT_EQ(listed.size(), 5u);
    for (size_t i = 0; i < listed.size(); ++i) {
      EXPECT_EQ(listed[i].time, i * 11264);
      const auto moof =
          mp4::boxes(std::span(file).subspan(listed[i].offset)).front();
      EXPECT_EQ(moof.type, "moof");
    }
  }
}

TEST(FragmentedMp4, ListsFragmentsFromSidxAndReadsTheirTimes) {
  const auto file = aacFixture("tones-fragmented-sidx.m4a");
  const auto top = mp4::boxes(file);
  const auto find = [&](std::string_view type) {
    return *std::ranges::find_if(
        top, [&](const auto &item) { return item.type == type; });
  };
  const auto moov = find("moov");
  const auto parsed = FragmentedMp4::parseInitialization(
      std::span(file).subspan(moov.offset, moov.header + moov.payload.size()));
  const auto sidx = find("sidx");
  const auto listed = parsed.parseIndex(
      sidx.payload, sidx.offset + sidx.header + sidx.payload.size());
  ASSERT_EQ(listed.size(), 5u);
  uint64_t time = 0;
  for (const auto &reference : listed) {
    const auto moof =
        mp4::boxes(std::span(file).subspan(reference.offset)).front();
    ASSERT_EQ(moof.type, "moof");
    const auto timing = parsed.timing(moof.payload);
    EXPECT_EQ(timing.time, reference.time);
    EXPECT_EQ(timing.duration, reference.duration);
    EXPECT_EQ(reference.time, time);
    time += reference.duration;
  }
  EXPECT_EQ(time, 55u * 1024 - 171);
}

TEST(Mp4AudioTrack, ReadsTheEditAfterAnyEmptyOne) {
  const Extras extras{.trak = editList({edit(500, -1), edit(1000, 2112)}),
                      .moov = mvhd()};
  const auto track =
      Mp4SampleTable::parse(movie(aacEntry(), chunkedTables(), extras)).track;
  EXPECT_EQ(track.editStart, 2112u);
  EXPECT_EQ(track.editLength, 44100u);
}

TEST(Mp4AudioTrack, IgnoresEditsItCannotFollow) {
  for (const auto &edits : {editList({edit(1000, 2112, 0x00020000)}),
                            editList({edit(500, 0), edit(500, 2112)}),
                            editList({edit(1000, -5)})}) {
    const Extras extras{.trak = edits, .moov = mvhd()};
    const auto track =
        Mp4SampleTable::parse(movie(aacEntry(), chunkedTables(), extras)).track;
    EXPECT_EQ(track.editStart, 0u);
    EXPECT_EQ(track.editLength, 0u);
  }
}

TEST(Mp4AudioTrack, FallsBackToTheITunesGaplessTag) {
  const auto tag = gaplessTag(" 00000000 00000840 000001CA 0000000000046E00");
  const auto tagged =
      Mp4SampleTable::parse(movie(aacEntry(), chunkedTables(), {.moov = tag}))
          .track;
  EXPECT_EQ(tagged.editStart, 2112u);
  EXPECT_EQ(tagged.editLength, 290304u);
  // An edit list, even one this player cannot follow, outranks the tag.
  const Extras both{.trak = editList({edit(1000, 0, 0x00020000)}),
                    .moov = join({mvhd(), tag})};
  const auto listed =
      Mp4SampleTable::parse(movie(aacEntry(), chunkedTables(), both)).track;
  EXPECT_EQ(listed.editStart, 0u);
  EXPECT_EQ(listed.editLength, 0u);
}

TEST(Mp4AudioTrack, ReadsTheEsdsQuickTimeNestsInAWaveBox) {
  const auto entry = aacEntry();
  // aacEntry's 28 bytes of fields follow its box header; its esds follows.
  Bytes fields(entry.begin() + 8, entry.begin() + 36);
  fields[9] = 1;
  const Bytes esds(entry.begin() + 36, entry.end());
  const auto wave =
      box("wave", join({box("frma", {'m', 'p', '4', 'a'}), esds}));
  const auto quickTime = box("mp4a", join({fields, zeros(16), wave}));
  const auto track =
      Mp4SampleTable::parse(movie(quickTime, chunkedTables())).track;
  EXPECT_EQ(track.codec, mp4::AudioTrack::Codec::AAC);
  EXPECT_EQ(track.config, (Bytes{0x12, 0x10}));
}

TEST(Mp4AudioTrack, RefusesAVersionOneEntryTooShortForItsFields) {
  const auto entry =
      box("mp4a", join({zeros(6), number(1, 2), number(1, 2), zeros(26)}));
  EXPECT_THROW(Mp4SampleTable::parse(movie(entry, chunkedTables())),
               std::runtime_error);
}

TEST(Mp4AudioTrack, IgnoresAGaplessTagTooShortToRead) {
  const auto tag = [](const Bytes &name, const Bytes &data) {
    const auto item = box("----", join({box("name", name), box("data", data)}));
    return box("udta", box("meta", join({zeros(4), box("ilst", item)})));
  };
  const Bytes named =
      join({zeros(4), {'i', 'T', 'u', 'n', 'S', 'M', 'P', 'B'}});
  for (const auto &bad : {tag(zeros(2), zeros(8)), tag(named, zeros(4)),
                          box("udta", box("meta", zeros(2)))}) {
    const auto track =
        Mp4SampleTable::parse(movie(aacEntry(), chunkedTables(), {.moov = bad}))
            .track;
    EXPECT_EQ(track.editStart, 0u);
    EXPECT_EQ(track.editLength, 0u);
  }
}

TEST(FragmentedMp4, CountsAnIndexInFramesOnlyWhereItLandsOnThem) {
  FragmentedMp4 init;
  init.track.timescale = 1000;
  const std::vector<FragmentedMp4::Reference> onFrames{{10, 0, 500},
                                                       {20, 500, 250}};
  const auto framed = init.inFrames(onFrames, 48000);
  ASSERT_EQ(framed.size(), 2u);
  EXPECT_EQ(framed[1].offset, 20u);
  EXPECT_EQ(framed[1].time, 24000u);
  EXPECT_EQ(framed[1].duration, 12000u);
  // A millisecond is 44.1 frames at 44.1 kHz: no frame starts there.
  EXPECT_TRUE(init.inFrames({{10, 0, 1}, {20, 1, 1}}, 44100).empty());
}

TEST(FragmentedMp4, PlacesDataByFileOffsetWhereTheFragmentSaysSo) {
  const auto file = aacFixture("tones-fragmented-mixed.m4a");
  const auto top = mp4::boxes(file);
  const auto moov = *std::ranges::find_if(
      top, [](const auto &item) { return item.type == "moov"; });
  const auto init = FragmentedMp4::parseInitialization(
      std::span(file).subspan(moov.offset, moov.header + moov.payload.size()));
  const auto moof = std::ranges::find_if(
      top, [](const auto &item) { return item.type == "moof"; });
  const auto &mdat = *std::next(moof);
  const auto fragment = std::span(file).subspan(
      moof->offset,
      mdat.offset + mdat.header + mdat.payload.size() - moof->offset);
  const auto parsed = init.parseSegments(fragment, moof->offset);
  ASSERT_EQ(parsed.size(), 1u);
  EXPECT_EQ(parsed.front().time, 11264u);
  // Its tfhd counts from a file offset, so the bytes must say where they lie.
  EXPECT_THROW(init.parseSegments(fragment), std::runtime_error);
}
