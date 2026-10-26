#include "PlaybackPcm.h"

#include <algorithm>
#include <memory>
#include <string_view>

namespace {
using Config = std::unique_ptr<snd_config_t, decltype(&snd_config_delete)>;

PlaybackPcm inspect(snd_config_t *root, snd_config_t *node, unsigned depth) {
  PlaybackPcm route{nullptr, DeviceAccess::Shared};
  if (depth == 16)
    return route;
  const char *name = nullptr;
  if (snd_config_get_string(node, &name) == 0) {
    snd_config_t *definition = nullptr;
    if (snd_config_search_definition(root, "pcm", name, &definition) < 0)
      return route;
    Config owned(definition, snd_config_delete);
    return inspect(root, definition, depth + 1);
  }
  snd_config_t *type = nullptr;
  const char *value = nullptr;
  if (snd_config_search(node, "type", &type) < 0 ||
      snd_config_get_string(type, &value) < 0)
    return route;
  const std::string_view kind(value);
  if (kind == "hw") {
    route.access = DeviceAccess::Exclusive;
    return route;
  }
  if (kind != "iec958" && kind != "hooks" && kind != "empty")
    return route;
  route.iec958 = kind == "iec958";
  if (route.iec958) {
    snd_config_t *format = nullptr;
    if (snd_config_search(node, "slave.format", &format) < 0 ||
        snd_config_get_string(format, &value) < 0 ||
        (std::string_view(value) != "IEC958_SUBFRAME_LE" &&
         std::string_view(value) != "IEC958_SUBFRAME_BE"))
      return route;
  }
  snd_config_t *slave = nullptr;
  if (snd_config_search(node, "slave.pcm", &slave) < 0)
    return route;
  const auto downstream = inspect(root, slave, depth + 1);
  route.access = downstream.access;
  route.iec958 = route.iec958 || downstream.iec958;
  return route;
}
} // namespace

PlaybackPcm inspectHdmiRoute(snd_config_t *config, const std::string &device) {
  PlaybackPcm route{nullptr, DeviceAccess::Shared};
  if (!device.starts_with("hdmi:"))
    return route;
  snd_config_t *definition = nullptr;
  if (snd_config_search_definition(config, "pcm", device.c_str(), &definition) <
      0)
    return route;
  Config owned(definition, snd_config_delete);
  return inspect(config, definition, 0);
}

int PlaybackPcm::significantBits(const snd_pcm_hw_params_t *params) const {
  const int bits = snd_pcm_hw_params_get_sbits(params);
  return iec958 ? std::min(bits, 24) : bits;
}

PlaybackPcm inspectHdmiRoute(const std::string &device) {
  if (!device.starts_with("hdmi:"))
    return {nullptr, DeviceAccess::Shared};
  snd_config_t *config = nullptr;
  if (snd_config_update_ref(&config) < 0)
    return {nullptr, DeviceAccess::Shared};
  std::unique_ptr<snd_config_t, decltype(&snd_config_unref)> owned(
      config, snd_config_unref);
  return inspectHdmiRoute(config, device);
}

std::expected<PlaybackPcm, int> openPlayback(const std::string &device,
                                             int mode, snd_config_t *config) {
  snd_pcm_t *pcm = nullptr;
  const int err = snd_pcm_open_lconf(&pcm, device.c_str(),
                                     SND_PCM_STREAM_PLAYBACK, mode, config);
  if (err < 0)
    return std::unexpected(err);
  auto route = inspectHdmiRoute(config, device);
  route.handle = pcm;
  if (snd_pcm_type(pcm) == SND_PCM_TYPE_HW)
    route.access = DeviceAccess::Exclusive;
  route.iec958 = route.iec958 || snd_pcm_type(pcm) == SND_PCM_TYPE_IEC958;
  return route;
}

std::expected<PlaybackPcm, int> openPlayback(const std::string &device,
                                             int mode) {
  snd_config_t *config = nullptr;
  const int err = snd_config_update_ref(&config);
  if (err < 0)
    return std::unexpected(err);
  std::unique_ptr<snd_config_t, decltype(&snd_config_unref)> owned(
      config, snd_config_unref);
  return openPlayback(device, mode, config);
}
