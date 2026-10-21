#include "StubPlayer.h"

#include <spdlog/spdlog.h>

#include <chrono>

namespace pb = kalinka::renderer::v1;

namespace {
constexpr const char *kNotImplemented = "playback is not implemented yet";

int64_t nowUnixMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}
}  // namespace

void StubPlayer::setStateSink(StateSink sink) { sink_ = std::move(sink); }

void StubPlayer::setSource(const pb::Source &) { notImplemented("set_source"); }
void StubPlayer::enqueueSource(const pb::Source &) {
  notImplemented("enqueue_source");
}
void StubPlayer::removeSource(const std::string &) {
  notImplemented("remove_source");
}
void StubPlayer::pause() { notImplemented("pause"); }
void StubPlayer::resume() { notImplemented("resume"); }
void StubPlayer::setVolume(uint32_t) { notImplemented("set_volume"); }
void StubPlayer::seek(uint64_t) { notImplemented("seek"); }
void StubPlayer::clearQueue() { reportStopped(); }
void StubPlayer::stop() { reportStopped(); }

void StubPlayer::fillConfig(pb::ConfigSection &out) const {
  out.set_path("output");
  out.set_title("Output");
  out.set_description("Where this renderer sends audio and how it sets the "
                      "level.");

  pb::ConfigField *driver = out.add_fields();
  driver->set_path("output.driver");
  driver->set_title("Driver");
  driver->set_description("Audio backend used to reach the sound card.");
  driver->set_type(pb::CONFIG_FIELD_TYPE_ENUM);
  driver->set_value(settings_.at("output.driver"));
  driver->set_default_value("alsa");
  driver->set_apply(pb::APPLY_COST_RESTART_REQUIRED);
  driver->set_importance(pb::CONFIG_IMPORTANCE_EXPERT);
  pb::ConfigOption *alsa = driver->add_options();
  alsa->set_value("alsa");
  alsa->set_label("ALSA");

  pb::ConfigField *device = out.add_fields();
  device->set_path("output.device");
  device->set_title("Output device");
  device->set_description(
      "The sound card to play through. This build has no audio output, so "
      "there is nothing to choose.");
  device->set_type(pb::CONFIG_FIELD_TYPE_ENUM);
  device->set_value(settings_.at("output.device"));
  device->set_default_value("default");
  device->set_apply(pb::APPLY_COST_INTERRUPTS_PLAYBACK);
  device->set_importance(pb::CONFIG_IMPORTANCE_SIMPLE);
  // No enumerator without an audio graph, so no options: an empty list is the
  // truth, and the config layer refuses writes against it.
}

bool StubPlayer::applyConfig(const std::string &path, const std::string &value,
                             std::string &error) {
  auto setting = settings_.find(path);
  if (setting == settings_.end()) {
    error = "unknown setting";
    return false;
  }
  setting->second = value;
  spdlog::info("Config {} = '{}'", path, value);
  return true;
}

void StubPlayer::fillSnapshot(pb::StateSnapshot &out) const {
  out.set_playback_state(failed_ ? pb::PLAYBACK_STATE_ERROR
                                 : pb::PLAYBACK_STATE_STOPPED);
  out.set_position_ms(0);
  out.set_position_valid(false);
  out.set_captured_at_unix_ms(nowUnixMs());
  out.mutable_volume()->set_supported(false);
  out.mutable_volume()->set_backend(pb::VOLUME_BACKEND_NONE);
  if (failed_) {
    out.mutable_error()->set_source(pb::ERROR_SOURCE_RENDERER_INTERNAL);
    out.mutable_error()->set_message(kNotImplemented);
  }
}

void StubPlayer::notImplemented(const char *command) {
  spdlog::warn("Command {}: {}", command, kNotImplemented);
  failed_ = true;
  if (!sink_) {
    return;
  }
  pb::Envelope env;
  pb::PlaybackStateChanged *changed = env.mutable_playback_state_changed();
  changed->set_state(pb::PLAYBACK_STATE_ERROR);
  changed->set_position_valid(false);
  changed->set_at_unix_ms(nowUnixMs());
  changed->mutable_error()->set_source(pb::ERROR_SOURCE_RENDERER_INTERNAL);
  changed->mutable_error()->set_message(kNotImplemented);
  sink_(env);
}

void StubPlayer::reportStopped() {
  if (!failed_) {
    return;
  }
  failed_ = false;
  if (!sink_) {
    return;
  }
  pb::Envelope env;
  pb::PlaybackStateChanged *changed = env.mutable_playback_state_changed();
  changed->set_state(pb::PLAYBACK_STATE_STOPPED);
  changed->set_position_valid(false);
  changed->set_at_unix_ms(nowUnixMs());
  sink_(env);
}
