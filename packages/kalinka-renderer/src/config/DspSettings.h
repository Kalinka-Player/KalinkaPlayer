#pragma once
#include "ConfigContributor.h"
#include "native_player/dsp/peq/Profile.h"
#include <functional>
#include <map>

/// Renderer-owned room correction, restored from the verified profile on disk.
class DspSettings : public ConfigContributor {
public:
  explicit DspSettings(std::function<void()> changed);
  void fillConfig(kalinka::renderer::v1::ConfigSection &out) const override;
  bool applyConfig(const std::string &path, const std::string &value,
                   std::string &error) override;
  bool bypassed() const;
  peq::Settings current() const;

private:
  bool set(const std::string &path, const std::string &value,
           std::string &error);
  std::map<std::string, std::string> values;
  std::shared_ptr<const peq::Profile> profile;
  std::function<void()> changed;
};
