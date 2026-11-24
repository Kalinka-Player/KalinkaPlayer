#pragma once
#include "kalinka/renderer/v1/renderer.pb.h"
#include "native_player/dsp/peq/Profile.h"

#include <filesystem>
#include <string_view>

/// The renderer's validated, content-addressed PEQ and FIR presets. A cache
/// miss fetches on the settings worker. Reading a selected hash also works
/// offline.
namespace dsp_profiles {
std::filesystem::path directory();
bool validId(std::string_view id);
std::shared_ptr<const peq::Profile> load(const std::string &id);
kalinka::renderer::v1::FirProfile loadFir(const std::string &id);
void fetch(const std::string &id, const std::string &url, bool fir = false);
} // namespace dsp_profiles
