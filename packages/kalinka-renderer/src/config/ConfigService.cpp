#include "ConfigService.h"

#include <spdlog/spdlog.h>

#include <algorithm>
#include <charconv>
#include <functional>

namespace renderer_pb = kalinka::renderer::v1;

namespace {

// Over the shape and the options, never the values: the version tells a Core
// whether the page it is holding still looks like this, and a volume knob
// moving is not a new page.
std::string versionOf(const renderer_pb::ConfigSnapshot &snapshot) {
  std::string blob;
  for (const renderer_pb::ConfigSection &section : snapshot.sections()) {
    blob += section.path() + '\x1f' + section.title() + '\x1e';
    for (const renderer_pb::ConfigField &field : section.fields()) {
      blob += field.path() + '\x1f' + field.title() + '\x1f' +
              std::to_string(field.type()) + '\x1f' +
              std::to_string(field.apply()) + '\x1f' +
              std::to_string(field.importance()) + '\x1f' +
              (field.read_only() ? "1" : "0") + '\x1f' + field.unit() + '\x1f' +
              std::to_string(field.widget()) + '\x1f' +
              std::to_string(field.range().min()) + '\x1f' +
              std::to_string(field.range().max()) + '\x1f' +
              std::to_string(field.range().step());
      for (const renderer_pb::ConfigOption &option : field.options()) {
        blob += '\x1f' + option.value();
      }
      blob += '\x1e';
    }
  }
  char hex[17];
  std::snprintf(hex, sizeof(hex), "%016zx", std::hash<std::string>{}(blob));
  return hex;
}

// The section index says which contributor declared the field: sections are
// filled one per contributor, in registration order.
const renderer_pb::ConfigField *
findField(const renderer_pb::ConfigSnapshot &snapshot, const std::string &path,
          int *sectionIndex = nullptr) {
  for (int i = 0; i < snapshot.sections_size(); ++i) {
    for (const renderer_pb::ConfigField &field :
         snapshot.sections(i).fields()) {
      if (field.path() == path) {
        if (sectionIndex != nullptr) {
          *sectionIndex = i;
        }
        return &field;
      }
    }
  }
  return nullptr;
}

}  // namespace

bool fieldAccepts(const renderer_pb::ConfigField &field,
                  const std::string &value, std::string &error) {
  if (field.read_only()) {
    error = "read-only setting";
    return false;
  }
  switch (field.type()) {
  case renderer_pb::CONFIG_FIELD_TYPE_ENUM: {
    for (const renderer_pb::ConfigOption &option : field.options()) {
      if (option.value() == value) {
        return true;
      }
    }
    error = field.options().empty() ? "no options available"
                                    : "not one of the offered options";
    return false;
  }
  case renderer_pb::CONFIG_FIELD_TYPE_INT: {
    int64_t parsed = 0;
    const char *end = value.data() + value.size();
    auto [stop, ec] = std::from_chars(value.data(), end, parsed);
    if (ec != std::errc{} || stop != end) {
      error = "not a number";
      return false;
    }
    if (field.has_range() &&
        (parsed < field.range().min() || parsed > field.range().max())) {
      error = "must be between " + std::to_string(field.range().min()) +
              " and " + std::to_string(field.range().max());
      return false;
    }
    return true;
  }
  case renderer_pb::CONFIG_FIELD_TYPE_BOOL:
    if (value != "true" && value != "false") {
      error = "expected 'true' or 'false'";
      return false;
    }
    return true;
  default:
    return true;
  }
}

ConfigService::ConfigService(
    std::vector<std::shared_ptr<ConfigContributor>> contributors,
    std::function<void()> onApplied)
    : contributors_(std::move(contributors)),
      onApplied_(std::move(onApplied)) {}

void ConfigService::fillSnapshot(renderer_pb::ConfigSnapshot &out) const {
  for (const auto &contributor : contributors_) {
    contributor->fillConfig(*out.add_sections());
  }
  out.set_config_version(versionOf(out));
}

void ConfigService::apply(const renderer_pb::ConfigUpdate &update,
                          renderer_pb::ConfigResult &out) {
  renderer_pb::ConfigSnapshot before;
  fillSnapshot(before);

  for (const renderer_pb::ConfigUpdate::Setting &setting : update.settings()) {
    renderer_pb::ConfigResult::Outcome *outcome = out.add_outcomes();
    outcome->set_path(setting.path());

    int declaredBy = 0;
    const renderer_pb::ConfigField *field =
        findField(before, setting.path(), &declaredBy);
    if (field == nullptr) {
      outcome->set_error("no such setting");
      spdlog::warn("Refusing config write to unknown setting '{}'",
                   setting.path());
      continue;
    }
    outcome->set_value(field->value());

    std::string error;
    if (!fieldAccepts(*field, setting.value(), error) ||
        !contributors_[declaredBy]->applyConfig(setting.path(),
                                                setting.value(), error)) {
      outcome->set_error(error);
      spdlog::warn("Refusing config write {} = '{}': {}", setting.path(),
                   setting.value(), error);
      continue;
    }
    outcome->set_applied(true);
    if (field->apply() > out.effect()) {
      out.set_effect(field->apply());
    }
  }

  // Re-read: a player may normalise what it was given, and the options may
  // look different once it has been applied.
  renderer_pb::ConfigSnapshot after;
  fillSnapshot(after);
  for (renderer_pb::ConfigResult::Outcome &outcome : *out.mutable_outcomes()) {
    if (const renderer_pb::ConfigField *field =
            findField(after, outcome.path())) {
      outcome.set_value(field->value());
    }
  }
  out.set_config_version(after.config_version());

  const auto &outcomes = out.outcomes();
  if (onApplied_ && std::any_of(outcomes.begin(), outcomes.end(),
                                [](const auto &o) { return o.applied(); })) {
    onApplied_();
  }
}
