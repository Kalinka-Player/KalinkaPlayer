#include "DashManifest.h"
#include "DashTestData.h"
#include "FragmentedMp4.h"

#include <gtest/gtest.h>

#include <numeric>
#include <string_view>

namespace {
std::span<const uint8_t> bytes(const std::string &text) {
  return {reinterpret_cast<const uint8_t *>(text.data()), text.size()};
}
std::string replace(std::string text, const std::string &from,
                    const std::string &to) {
  const auto at = text.find(from);
  if (at == std::string::npos)
    throw std::runtime_error("Missing fixture text");
  text.replace(at, from.size(), to);
  return text;
}
const std::string manifest =
    R"(<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2.5S">
  <BaseURL>../media/</BaseURL><Period><AdaptationSet codecs="flac">
    <SegmentTemplate timescale="44100" startNumber="4" initialization="$RepresentationID$-init.mp4"
      media="$RepresentationID$-$Number%05d$-$Time$-$$.mp4">
      <SegmentTimeline><S t="0" d="44100" r="1"/><S d="22050"/></SegmentTimeline>
    </SegmentTemplate><Representation id="FLAC,44100,16"/>
  </AdaptationSet></Period></MPD>)";
} // namespace

TEST(DashManifest, ResolvesInheritedTemplatesAndRelativeUrls) {
  const auto parsed = DashManifest::parse(
      bytes(manifest), "https://example.test/path/manifest.mpd");
  EXPECT_EQ(parsed.initialization,
            "https://example.test/media/FLAC,44100,16-init.mp4");
  EXPECT_EQ(parsed.segmentUrl(1),
            "https://example.test/media/FLAC,44100,16-00005-44100-$.mp4");
  ASSERT_EQ(parsed.segments.size(), 3u);
  EXPECT_EQ(parsed.segments.back().time, 88200u);
  EXPECT_EQ(parsed.segments.back().duration, 22050u);
}

TEST(DashManifest, TreatsAMissingTypeAsStatic) {
  const auto parsed = DashManifest::parse(
      bytes(replace(manifest, " type=\"static\"", "")),
      "https://example.test/path/manifest.mpd");
  EXPECT_EQ(parsed.segments.size(), 3u);
}

TEST(DashManifest, UsesTheFirstOfAlternativeBaseUrls) {
  const auto parsed = DashManifest::parse(
      bytes(replace(manifest, "<BaseURL>../media/</BaseURL>",
                    "<BaseURL>../media/</BaseURL><BaseURL>../mirror/</BaseURL>")),
      "https://example.test/path/manifest.mpd");
  EXPECT_EQ(parsed.initialization,
            "https://example.test/media/FLAC,44100,16-init.mp4");
}

TEST(DashManifest, ExpandsNegativeRepeatsUpToNextTimestamp) {
  const auto xml = replace(replace(manifest, "r=\"1\"", "r=\"-1\""),
                           "<S d=\"22050\"/>", "<S t=\"88200\" d=\"22050\"/>");
  const auto parsed =
      DashManifest::parse(bytes(xml), "https://example.test/a.mpd");
  EXPECT_EQ(parsed.segments.size(), 3u);
}

TEST(DashManifest, OnlyAnAacTimelineMayOverrunItsDuration) {
  // The fixture's timeline lasts 2.5 s; 2.6 s is a tenth of a second more.
  const auto longer = replace(manifest, "PT2.5S", "PT2.6S");
  EXPECT_THROW(
      DashManifest::parse(bytes(longer), "https://example.test/a.mpd"),
      std::runtime_error);
  EXPECT_NO_THROW(DashManifest::parse(
      bytes(replace(longer, "flac", "mp4a.40.2")),
      "https://example.test/a.mpd"));
}

TEST(DashManifest, AcceptsAacRepresentations) {
  for (const auto *codec : {"mp4a.40.2", "mp4a.40.5", "mp4a.40.29"}) {
    SCOPED_TRACE(codec);
    const auto parsed = DashManifest::parse(
        bytes(replace(manifest, "flac", codec)), "https://example.test/a.mpd");
    EXPECT_EQ(parsed.segments.size(), 3u);
  }
}

