#include "DashManifest.h"
#include "DashTestData.h"
#include "FragmentedFlac.h"

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

TEST(DashManifest, RejectsUnsupportedAndUnboundedDocuments) {
  for (const auto &xml : {
           replace(manifest, "static", "dynamic"),
           replace(manifest, "flac", "mp4a.40.2"),
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

TEST(FragmentedFlac, ExtractsConfigurationAndSampleTimings) {
  for (int bits : {16, 24}) {
    const auto fixture = dashFixtures(bits);
    const auto config = FragmentedFlac::parseInitialization(
        bytes(fixture.at("/init-stream0.m4s")));
    EXPECT_EQ(config.format.bitsPerSample, bits);
    EXPECT_EQ(config.format.sampleRate, bits == 16 ? 44100u : 96000u);
    uint64_t position = 0;
    for (const auto &[name, body] : fixture) {
      if (!name.starts_with("/chunk"))
        continue;
      for (const auto &fragment : config.parseSegments(bytes(body))) {
        EXPECT_EQ(fragment.time, position);
        EXPECT_FALSE(fragment.frames.empty());
        position += std::accumulate(fragment.durations.begin(),
                                    fragment.durations.end(), uint64_t(0));
      }
    }
    EXPECT_EQ(config.samplePosition(position),
              config.format.sampleRate * 5 / 4);
  }
}

TEST(FragmentedFlac, RejectsTruncatedInitialization) {
  const auto fixture = dashFixtures(16).at("/init-stream0.m4s");
  for (size_t size = 0; size < fixture.size(); ++size)
    EXPECT_THROW(
        FragmentedFlac::parseInitialization(bytes(fixture).first(size)),
        std::runtime_error)
        << size;
}

TEST(FragmentedFlac, RejectsBadOffsetsEncryptionAndTruncatedSegments) {
  const auto fixture = dashFixtures(16);
  const auto config = FragmentedFlac::parseInitialization(
      bytes(fixture.at("/init-stream0.m4s")));
  const auto original = fixture.at("/chunk-stream0-00001.m4s");
  for (size_t size : {size_t(0), size_t(7), size_t(100), original.size() - 1})
    EXPECT_THROW(config.parseSegments(bytes(original).first(size)),
                 std::runtime_error);
  auto badOffset = original;
  const auto trun = badOffset.find("trun");
  ASSERT_NE(trun, std::string::npos);
  for (size_t i = trun + 12; i < trun + 16; ++i)
    badOffset[i] = char(0xff);
  EXPECT_THROW(config.parseSegments(bytes(badOffset)), std::runtime_error);
  const auto encrypted = replace(original, "trun", "senc");
  EXPECT_THROW(config.parseSegments(bytes(encrypted)), std::runtime_error);
  auto hugeBox = original;
  for (size_t i = 0; i < 4; ++i)
    hugeBox[i] = char(0xff);
  EXPECT_THROW(config.parseSegments(bytes(hugeBox)), std::runtime_error);
}
