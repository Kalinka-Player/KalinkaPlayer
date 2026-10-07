#include <gtest/gtest.h>

#include <memory>
#include <string>

#include "config/ConfigService.h"
#include "fakes.h"

namespace pb = kalinka::renderer::v1;

class ConfigServiceTest : public ::testing::Test {
protected:
  pb::ConfigSnapshot snapshot() {
    pb::ConfigSnapshot out;
    service.fillSnapshot(out);
    return out;
  }

  pb::ConfigResult apply(const std::string &path, const std::string &value) {
    pb::ConfigUpdate update;
    pb::ConfigUpdate::Setting *setting = update.add_settings();
    setting->set_path(path);
    setting->set_value(value);
    pb::ConfigResult out;
    service.apply(update, out);
    return out;
  }

  std::shared_ptr<FakePlayer> player = std::make_shared<FakePlayer>();
  ConfigService service{{player}};
};

/// A second, minimal contributor: one section, one writable string field.
class FakeContributor : public ConfigContributor {
public:
  std::string value = "kalinka";
  std::vector<std::string> calls;

  void fillConfig(pb::ConfigSection &out) const override {
    out.set_path("renderer");
    pb::ConfigField *name = out.add_fields();
    name->set_path("renderer.name");
    name->set_type(pb::CONFIG_FIELD_TYPE_STRING);
    name->set_value(value);
    name->set_apply(pb::APPLY_COST_INSTANT);
  }

  bool applyConfig(const std::string &path, const std::string &newValue,
                   std::string &) override {
    calls.push_back(path + "=" + newValue);
    value = newValue;
    return true;
  }
};

TEST_F(ConfigServiceTest, SectionsFollowContributorRegistrationOrder) {
  auto other = std::make_shared<FakeContributor>();
  ConfigService combined{{player, other}};

  pb::ConfigSnapshot snap;
  combined.fillSnapshot(snap);

  ASSERT_EQ(snap.sections_size(), 2);
  EXPECT_EQ(snap.sections(0).path(), "output");
  EXPECT_EQ(snap.sections(1).path(), "renderer");
}

TEST_F(ConfigServiceTest, WritesReachTheContributorThatDeclaredTheField) {
  auto other = std::make_shared<FakeContributor>();
  ConfigService combined{{player, other}};

  pb::ConfigUpdate update;
  pb::ConfigUpdate::Setting *name = update.add_settings();
  name->set_path("renderer.name");
  name->set_value("kitchen");
  pb::ConfigUpdate::Setting *buffer = update.add_settings();
  buffer->set_path("output.buffer_ms");
  buffer->set_value("250");
  pb::ConfigResult result;
  combined.apply(update, result);

  EXPECT_TRUE(result.outcomes(0).applied());
  EXPECT_TRUE(result.outcomes(1).applied());
  EXPECT_EQ(other->calls, (std::vector<std::string>{"renderer.name=kitchen"}));
  EXPECT_EQ(player->calls,
            (std::vector<std::string>{"apply_config:output.buffer_ms=250"}));
  EXPECT_EQ(other->value, "kitchen");
}

TEST_F(ConfigServiceTest, VersionCoversEveryContributor) {
  auto other = std::make_shared<FakeContributor>();
  ConfigService combined{{player, other}};

  pb::ConfigSnapshot withBoth;
  combined.fillSnapshot(withBoth);

  EXPECT_NE(withBoth.config_version(), snapshot().config_version());
}

TEST_F(ConfigServiceTest, SnapshotCarriesThePlayerSectionAndAVersion) {
  pb::ConfigSnapshot snap = snapshot();

  ASSERT_EQ(snap.sections_size(), 1);
  EXPECT_EQ(snap.sections(0).path(), "output");
  EXPECT_EQ(snap.sections(0).fields_size(), 3);
  EXPECT_FALSE(snap.config_version().empty());
}

TEST_F(ConfigServiceTest, VersionCoversShapeAndOptionsButNeverValues) {
  const std::string before = snapshot().config_version();

  player->values["output.buffer_ms"] = "250";
  EXPECT_EQ(snapshot().config_version(), before);

  player->driverOptions.push_back("pipewire");
  EXPECT_NE(snapshot().config_version(), before);
}

TEST_F(ConfigServiceTest, VersionCoversWhichPageAFieldIsOn) {
  const std::string before = snapshot().config_version();

  player->bufferImportance = pb::CONFIG_IMPORTANCE_SIMPLE;

  EXPECT_NE(snapshot().config_version(), before);
}

TEST_F(ConfigServiceTest, AValidWriteReachesThePlayer) {
  player->driverOptions = {"alsa", "pipewire"};

  pb::ConfigResult result = apply("output.driver", "pipewire");

  ASSERT_EQ(result.outcomes_size(), 1);
  EXPECT_TRUE(result.outcomes(0).applied());
  EXPECT_EQ(result.outcomes(0).value(), "pipewire");
  EXPECT_EQ(result.effect(), pb::APPLY_COST_RESTART_REQUIRED);
  EXPECT_EQ(player->values.at("output.driver"), "pipewire");
}

TEST_F(ConfigServiceTest, TheOutcomeCarriesWhatIsInEffectNotWhatWasAsked) {
  player->driverOptions = {"alsa", "pipewire"};
  player->normalizedSuffix = ":0";  // the player elaborates what it was given

  pb::ConfigResult result = apply("output.driver", "pipewire");

  EXPECT_TRUE(result.outcomes(0).applied());
  EXPECT_EQ(result.outcomes(0).value(), "pipewire:0");
}