TEST(DashManifest, RejectsUnsupportedAndUnboundedDocuments) {
  for (const auto &xml : {
           replace(manifest, "static", "dynamic"),
           replace(manifest, "flac", "ec-3"),
           replace(manifest, "flac", "mp4a.40.42"),
           replace(manifest, "<Period>", "<Period><ContentProtection/>"),
           replace(manifest, "<Period>", "<Period start=\"PT1S\">"),
           replace(manifest, "r=\"1\"", "r=\"1000000000\""),
           replace(manifest, "timescale=\"44100\"", "timescale=\"0\""),
           replace(manifest, "../media/", "file:///etc/"),
           replace(manifest, "$Number%05d$", "$Number%099999d$"),
           replace(manifest, "<S t=\"0\"", "<S t=\"5\""),
           std::string(
               "<!DOCTYPE MPD [<!ENTITY x SYSTEM 'file:///etc/passwd'>]>") +
               manifest,
           manifest + std::string(1024 * 1024, ' '),
       }) {
    EXPECT_THROW(DashManifest::parse(bytes(xml), "https://example.test/a.mpd"),
                 std::runtime_error);
  }
}

TEST(FragmentedMp4, ExtractsConfigurationAndSampleTimings) {
  for (int bits : {16, 24}) {
    const auto fixture = dashFixtures(bits);
    const auto init = FragmentedMp4::parseInitialization(
        bytes(fixture.at("/init-stream0.m4s")));
    EXPECT_EQ(init.track.codec, mp4::AudioTrack::Codec::FLAC);
    EXPECT_EQ(init.track.config.size(), 34u);
    const auto rate = bits == 16 ? 44100u : 96000u;
    uint64_t position = 0;
    for (const auto &[name, body] : fixture) {
      if (!name.starts_with("/chunk"))
        continue;
      for (const auto &fragment : init.parseSegments(bytes(body))) {
        EXPECT_EQ(fragment.time, position);
        EXPECT_EQ(fragment.sizes.size(), fragment.durations.size());
        EXPECT_EQ(std::accumulate(fragment.sizes.begin(), fragment.sizes.end(),
                                  size_t(0)),
                  fragment.data.size());
        position += std::accumulate(fragment.durations.begin(),
                                    fragment.durations.end(), uint64_t(0));
      }
    }
    EXPECT_EQ(samplesAt(position, init.track.timescale, rate), rate * 5 / 4);
  }
}

TEST(FragmentedMp4, RejectsTruncatedInitialization) {
  const auto fixture = dashFixtures(16).at("/init-stream0.m4s");
  for (size_t size = 0; size < fixture.size(); ++size)
    EXPECT_THROW(FragmentedMp4::parseInitialization(bytes(fixture).first(size)),
                 std::runtime_error)
        << size;
}

TEST(FragmentedMp4, RejectsBadOffsetsEncryptionAndTruncatedSegments) {
  const auto fixture = dashFixtures(16);
  const auto init = FragmentedMp4::parseInitialization(
      bytes(fixture.at("/init-stream0.m4s")));
  const auto original = fixture.at("/chunk-stream0-00001.m4s");
  for (size_t size : {size_t(0), size_t(7), size_t(100), original.size() - 1})
    EXPECT_THROW(init.parseSegments(bytes(original).first(size)),
                 std::runtime_error);
  auto badOffset = original;
  const auto trun = badOffset.find("trun");
  ASSERT_NE(trun, std::string::npos);
  for (size_t i = trun + 12; i < trun + 16; ++i)
    badOffset[i] = char(0xff);
  EXPECT_THROW(init.parseSegments(bytes(badOffset)), std::runtime_error);
  const auto encrypted = replace(original, "trun", "senc");
  EXPECT_THROW(init.parseSegments(bytes(encrypted)), std::runtime_error);
  auto hugeBox = original;
  for (size_t i = 0; i < 4; ++i)
    hugeBox[i] = char(0xff);
  EXPECT_THROW(init.parseSegments(bytes(hugeBox)), std::runtime_error);
}
