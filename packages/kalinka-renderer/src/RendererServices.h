#pragma once

#include <memory>

#include "capabilities/CapabilityService.h"
#include "config/ConfigService.h"
#include "session/SessionManager.h"
#include "upgrade/UpgradeService.h"

/**
 * @brief The planes a Core connection talks to, shared by every connection.
 *
 * The session plane decides who owns the audio graph and carries playback;
 * the config plane says how the renderer is set up and needs no session; the
 * upgrade plane replaces the binary and needs neither, which is why a Core
 * that can no longer drive playback can still reach it; the capability plane
 * tells every Core what the renderer can play. Bundled so that adding a plane
 * does not re-thread every constructor between main() and CoreConnection.
 */
struct RendererServices {
  std::shared_ptr<SessionManager> sessions;
  std::shared_ptr<ConfigService> config;
  /// May be null in a build with no upgrade plane; treated as unsupported.
  std::shared_ptr<UpgradeService> upgrade;
  /// May be null; Hello then says nothing of capabilities, as an older
  /// renderer's does.
  std::shared_ptr<CapabilityService> capabilities;
};
