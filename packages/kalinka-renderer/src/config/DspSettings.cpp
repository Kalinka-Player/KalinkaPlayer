#include "DspSettings.h"
#include "DspProfileCache.h"
#include "SettingsPersistence.h"
#include <algorithm>
#include <charconv>
#include <spdlog/spdlog.h>

namespace renderer_pb = kalinka::renderer::v1;
namespace {
const std::map<std::string, std::string> defaults{{"dsp.enabled", "false"},
                                                  {"dsp.profile", "none"},
                                                  {"dsp.bypass", "false"},
                                                  {"dsp.preamp", "auto"},
                                                  {"dsp.preamp_db", "0"}};
void option(renderer_pb::ConfigField &field, const std::string &id,
            const std::string &label) {
  auto &o = *field.add_options();
  o.set_value(id);
  o.set_label(label);
}
} // namespace
DspSettings::DspSettings(std::function<void()> changed)
    : values(defaults), changed(std::move(changed)) {
  for (const auto &[path, value] : loadSettingsOverrides()) {
    if (!values.contains(path))
      continue;
    std::string error;
    if (!set(path, value, error))
      spdlog::warn("Ignoring {}: {}", path, error);
  }
}

void DspSettings::fillConfig(renderer_pb::ConfigSection &out) const {
  out.set_path("dsp");
  out.set_title("Room correction");
  auto field = [&](std::string path, std::string title,
                   renderer_pb::ConfigFieldType type,
                   std::string description) -> renderer_pb::ConfigField & {
    auto &f = *out.add_fields();
    f.set_path(path);
    f.set_title(title);
    f.set_type(type);
    f.set_description(description);
    f.set_value(values.at(path));
    f.set_default_value(defaults.at(path));
    f.set_importance(path.starts_with("dsp.preamp")
                         ? renderer_pb::CONFIG_IMPORTANCE_EXPERT
                         : renderer_pb::CONFIG_IMPORTANCE_SIMPLE);
    f.set_apply(renderer_pb::APPLY_COST_INSTANT);
    return f;
  };
  field("dsp.enabled", "Room correction", renderer_pb::CONFIG_FIELD_TYPE_BOOL,
        "Apply room correction to PCM playback.");
  auto &selected =
      field("dsp.profile", "Room profile", renderer_pb::CONFIG_FIELD_TYPE_ENUM,
            "Select a room correction profile imported by the Core.");
  option(selected, "none", "None");
  if (profile)
    option(selected, values.at("dsp.profile"), profile->name);
  field("dsp.bypass", "Bypass", renderer_pb::CONFIG_FIELD_TYPE_BOOL,
        "Bypass the FIR filter, parametric EQ and preamp together.");
  auto &mode =
      field("dsp.preamp", "Preamp", renderer_pb::CONFIG_FIELD_TYPE_ENUM,
            "Automatic reserves headroom for the filters on each channel.");
  option(mode, "auto", "Automatic");
  option(mode, "manual", "Manual");
  auto &db = field("dsp.preamp_db", "Manual preamp",
                   renderer_pb::CONFIG_FIELD_TYPE_DECIMAL,
                   "Attenuation applied in manual mode.");
  db.set_unit("dB");
  db.set_widget(renderer_pb::CONFIG_WIDGET_NUMBER);
  auto *range = db.mutable_decimal_range();
  range->set_min(-30);
  range->set_max(0);
  range->set_step(0.1);
}

bool DspSettings::set(const std::string &path, const std::string &value,
                      std::string &error) {
  if (path == "dsp.profile") {
    try {
      profile = value == "none" ? nullptr : dsp_profiles::load(value);
    } catch (const std::exception &e) {
      error = e.what();
      return false;
    }
  } else {
    renderer_pb::ConfigSection section;
    fillConfig(section);
    const auto &fields = section.fields();
    const auto field = std::ranges::find_if(
        fields, [&](const auto &f) { return f.path() == path; });
    if (field == fields.end()) {
      error = "Unknown DSP setting";
      return false;
    }
    if (!fieldAccepts(*field, value, error))
      return false;
  }
  values.at(path) = value;
  return true;
}

bool DspSettings::applyConfig(const std::string &path, const std::string &value,
                              std::string &error) {
  if (const auto current = values.find(path);
      current != values.end() && current->second == value)
    return true;
  if (!set(path, value, error))
    return false;
  updateSettingsOverrides(values, defaults);
  changed();
  return true;
}

bool DspSettings::bypassed() const { return values.at("dsp.bypass") == "true"; }

peq::Settings DspSettings::current() const {
  if (values.at("dsp.enabled") != "true" || bypassed())
    return {};
  peq::Settings settings{profile, std::nullopt};
  if (values.at("dsp.preamp") == "manual") {
    const auto &value = values.at("dsp.preamp_db");
    double db = 0;
    std::from_chars(value.data(), value.data() + value.size(), db);
    settings.preampDb = db;
  }
  return settings;
}
