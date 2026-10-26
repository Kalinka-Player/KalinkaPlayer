#include <gtest/gtest.h>

#include "AlsaVolumeControl.h"

TEST(AlsaVolumeControlTest, CubicScaleMatchesTheSoftwareGain) {
  const std::vector<double> db = cubicVolumeDb();
  ASSERT_EQ(db.size(), 101u);
  EXPECT_DOUBLE_EQ(db[0], -200.0);
  EXPECT_NEAR(db[50], -18.0618, 0.0001);
  EXPECT_NEAR(db[54] - db[28], 17.11, 0.01);
  EXPECT_DOUBLE_EQ(db[100], 0.0);
}

TEST(AlsaVolumeControlTest, OnlyTheSoundServerPluginsCountAsCubic) {
  EXPECT_TRUE(isSoundServerMixer(SND_CTL_TYPE_EXT, "pipewire"));
  EXPECT_TRUE(isSoundServerMixer(SND_CTL_TYPE_EXT, "pulse"));
  EXPECT_FALSE(isSoundServerMixer(SND_CTL_TYPE_HW, "pipewire"))
      << "a card that happens to be named so";
  EXPECT_FALSE(isSoundServerMixer(SND_CTL_TYPE_EXT, "bluealsa"));
  EXPECT_FALSE(isSoundServerMixer(SND_CTL_TYPE_HW, "sndrpihifiberry"));
}

TEST(AlsaVolumeControlTest, OnlyTheCardsOwnDevicesSkipSoftwareVolume) {
  EXPECT_TRUE(reachesTheCardItself("hw:CARD=vc4hdmi0,DEV=0"));
  EXPECT_TRUE(reachesTheCardItself("plughw:CARD=vc4hdmi0,DEV=0"));
  EXPECT_TRUE(reachesTheCardItself("hw:0,0"));
  EXPECT_FALSE(reachesTheCardItself("default:CARD=vc4hdmi0"))
      << "the Pi's HDMI default: plays through softvol";
  EXPECT_FALSE(reachesTheCardItself("default"));
  EXPECT_FALSE(reachesTheCardItself("pipewire"));
}

TEST(AlsaVolumeControlTest, AnElementIsMatchedToTheControlsItsVolumeComesFrom) {
  const std::vector<std::string> softvol{"PCM Playback Volume"};
  EXPECT_TRUE(playbackVolumeAmong("PCM", softvol));
  EXPECT_TRUE(playbackVolumeAmong("Master", {"Master Volume"}));
  EXPECT_FALSE(playbackVolumeAmong("Digital", softvol));
  EXPECT_FALSE(playbackVolumeAmong("PC", softvol));
  EXPECT_FALSE(playbackVolumeAmong("PCM", {}));
}
