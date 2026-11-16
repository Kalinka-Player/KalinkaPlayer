#pragma once

#include "ScopedEnvironmentVariable.h"

#include <alsa/asoundlib.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>

/// A test-owned ALSA configuration; restores the environment and cached config.
class ScopedAlsaConfig {
public:
  explicit ScopedAlsaConfig(std::string_view configuration) {
    char pattern[] = "/tmp/kalinka-alsa-config-XXXXXX";
    const char *directory = mkdtemp(pattern);
    if (!directory)
      throw std::runtime_error("Cannot create ALSA test configuration directory");
    directory_ = directory;
    const auto file = directory_ / "alsa.conf";
    std::ofstream(file) << configuration;
    path_.emplace("ALSA_CONFIG_PATH", file.string());
    snd_config_update_free_global();
  }

  ~ScopedAlsaConfig() {
    path_.reset();
    snd_config_update_free_global();
    std::error_code error;
    std::filesystem::remove_all(directory_, error);
  }

  ScopedAlsaConfig(const ScopedAlsaConfig &) = delete;
  ScopedAlsaConfig &operator=(const ScopedAlsaConfig &) = delete;

private:
  std::filesystem::path directory_;
  std::optional<ScopedEnvironmentVariable> path_;
};

inline constexpr std::string_view kHdmiTestConfig = R"(
  pcm.null { type null }
  pcm.hdmi {
    @args [CARD DEV AES3]
    @args.CARD { type string }
    @args.DEV { type integer }
    @args.AES3 { type integer default 2 }
    type null
  }
)";
