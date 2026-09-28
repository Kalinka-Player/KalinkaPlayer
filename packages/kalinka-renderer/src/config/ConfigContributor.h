#pragma once

#include <string>

#include "kalinka/renderer/v1/renderer.pb.h"

/**
 * @brief Whether @p field takes @p value: the field is writable, and the value
 * parses as its type, lies within its range and is among its options.
 *
 * The check ConfigService::apply() makes of every write, for a contributor to
 * hold a value that did not come through it, such as a stored one, to the same.
 *
 * @param error Filled with the reason when the value is not taken.
 */
bool fieldAccepts(const kalinka::renderer::v1::ConfigField &field,
                  const std::string &value, std::string &error);

/**
 * @brief One section of the renderer's settings, owned by whoever declares it.
 *
 * The player contributes its backend-specific section (driver, device, and
 * whatever else the sink needs); anything renderer-level joins by registering
 * another contributor with ConfigService — the schema grows by registration,
 * not by editing the config plane.
 *
 * ConfigService validates every write against the field it names before the
 * declaring contributor sees it, so applyConfig() never receives a path it did
 * not declare, a read-only field, or a value of the wrong shape.
 *
 * @note Called on the io_context thread; no locking.
 */
class ConfigContributor {
public:
  virtual ~ConfigContributor() = default;

  /**
   * @brief The settings this contributor exposes.
   *
   * Unlike playback, this answers on the spot: settings are read and written
   * outside any playback session.
   *
   * @param out Filled with one section. Options are enumerated during the
   *            call, so a device list is as fresh as the request that asked
   *            for it. Field paths must be unique across the whole renderer —
   *            the section's path is the natural prefix — because a write is
   *            routed to whichever contributor declared the path first.
   */
  virtual void fillConfig(kalinka::renderer::v1::ConfigSection &out) const = 0;

  /**
   * @brief Apply one setting, already validated against the field.
   *
   * Applying may stop playback or need a restart — whichever the field
   * declared as its ApplyCost.
   *
   * @param path  A field path from this contributor's fillConfig().
   * @param value The new value, as text.
   * @param error Filled when the value cannot be applied.
   * @return false when nothing changed and @p error says why.
   */
  virtual bool applyConfig(const std::string &path, const std::string &value,
                           std::string &error) = 0;
};
