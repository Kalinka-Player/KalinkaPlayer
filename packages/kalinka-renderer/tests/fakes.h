#pragma once

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "capabilities/CapabilityService.h"
#include "player/Player.h"
#include "session/SessionTransport.h"

/**
 * @brief A Player that records what it was told and emits state on demand.
 *
 * Config side: declares one "output" section with an ENUM, an INT and a BOOL
 * field, all driven by public knobs, so a test can shape exactly the schema a
 * scenario needs.
 */
class FakePlayer : public Player {
public:
  StateSink sink;
  std::vector<std::string> calls;
  std::vector<SessionVolumePolicy> sessionVolumes;
  int sessionVolumeEnds = 0;
  std::string sessionVolumeError;

  std::map<std::string, std::string> values{{"output.driver", "alsa"},
                                            {"output.buffer_ms", "100"},
                                            {"output.exclusive", "false"}};
  std::vector<std::string> driverOptions{"alsa"};
  bool driverReadOnly = false;
  kalinka::renderer::v1::ConfigImportance bufferImportance =
      kalinka::renderer::v1::CONFIG_IMPORTANCE_EXPERT;
  // Unset until a test declares what the buffer accepts.
  std::optional<std::pair<int64_t, int64_t>> bufferRange;
  bool refuseApply = false;
  // In-effect value may differ from what was asked; empty = store as given.
  std::string normalizedSuffix;

  void setStateSink(StateSink s) override { sink = std::move(s); }

  void setSource(const kalinka::renderer::v1::Source &source) override {
    calls.push_back("set_source:" + source.uri());
  }
  void enqueueSource(const kalinka::renderer::v1::Source &source) override {
    calls.push_back("enqueue_source:" + source.uri());
  }
  void removeSource(const std::string &sourceToken) override {
    calls.push_back("remove_source:" + sourceToken);
  }
  void clearQueue() override { calls.push_back("clear_queue"); }
  void pause() override { calls.push_back("pause"); }
  void resume() override { calls.push_back("resume"); }
  void stop() override { calls.push_back("stop"); }
  void setVolume(uint32_t percent) override {
    calls.push_back("set_volume:" + std::to_string(percent));
  }
  bool beginSessionVolume(const SessionVolumePolicy &volume,
                          std::string &error) override {
    sessionVolumes.push_back(volume);
    error = sessionVolumeError;
    return error.empty();
  }
  void endSessionVolume() override { ++sessionVolumeEnds; }
  void seek(uint64_t positionMs) override {
    calls.push_back("seek:" + std::to_string(positionMs));
  }

  void fillConfig(kalinka::renderer::v1::ConfigSection &out) const override {
    namespace pb = kalinka::renderer::v1;
    out.set_path("output");
    out.set_title("Output");

    pb::ConfigField *driver = out.add_fields();
    driver->set_path("output.driver");
    driver->set_type(pb::CONFIG_FIELD_TYPE_ENUM);
    driver->set_value(values.at("output.driver"));
    driver->set_apply(pb::APPLY_COST_RESTART_REQUIRED);
    driver->set_read_only(driverReadOnly);
    for (const std::string &option : driverOptions) {
      driver->add_options()->set_value(option);
    }

    pb::ConfigField *buffer = out.add_fields();
    buffer->set_path("output.buffer_ms");
    buffer->set_type(pb::CONFIG_FIELD_TYPE_INT);
    buffer->set_value(values.at("output.buffer_ms"));
    buffer->set_apply(pb::APPLY_COST_INSTANT);
    buffer->set_importance(bufferImportance);
    if (bufferRange) {
      buffer->mutable_range()->set_min(bufferRange->first);
      buffer->mutable_range()->set_max(bufferRange->second);
    }

    pb::ConfigField *exclusive = out.add_fields();
    exclusive->set_path("output.exclusive");
    exclusive->set_type(pb::CONFIG_FIELD_TYPE_BOOL);
    exclusive->set_value(values.at("output.exclusive"));
    exclusive->set_apply(pb::APPLY_COST_INTERRUPTS_PLAYBACK);
  }

  bool applyConfig(const std::string &path, const std::string &value,
                   std::string &error) override {
    calls.push_back("apply_config:" + path + "=" + value);
    if (refuseApply) {
      error = "player said no";
      return false;
    }
    values[path] = value + normalizedSuffix;
    return true;
  }

  void fillSnapshot(kalinka::renderer::v1::StateSnapshot &out) const override {
    out.set_playback_state(kalinka::renderer::v1::PLAYBACK_STATE_STOPPED);
  }

  void emitPlaybackState(kalinka::renderer::v1::PlaybackState state) {
    if (!sink) {
      return;
    }
    kalinka::renderer::v1::Envelope env;
    env.mutable_playback_state_changed()->set_state(state);
    sink(env);
  }
};

/// A route that keeps what it was asked to carry.
class FakeTransport : public SessionTransport {
public:
  std::vector<kalinka::renderer::v1::Envelope> sent;
  bool carry = true;
  int sessionClosed = 0;

  bool sendSessionMessage(kalinka::renderer::v1::Envelope &env) override {
    if (!carry) {
      return false;
    }
    sent.push_back(env);
    return true;
  }
  void onSessionClosed() override { ++sessionClosed; }
};

/// Says whatever a test sets.
class FakeCapabilitySource : public CapabilitySource {
public:
  bool dsd = false;

  void
  fillCapabilities(kalinka::renderer::v1::Capabilities &out) const override {
    out.set_dsd(dsd);
  }
};
