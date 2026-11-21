#pragma once
#include "native_player/dsp/peq/Profile.h"
#include <string_view>

namespace dsp_profiles {
inline constexpr size_t kMaxSize = 16 * 1024;
/// Decodes a versioned profile and checks the renderer's limits independently
/// of the Core.
peq::Profile parse(std::string_view bytes);
} // namespace dsp_profiles
