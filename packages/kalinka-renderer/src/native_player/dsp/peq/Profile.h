#pragma once

#include <cstddef>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace peq {
inline constexpr size_t kBands = 20;
enum class Type {
  Peak,
  LowShelf,
  HighShelf,
  HighPass,
  LowPass,
  FirstOrderHighPass,
  FirstOrderLowPass
};

struct Band {
  Type type;
  double hz, db, q;
};

/// A first-order band falls 6 dB per octave from `hz`: its `q` is unused.
constexpr bool isFirstOrder(Type type) {
  return type == Type::FirstOrderHighPass || type == Type::FirstOrderLowPass;
}

/// A pass band is shaped by `hz`, and by `q` when second order: its `db` is
/// unused.
constexpr bool isPass(Type type) {
  return type == Type::HighPass || type == Type::LowPass || isFirstOrder(type);
}

/// A peak or shelf at 0 dB leaves the signal as it is and costs nothing.
constexpr bool isFlat(const Band &band) {
  return !isPass(band.type) && band.db == 0;
}

/// A shared curve applies to every channel. A stereo pair corrects the front
/// pair.
struct Profile {
  std::string name;
  std::vector<Band> shared, left, right;
};

struct Settings {
  std::shared_ptr<const Profile> profile;
  /// Empty selects automatic headroom.
  std::optional<double> preampDb;
};

} // namespace peq
