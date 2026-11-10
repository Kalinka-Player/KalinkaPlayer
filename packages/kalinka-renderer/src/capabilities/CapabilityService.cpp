#include "CapabilityService.h"

#include <utility>

namespace renderer_pb = kalinka::renderer::v1;

CapabilityService::CapabilityService(
    std::shared_ptr<const CapabilitySource> source)
    : source_(std::move(source)), announced_(current().SerializeAsString()) {}

renderer_pb::Capabilities CapabilityService::current() const {
  renderer_pb::Capabilities capabilities;
  source_->fillCapabilities(capabilities);
  return capabilities;
}

void CapabilityService::addListener(
    std::weak_ptr<CapabilityListener> listener) {
  forgetExpired();
  listeners_.push_back(std::move(listener));
}

void CapabilityService::forgetExpired() {
  std::erase_if(listeners_,
                [](const auto &listener) { return listener.expired(); });
}

void CapabilityService::refresh() {
  const renderer_pb::Capabilities capabilities = current();
  std::string serialized = capabilities.SerializeAsString();
  if (serialized == announced_) {
    return;
  }
  announced_ = std::move(serialized);
  forgetExpired();
  for (const auto &weak : listeners_) {
    if (const auto listener = weak.lock()) {
      listener->onCapabilitiesChanged(capabilities);
    }
  }
}
