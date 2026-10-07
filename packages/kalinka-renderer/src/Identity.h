#pragma once

#include <string>

/**
 * @brief Stable renderer identity plus a per-process instance id.
 *
 * A Core tells one renderer from another by rendererId, and one run from the
 * next by instanceId — which is how it knows a renderer restarted rather than
 * merely reconnected.
 */
struct Identity {
  std::string rendererId;  ///< Persistent, shared with every Core.
  std::string instanceId;  ///< Fresh each start.
  std::string machineId;   ///< /etc/machine-id, or empty when unavailable.

  /**
   * @brief Read rendererId from
   *        <KALINKA_PREFIX>/var/lib/kalinka-renderer/renderer_id, minting and
   *        storing one on first run.
   *
   * KALINKA_PREFIX defaults to "/", the same convention as the server's
   * paths.py.
   *
   * @note If the state directory is not writable, a warning is logged and an
   *       ephemeral id is used for this run — so every Core will see this
   *       renderer as a new one at each restart.
   */
  static Identity load();
};

std::string generateUuid();
