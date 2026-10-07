#pragma once

#include <memory>
#include <string>
#include <vector>

#include "kalinka/renderer/v1/renderer.pb.h"

/**
 * @brief Whatever decides what the renderer can play right now.
 *
 * The player, from its settings: it alone knows which of them gate a format.
 *
 * @note Called on the io_context thread; no locking.
 */
class CapabilitySource {
public:
  virtual ~CapabilitySource() = default;

  /// @param out Filled whole: every field states its current answer.
  virtual void
  fillCapabilities(kalinka::renderer::v1::Capabilities &out) const = 0;
};

/// Told what the renderer can play each time it changes. io_context thread.
class CapabilityListener {
public:
  virtual ~CapabilityListener() = default;

  virtual void onCapabilitiesChanged(
      const kalinka::renderer::v1::Capabilities &capabilities) = 0;
};

/**
 * @brief What the renderer can play, and every Core link to restate it to.
 *
 * Every Core hears the same answer, whichever Core's write moved it: a link
 * states it in Hello, and refresh() restates it to every listener that is
 * still alive when it has changed since the last time.
 *
 * @note Lives on the io_context thread; no locking.
 */
class CapabilityService {
public:
  explicit CapabilityService(std::shared_ptr<const CapabilitySource> source);

  /// What the renderer can play now.
  kalinka::renderer::v1::Capabilities current() const;

  /// Tell @p listener of every change for as long as it lives; it is not kept
  /// alive by this.
  void addListener(std::weak_ptr<CapabilityListener> listener);

  /// What the answer depends on may have moved: tell every listener if it did.
  void refresh();

private:
  // A make_shared listener's storage outlives it for as long as a weak_ptr does.
  void forgetExpired();

  std::shared_ptr<const CapabilitySource> source_;
  std::vector<std::weak_ptr<CapabilityListener>> listeners_;
  std::string announced_;  ///< current(), serialized, as last told.
};
