#include <gtest/gtest.h>

#include <stdexcept>

#include "config/DspProfile.h"
#include "kalinka/renderer/v1/renderer.pb.h"

namespace renderer_pb = kalinka::renderer::v1;
using WireBand = renderer_pb::DspProfile::Band;

namespace {

renderer_pb::DspProfile named() {
  renderer_pb::DspProfile profile;
  profile.set_version(1);
  profile.set_name("Room");
  return profile;
}

void add(renderer_pb::DspProfile &profile, WireBand::Type type, double hz,
         double db, double q) {
  auto &band = *profile.add_shared();
  band.set_type(type);
  band.set_frequency_hz(hz);
  band.set_gain_db(db);
  band.set_q(q);
}

peq::Profile parsed(const renderer_pb::DspProfile &profile) {
  return dsp_profiles::parse(profile.SerializeAsString());
}

} // namespace

TEST(DspProfile, EveryBandTypeArrivesWithItsParameters) {
  auto profile = named();
  add(profile, WireBand::PEAK, 1000, -6, 2);
  add(profile, WireBand::LOW_SHELF, 90, 2.5, 0.7);
  add(profile, WireBand::HIGH_SHELF, 8000, 3, 0.67);
  add(profile, WireBand::HIGH_PASS, 30, 0, 0.707);
  add(profile, WireBand::LOW_PASS, 18000, 0, 0.54);
  add(profile, WireBand::FIRST_ORDER_HIGH_PASS, 80, 0, 0);
  add(profile, WireBand::FIRST_ORDER_LOW_PASS, 12000, 0, 0);
  const auto bands = parsed(profile).shared;
  ASSERT_EQ(bands.size(), 7u);
  using enum peq::Type;
  const peq::Type types[]{
      Peak,    LowShelf,           HighShelf,        HighPass,
      LowPass, FirstOrderHighPass, FirstOrderLowPass};
  for (size_t i = 0; i < bands.size(); ++i) {
    EXPECT_EQ(bands[i].type, types[i]);
    EXPECT_EQ(bands[i].hz, profile.shared(int(i)).frequency_hz());
    EXPECT_EQ(bands[i].db, profile.shared(int(i)).gain_db());
    EXPECT_EQ(bands[i].q, profile.shared(int(i)).q());
  }
}

TEST(DspProfile, APassBandIsActiveAtZeroGainWhereAPeakIsDropped) {
  auto profile = named();
  add(profile, WireBand::PEAK, 1000, 0, 2);
  add(profile, WireBand::HIGH_PASS, 30, 0, 0.707);
  const auto bands = parsed(profile).shared;
  ASSERT_EQ(bands.size(), 1u);
  EXPECT_EQ(bands[0].type, peq::Type::HighPass);

  auto flat = named();
  add(flat, WireBand::PEAK, 1000, 0, 2);
  EXPECT_THROW(parsed(flat), std::runtime_error);
}

TEST(DspProfile, RefusesAPassBandWithGainAndAnUnknownType) {
  for (auto type : {WireBand::HIGH_PASS, WireBand::LOW_PASS}) {
    for (double db : {-3.0, 0.5}) {
      auto profile = named();
      add(profile, type, 30, db, 0.707);
      EXPECT_THROW(parsed(profile), std::runtime_error);
    }
  }
  for (int type : {0, 8}) {
    auto profile = named();
    add(profile, WireBand::Type(type), 30, 0, 0.707);
    EXPECT_THROW(parsed(profile), std::runtime_error);
  }
}

TEST(DspProfile, RefusesAFirstOrderBandWithAGainOrAQ) {
  for (auto type :
       {WireBand::FIRST_ORDER_HIGH_PASS, WireBand::FIRST_ORDER_LOW_PASS}) {
    for (const auto [db, q] : {std::pair{0.0, 0.707}, std::pair{3.0, 0.0}}) {
      auto profile = named();
      add(profile, type, 30, db, q);
      EXPECT_THROW(parsed(profile), std::runtime_error);
    }
  }
}

TEST(DspProfile, RefusesAPassBandOutsideTheFrequencyAndQLimits) {
  for (const auto [hz, q] : {std::pair{0.0, 0.707}, std::pair{400000.0, 0.707},
                             std::pair{30.0, 0.0}, std::pair{30.0, 1001.0}}) {
    auto profile = named();
    add(profile, WireBand::LOW_PASS, hz, 0, q);
    EXPECT_THROW(parsed(profile), std::runtime_error);
  }
}
