#include <gtest/gtest.h>

#include "PlaybackPcm.h"

#include <initializer_list>

namespace {

struct FormatMask {
  explicit FormatMask(std::initializer_list<snd_pcm_format_t> formats) {
    snd_pcm_format_mask_malloc(&mask);
    snd_pcm_format_mask_none(mask);
    for (snd_pcm_format_t format : formats)
      snd_pcm_format_mask_set(mask, format);
  }
  ~FormatMask() { snd_pcm_format_mask_free(mask); }
  FormatMask(const FormatMask &) = delete;
  FormatMask &operator=(const FormatMask &) = delete;

  snd_pcm_format_mask_t *mask = nullptr;
};

TEST(PlaybackPcm, ACardTakingOnlyIec958SubframesNeedsFraming) {
  EXPECT_TRUE(
      onlyIec958Subframes(FormatMask{SND_PCM_FORMAT_IEC958_SUBFRAME_LE}.mask));
}

TEST(PlaybackPcm, ACardTakingSamplesPlaysThemAsTheyAre) {
  EXPECT_FALSE(onlyIec958Subframes(FormatMask{SND_PCM_FORMAT_S16_LE,
                                              SND_PCM_FORMAT_S32_LE,
                                              SND_PCM_FORMAT_IEC958_SUBFRAME_LE}
                                       .mask))
      << "an HDA HDMI port takes both";
  EXPECT_FALSE(onlyIec958Subframes(FormatMask{SND_PCM_FORMAT_S24_3LE}.mask));
  EXPECT_FALSE(onlyIec958Subframes(FormatMask{}.mask));
}

TEST(PlaybackPcm, TheFramingStampsTheRateItPlaysAt) {
  EXPECT_EQ(iec958FramingOf(1, 0), "hdmi:CARD=1,DEV=0,AES3=0x01");
}

TEST(PlaybackPcm, APluginIsShared) {
  const auto opened = openPlayback("null", 0);
  ASSERT_TRUE(opened);
  EXPECT_EQ(opened->access, DeviceAccess::Shared);
  snd_pcm_close(opened->handle);
}

TEST(PlaybackPcm, AMissingDeviceGivesAlsasError) {
  const auto opened = openPlayback("kalinka-device-that-does-not-exist", 0);
  ASSERT_FALSE(opened);
  EXPECT_LT(opened.error(), 0);
}

} // namespace
