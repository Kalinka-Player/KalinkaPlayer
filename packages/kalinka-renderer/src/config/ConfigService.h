#pragma once

#include <memory>
#include <string>
#include <vector>

#include "ConfigContributor.h"

/**
 * @brief The renderer's settings, as one schema the Core can render.
 *
 * Assembles the sections its contributors declare — one section per
 * contributor, in registration order — validates writes against the field
 * they name, and hands the surviving ones to the contributor that declared
 * them.
 *
 * Answers on any connection, with or without a playback session: settings
 * belong to the renderer, not to whoever is playing through it, so a Core can
 * show a settings page without claiming the audio graph.
 *
 * @note Lives on the io_context thread; no locking.
 */
class ConfigService {
public:
  explicit ConfigService(
      std::vector<std::shared_ptr<ConfigContributor>> contributors);

  /**
   * @brief Schema and current values together, so one round trip is a whole
   *        settings page.
   *
   * @param out Filled with every section, its fields, and their options as
   *            enumerated during this call — a device list is as fresh as the
   *            request that asked for it. config_version covers the shape and
   *            the options, never the values.
   */
  void fillSnapshot(kalinka::renderer::v1::ConfigSnapshot &out) const;

  /**
   * @brief Apply what is valid and report every path either way.
   *
   * A setting is refused, with nothing attempted, when no field declares it,
   * the field is read-only, the text does not parse as the field's type, or an
   * enum value is not among the options offered. Values are re-read afterwards,
   * so what comes back is what is in effect rather than what was asked for.
   *
   * @param out Per-path outcomes, the new config_version, and the worst
   *            ApplyCost among the settings that were actually applied — what
   *            just happened, not what might.
   */
  void apply(const kalinka::renderer::v1::ConfigUpdate &update,
             kalinka::renderer::v1::ConfigResult &out);

private:
  std::vector<std::shared_ptr<ConfigContributor>> contributors_;
};
