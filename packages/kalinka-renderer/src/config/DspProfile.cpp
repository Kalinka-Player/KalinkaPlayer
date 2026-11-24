#include "DspProfile.h"
#include "kalinka/renderer/v1/renderer.pb.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace renderer_pb = kalinka::renderer::v1;
namespace dsp_profiles {
namespace {
auto bands(const auto &wire) {
  if (wire.size() > int(peq::kBands))
    throw std::runtime_error("Too many EQ bands");
  std::vector<peq::Band> result;
  for (const auto &b : wire) {
    peq::Type type;
    switch (b.type()) {
    case renderer_pb::DspProfile::Band::PEAK:
      type = peq::Type::Peak;
      break;
    case renderer_pb::DspProfile::Band::LOW_SHELF:
      type = peq::Type::LowShelf;
      break;
    case renderer_pb::DspProfile::Band::HIGH_SHELF:
      type = peq::Type::HighShelf;
      break;
    case renderer_pb::DspProfile::Band::HIGH_PASS:
      type = peq::Type::HighPass;
      break;
    case renderer_pb::DspProfile::Band::LOW_PASS:
      type = peq::Type::LowPass;
      break;
    case renderer_pb::DspProfile::Band::FIRST_ORDER_HIGH_PASS:
      type = peq::Type::FirstOrderHighPass;
      break;
    case renderer_pb::DspProfile::Band::FIRST_ORDER_LOW_PASS:
      type = peq::Type::FirstOrderLowPass;
      break;
    default:
      throw std::runtime_error("Unknown EQ band type");
    }
    const double gainLimit = peq::isPass(type) ? 0 : 30;
    const bool validQ =
        peq::isFirstOrder(type) ? b.q() == 0 : b.q() >= 0.01 && b.q() <= 1000;
    if (!(b.frequency_hz() > 0 && b.frequency_hz() <= 384000 && validQ &&
          std::abs(b.gain_db()) <= gainLimit))
      throw std::runtime_error("Invalid EQ frequency, gain or Q");
    const peq::Band band{type, b.frequency_hz(), b.gain_db(), b.q()};
    if (!peq::isFlat(band))
      result.push_back(band);
  }
  return result;
}
} // namespace

peq::Profile parse(std::string_view bytes) {
  renderer_pb::DspProfile wire;
  if (bytes.size() > kMaxSize ||
      !wire.ParseFromArray(bytes.data(), int(bytes.size())) ||
      wire.version() != 1 || wire.name().empty() || wire.name().size() > 120 ||
      std::ranges::any_of(wire.name(),
                          [](unsigned char c) { return c < 32 || c == 127; }) ||
      (wire.shared_size() && (wire.left_size() || wire.right_size())))
    throw std::runtime_error("Invalid DSP profile");
  peq::Profile result{wire.name(), bands(wire.shared()), bands(wire.left()),
                      bands(wire.right())};
  if (result.shared.empty() && result.left.empty() && result.right.empty())
    throw std::runtime_error("No active EQ bands");
  return result;
}

} // namespace dsp_profiles
