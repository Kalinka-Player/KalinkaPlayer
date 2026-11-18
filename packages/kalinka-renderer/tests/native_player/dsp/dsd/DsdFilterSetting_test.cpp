#include "dsp/dsd/DsdFilterSetting.h"

#include <gtest/gtest.h>

#include <stdexcept>

TEST(DsdFilterSetting, TheSettingNamesAFilterOrAutomatic) {
  EXPECT_EQ(dsdFilterSetting("auto"), std::nullopt);
  EXPECT_EQ(dsdFilterSetting("30k"), DsdFilter::Cut30k);
  EXPECT_EQ(dsdFilterSetting("50k"), DsdFilter::Cut50k);
  EXPECT_EQ(dsdFilterSetting("wide"), DsdFilter::Wide);
  for (const char *value : {"", "40k", "30", "Wide", "AUTO"})
    EXPECT_THROW(dsdFilterSetting(value), std::invalid_argument) << value;
}
