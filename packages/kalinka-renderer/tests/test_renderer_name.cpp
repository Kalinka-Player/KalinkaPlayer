#include <gtest/gtest.h>
#include <stdlib.h>

#include <filesystem>
#include <fstream>

#include "../src/config/RendererName.h"
#include "../src/config/SettingsPersistence.h"

namespace fs = std::filesystem;
namespace renderer_pb = kalinka::renderer::v1;

namespace {

// Every setting is stored under KALINKA_PREFIX, so a temp prefix keeps the
// test off the real machine's config.
class RendererNameTest : public ::testing::Test {
protected:
  void SetUp() override {
    prefix_ = fs::temp_directory_path() /
              ("kalinka-name-test-" + std::to_string(::getpid()));
    fs::create_directories(prefix_);
    setenv("KALINKA_PREFIX", prefix_.c_str(), 1);
  }
  void TearDown() override {
    unsetenv("KALINKA_PREFIX");
    std::error_code ec;
    fs::remove_all(prefix_, ec);
  }

  static std::string field(const RendererName &name, bool *readOnly = nullptr) {
    renderer_pb::ConfigSection section;
    name.fillConfig(section);
    EXPECT_EQ(section.fields_size(), 1);
    if (readOnly != nullptr) {
      *readOnly = section.fields(0).read_only();
    }
    return section.fields(0).value();
  }

  fs::path prefix_;
};

TEST_F(RendererNameTest, WithNothingSetItIsNamedAfterTheHost) {
  const RendererName name("");
  EXPECT_EQ(name.value(), defaultRendererName());
  EXPECT_EQ(field(name), defaultRendererName());
}

TEST_F(RendererNameTest, TheStoredSettingBeatsTheDefault) {
  saveSettingsOverrides({{"renderer.name", "Kitchen"}});
  const RendererName name("");
  EXPECT_EQ(name.value(), "Kitchen");
}

TEST_F(RendererNameTest, TheCommandLineBeatsTheStoredSetting) {
  saveSettingsOverrides({{"renderer.name", "Kitchen"}});
  bool readOnly = false;
  const RendererName name("Study");
  EXPECT_EQ(name.value(), "Study");
  EXPECT_EQ(field(name, &readOnly), "Study");
  EXPECT_TRUE(readOnly) << "a name that --name overrides must not look "
                           "editable";
}

TEST_F(RendererNameTest, WritingTheSettingPersistsIt) {
  std::string error;
  RendererName name("");
  ASSERT_TRUE(name.applyConfig("renderer.name", " Living room ", error))
      << error;
  EXPECT_EQ(name.value(), "Living room");
  EXPECT_EQ(loadSettingsOverrides().at("renderer.name"), "Living room");
}

TEST_F(RendererNameTest, ClearingItGoesBackToTheDefault) {
  std::string error;
  RendererName name("");
  ASSERT_TRUE(name.applyConfig("renderer.name", "Kitchen", error)) << error;
  ASSERT_TRUE(name.applyConfig("renderer.name", "", error)) << error;
  EXPECT_EQ(name.value(), defaultRendererName());
  EXPECT_EQ(loadSettingsOverrides().count("renderer.name"), 0u);
}

// The player's own settings live in the same file.
TEST_F(RendererNameTest, WritingTheNameLeavesOtherSettingsAlone) {
  saveSettingsOverrides({{"output.device", "hw:CARD=Digi,DEV=0"}});
  std::string error;
  RendererName name("");
  ASSERT_TRUE(name.applyConfig("renderer.name", "Kitchen", error)) << error;
  EXPECT_EQ(loadSettingsOverrides().at("output.device"), "hw:CARD=Digi,DEV=0");
}

}  // namespace
