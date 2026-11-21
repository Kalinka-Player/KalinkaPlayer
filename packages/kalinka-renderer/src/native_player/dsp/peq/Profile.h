#pragma once

#include <cstddef>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace peq {
inline constexpr size_t kBands = 20;
enum class Type { Peak, LowShelf, HighShelf };

struct Band {
  Type type;
  double hz, db, q;
};

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