TEST_F(ConfigServiceTest, UnknownPathIsRefusedWithoutTouchingThePlayer) {
  pb::ConfigResult result = apply("output.nonsense", "x");

  EXPECT_FALSE(result.outcomes(0).applied());
  EXPECT_EQ(result.outcomes(0).error(), "no such setting");
  EXPECT_TRUE(player->calls.empty());
}

TEST_F(ConfigServiceTest, ReadOnlyFieldIsRefusedWithoutTouchingThePlayer) {
  player->driverReadOnly = true;

  pb::ConfigResult result = apply("output.driver", "alsa");

  EXPECT_FALSE(result.outcomes(0).applied());
  EXPECT_EQ(result.outcomes(0).error(), "read-only setting");
  EXPECT_TRUE(player->calls.empty());
}

TEST_F(ConfigServiceTest, ValuesMustParseAsTheFieldType) {
  EXPECT_EQ(apply("output.buffer_ms", "fast").outcomes(0).error(),
            "not a number");
  EXPECT_EQ(apply("output.exclusive", "yes").outcomes(0).error(),
            "expected 'true' or 'false'");
  EXPECT_EQ(apply("output.driver", "wasapi").outcomes(0).error(),
            "not one of the offered options");
  player->driverOptions.clear();
  EXPECT_EQ(apply("output.driver", "alsa").outcomes(0).error(),
            "no options available");
  EXPECT_TRUE(player->calls.empty());
}

TEST_F(ConfigServiceTest, AValueOutsideTheDeclaredRangeIsRefused) {
  player->bufferRange = {20, 1000};

  EXPECT_EQ(apply("output.buffer_ms", "5000").outcomes(0).error(),
            "must be between 20 and 1000");
  EXPECT_EQ(apply("output.buffer_ms", "-1").outcomes(0).error(),
            "must be between 20 and 1000");
  EXPECT_TRUE(player->calls.empty());
}

TEST_F(ConfigServiceTest, TheEdgesOfADeclaredRangeAreAccepted) {
  player->bufferRange = {20, 1000};

  EXPECT_TRUE(apply("output.buffer_ms", "20").outcomes(0).applied());
  EXPECT_TRUE(apply("output.buffer_ms", "1000").outcomes(0).applied());
}

TEST_F(ConfigServiceTest, AFieldWithNoRangeTakesAnyNumber) {
  EXPECT_TRUE(apply("output.buffer_ms", "999999").outcomes(0).applied());
}

TEST_F(ConfigServiceTest, VersionCoversWhatAFieldAccepts) {
  const std::string before = snapshot().config_version();

  player->bufferRange = {20, 1000};

  EXPECT_NE(snapshot().config_version(), before);
}

TEST_F(ConfigServiceTest, PlayerRefusalIsReportedPerPath) {
  player->refuseApply = true;

  pb::ConfigResult result = apply("output.buffer_ms", "250");

  EXPECT_FALSE(result.outcomes(0).applied());
  EXPECT_EQ(result.outcomes(0).error(), "player said no");
  EXPECT_EQ(result.effect(), pb::APPLY_COST_UNSPECIFIED);  // nothing happened
}

TEST_F(ConfigServiceTest, AWriteThatAppliedSomethingIsAnnouncedOnce) {
  int announced = 0;
  ConfigService watched{{player}, [&announced] { ++announced; }};
  pb::ConfigUpdate update;
  pb::ConfigUpdate::Setting *unknown = update.add_settings();
  unknown->set_path("output.nonexistent");
  unknown->set_value("1");
  pb::ConfigResult refused;
  watched.apply(update, refused);
  EXPECT_EQ(announced, 0);

  pb::ConfigUpdate::Setting *buffer = update.add_settings();
  buffer->set_path("output.buffer_ms");
  buffer->set_value("200");
  pb::ConfigUpdate::Setting *exclusive = update.add_settings();
  exclusive->set_path("output.exclusive");
  exclusive->set_value("true");
  pb::ConfigResult applied;
  watched.apply(update, applied);

  EXPECT_EQ(announced, 1);
}

TEST_F(ConfigServiceTest, EffectIsTheWorstAmongTheSettingsActuallyApplied) {
  pb::ConfigUpdate update;
  pb::ConfigUpdate::Setting *buffer = update.add_settings();
  buffer->set_path("output.buffer_ms");
  buffer->set_value("250");
  pb::ConfigUpdate::Setting *exclusive = update.add_settings();
  exclusive->set_path("output.exclusive");
  exclusive->set_value("true");
  pb::ConfigUpdate::Setting *driver = update.add_settings();
  driver->set_path("output.driver");
  driver->set_value("wasapi");  // refused: its RESTART_REQUIRED must not count

  pb::ConfigResult result;
  service.apply(update, result);

  ASSERT_EQ(result.outcomes_size(), 3);
  EXPECT_TRUE(result.outcomes(0).applied());
  EXPECT_TRUE(result.outcomes(1).applied());
  EXPECT_FALSE(result.outcomes(2).applied());
  EXPECT_EQ(result.effect(), pb::APPLY_COST_INTERRUPTS_PLAYBACK);
}
