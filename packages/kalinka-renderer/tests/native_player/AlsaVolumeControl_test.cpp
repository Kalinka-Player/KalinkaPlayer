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
